#!/usr/bin/env python3
"""
checker.py — Ridgeline Schools Weather Network Quality Checker

Usage:
    python checker.py <data_pack_directory>

Outputs (to current working directory):
    flags.csv   — data-quality flags (one row per problem interval)
    report.html — human-readable network health report

Exit codes:
    0 — completed successfully
    1 — fatal input error (missing directory, unreadable files)
    2 — partial completion (some stations failed)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import pandas as pd

# ── Ensure src is importable even when run from a different directory ─────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from src.config import CheckerConfig
from src.ingestion import (
    discover_gps_files,
    discover_station_files,
    load_maintenance_log,
    load_station_data,
    load_stations_meta,
)
from src.checks import run_all_checks
from src.flagging import build_flag_rows, write_flags_csv
from src.gps import check_gps_integrity
from src.maintenance import parse_maintenance_log
from src.network_health import compute_station_summaries, recommend_technician_visit
from src.report import generate_html_report

# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    # Suppress noisy third-party loggers
    logging.getLogger("pandas").setLevel(logging.WARNING)
    logging.getLogger("numpy").setLevel(logging.WARNING)


# ── Input validation ──────────────────────────────────────────────────────────

def _validate_input_directory(raw_path: str) -> Path:
    """
    Resolve and validate the data-pack directory.
    Protects against path traversal by resolving and checking it exists.
    """
    p = Path(raw_path).resolve()
    if not p.exists():
        print(f"ERROR: Data pack directory not found: {p}", file=sys.stderr)
        sys.exit(1)
    if not p.is_dir():
        print(f"ERROR: Argument is not a directory: {p}", file=sys.stderr)
        sys.exit(1)
    if p.is_symlink():
        print(f"ERROR: Directory is a symlink, refusing to follow: {p}", file=sys.stderr)
        sys.exit(1)
    return p


def _validate_output_directory(out_dir: str) -> Path:
    p = Path(out_dir).resolve()
    if not p.exists():
        print(f"ERROR: Output directory not found: {p}", file=sys.stderr)
        sys.exit(1)
    if not p.is_dir():
        print(f"ERROR: Output path is not a directory: {p}", file=sys.stderr)
        sys.exit(1)
    return p


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="checker.py",
        description="Ridgeline weather-station data quality checker.",
    )
    parser.add_argument("data_pack", help="Path to the data-pack directory.")
    parser.add_argument(
        "--output-dir", default=".",
        help="Directory to write flags.csv and report.html (default: current directory).",
    )
    parser.add_argument(
        "--config", default=None,
        help="Optional JSON config file to override default thresholds.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    parser.add_argument(
        "--benchmark", action="store_true",
        help="Run synthetic scalability benchmark and exit.",
    )

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    logger = logging.getLogger("checker")

    # ── Optional benchmark mode ───────────────────────────────────────────
    if args.benchmark:
        from src.benchmark import run_benchmark
        run_benchmark([8, 50, 100])
        return 0

    t_start = time.perf_counter()
    logger.info("Ridgeline checker starting.")

    # ── Validate paths ────────────────────────────────────────────────────
    pack_root  = _validate_input_directory(args.data_pack)
    output_dir = _validate_output_directory(args.output_dir)
    flags_path  = str(output_dir / "flags.csv")
    report_path = str(output_dir / "report.html")

    # Refuse to overwrite source data files (data/ subdirectory only)
    for src_file in (pack_root / "data").glob("*.csv") if (pack_root / "data").exists() else []:
        for dangerous in [flags_path, report_path]:
            if Path(dangerous).resolve() == src_file.resolve():
                logger.error("Output path would overwrite source file: %s", dangerous)
                return 1

    # ── Load configuration ────────────────────────────────────────────────
    config_path = args.config or str(pack_root / "config.json")
    cfg = CheckerConfig.from_file(config_path)
    logger.info("Loaded configuration (sampling_interval=%d min).", cfg.sampling_interval_min)

    # ── Load inputs ───────────────────────────────────────────────────────
    stations_meta    = load_stations_meta(pack_root)
    station_files    = discover_station_files(pack_root)
    gps_files        = discover_gps_files(pack_root)
    maintenance_text = load_maintenance_log(pack_root)
    maintenance_events = parse_maintenance_log(maintenance_text)

    if not station_files:
        logger.error("No station data files found in %s/data/", pack_root)
        return 1

    # ── Build metadata lookup ─────────────────────────────────────────────
    meta_by_id: Dict[str, dict] = {}
    if not stations_meta.empty:
        for _, row in stations_meta.iterrows():
            sid = str(row.get("station_id", ""))
            meta_by_id[sid] = row.to_dict()

    # ── Process each station ──────────────────────────────────────────────
    all_flag_rows: List[dict] = []
    station_data: Dict[str, pd.DataFrame] = {}
    n_failed = 0

    for station_id, csv_path in sorted(station_files.items()):
        logger.info("Processing %s ...", station_id)

        # Load data
        df, load_warnings = load_station_data(station_id, csv_path, cfg)
        for w in load_warnings:
            logger.warning(w)

        if df is None:
            logger.error("Skipping %s: could not load data.", station_id)
            n_failed += 1
            continue

        station_data[station_id] = df

        # Run all validation checks
        try:
            candidates = run_all_checks(station_id, df, cfg)
        except Exception as exc:
            logger.error("Checks failed for %s: %s", station_id, exc, exc_info=True)
            n_failed += 1
            continue

        # GPS checks
        if station_id in gps_files:
            meta = meta_by_id.get(station_id, {})
            meta_lat = meta.get("lat") if isinstance(meta.get("lat"), float) else None
            meta_lon = meta.get("lon") if isinstance(meta.get("lon"), float) else None
            try:
                gps_flags = check_gps_integrity(
                    station_id,
                    gps_files[station_id],
                    meta_lat,
                    meta_lon,
                )
                candidates.extend(gps_flags)
            except Exception as exc:
                logger.warning("GPS check failed for %s: %s", station_id, exc)

        # Build and collect flag rows
        flag_rows = build_flag_rows(station_id, candidates, maintenance_events, cfg)
        all_flag_rows.extend(flag_rows)
        logger.info("%s: %d final flags.", station_id, len(flag_rows))

    # ── Write flags.csv ───────────────────────────────────────────────────
    try:
        write_flags_csv(all_flag_rows, flags_path)
    except Exception as exc:
        logger.error("Failed to write flags.csv: %s", exc)
        return 1

    # ── Validate output schema ────────────────────────────────────────────
    try:
        out_df = pd.read_csv(flags_path)
        required = ["station_id", "channel", "start_utc", "end_utc", "issue", "action", "note"]
        missing = [c for c in required if c not in out_df.columns]
        if missing:
            logger.error("flags.csv missing columns: %s", missing)
            return 1
        logger.info("flags.csv schema validated: %d rows, columns correct.", len(out_df))
    except Exception as exc:
        logger.error("Cannot validate flags.csv: %s", exc)
        return 1

    # ── Generate network health report ────────────────────────────────────
    try:
        flags_for_report = out_df.to_dict("records")
        summaries = compute_station_summaries(station_data, out_df, stations_meta)
        tech_station, runner_up = recommend_technician_visit(summaries)
        logger.info(
            "Technician recommendation: %s (runner-up: %s)",
            tech_station, runner_up
        )
        generate_html_report(summaries, flags_for_report, tech_station, runner_up, report_path)
    except Exception as exc:
        logger.error("Report generation failed: %s", exc, exc_info=True)
        # Non-fatal: flags.csv is already written

    # ── Summary ───────────────────────────────────────────────────────────
    elapsed = time.perf_counter() - t_start
    logger.info(
        "Done in %.1f s: %d stations processed, %d failed, %d flags written.",
        elapsed, len(station_data), n_failed, len(all_flag_rows),
    )

    if n_failed > 0 and len(station_data) == 0:
        return 1
    if n_failed > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
