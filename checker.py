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
    2 — partial completion (a station or report failed)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
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
from src.gps import analyze_gps
from src.temporal import prepare_observations
from src.schema import FLAG_COLUMNS
from src.maintenance import parse_maintenance_log
from src.network_health import compute_station_summaries, recommend_technician_visit
from src.report import generate_html_report
from src.network_checks import run_network_checks


def _failure_flag(station_id: str, reason: str) -> dict:
    """A failed station remains visible in the machine-readable output."""
    return {
        "station_id": station_id, "channel": "metadata", "start_utc": "", "end_utc": "",
        "issue": "Station analysis incomplete", "action": "exclude",
        "note": f"Withhold this station from the summary until the failed analysis is rerun: {reason}.",
    }

# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
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
    parser.add_argument("data_pack", nargs="?", help="Path to the data-pack directory.")
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
    _setup_logging(args.verbose, quiet=args.benchmark)
    logger = logging.getLogger("checker")

    # ── Optional benchmark mode ───────────────────────────────────────────
    if args.benchmark:
        from src.benchmark import run_benchmark
        run_benchmark()
        return 0
    if not args.data_pack:
        parser.error("data_pack is required unless --benchmark is used")

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
    try:
        if args.config and not Path(config_path).is_file():
            raise ValueError(f"configuration file not found: {config_path}")
        cfg = CheckerConfig.from_file(config_path)
    except (OSError, ValueError, TypeError) as exc:
        logger.error("Invalid configuration: %s", exc)
        return 1
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
    absent_stations = sorted(set(meta_by_id) - set(station_files))
    all_flag_rows: List[dict] = [
        _failure_flag(sid, "station listed in stations.csv has no observation file")
        for sid in absent_stations
    ]
    station_data: Dict[str, pd.DataFrame] = {}
    station_candidates: Dict[str, list[dict]] = {}
    station_configs: Dict[str, CheckerConfig] = {}
    n_failed = len(absent_stations)

    for station_id, csv_path in sorted(station_files.items()):
        logger.info("Processing %s ...", station_id)

        # Load data
        df, load_warnings = load_station_data(station_id, csv_path, cfg)
        for w in load_warnings:
            logger.warning(w)

        if df is None:
            logger.error("Skipping %s: could not load data.", station_id)
            n_failed += 1
            all_flag_rows.append(_failure_flag(station_id, "; ".join(load_warnings)))
            continue

        # Establish time and replay handling before interpreting sensor changes.
        try:
            meta = meta_by_id.get(station_id, {})
            cadence = int(meta.get("logger_interval_min", cfg.sampling_interval_min))
            station_cfg = replace(cfg, sampling_interval_min=cadence,
                                  gap_threshold_min=max(cadence, cfg.gap_threshold_min))
            samples = []
            candidates = []
            if station_id in gps_files:
                analysis = analyze_gps(station_id, gps_files[station_id],
                                       meta.get("lat"), meta.get("lon"), station_cfg)
                samples = analysis.clock_samples
                candidates.extend(analysis.flags)
                if analysis.position is not None:
                    meta = {**meta, "lat": analysis.position[0], "lon": analysis.position[1]}
                    meta_by_id[station_id] = meta
            df, temporal_flags = prepare_observations(df, samples, station_cfg)
            candidates.extend(temporal_flags)
            candidates.extend(run_all_checks(station_id, df, station_cfg, meta.get("lat"), meta.get("lon")))
        except Exception as exc:
            logger.error("Checks failed for %s: %s", station_id, exc, exc_info=True)
            n_failed += 1
            all_flag_rows.append(_failure_flag(station_id, str(exc)))
            continue
        station_data[station_id] = df
        station_candidates[station_id] = candidates
        station_configs[station_id] = station_cfg

    # Network evidence operates on corrected, deduplicated timelines and masks
    # first-pass local exclusions. No peer measurements are copied into source data.
    try:
        network = run_network_checks(station_data, meta_by_id, station_candidates,
                                     station_configs, maintenance_events)
        station_candidates = network.flags
        for sid, reason in network.failures.items():
            all_flag_rows.append(_failure_flag(sid, f"network evidence failed: {reason}"))
            n_failed += 1
    except Exception as exc:
        logger.error("Network evidence checks failed: %s", exc, exc_info=True)
        for sid in station_data:
            all_flag_rows.append(_failure_flag(sid, f"network evidence failed: {exc}"))
        n_failed += len(station_data)

    for station_id, candidates in station_candidates.items():
        flag_rows = build_flag_rows(station_id, candidates, maintenance_events, station_configs[station_id])
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
        required = FLAG_COLUMNS
        missing = [c for c in required if c not in out_df.columns]
        if missing:
            logger.error("flags.csv missing columns: %s", missing)
            return 1
        logger.info("flags.csv schema validated: %d rows, columns correct.", len(out_df))
    except Exception as exc:
        logger.error("Cannot validate flags.csv: %s", exc)
        return 1

    # ── Generate network health report ────────────────────────────────────
    n_processed = len(station_data)
    try:
        flags_for_report = out_df.to_dict("records")
        summaries = compute_station_summaries(station_data, out_df, stations_meta, station_configs)
        # Report generation needs compact summaries, not the network's raw frames.
        station_data.clear()
        tech_station, runner_up = recommend_technician_visit(summaries)
        logger.info(
            "Technician recommendation: %s (runner-up: %s)",
            tech_station, runner_up
        )
        generate_html_report(summaries, flags_for_report, tech_station, runner_up, report_path, cfg.report_title)
    except Exception as exc:
        logger.error("Report generation failed: %s", exc, exc_info=True)
        # flags.csv remains available, but the run did not produce every deliverable.
        return 2

    # ── Summary ───────────────────────────────────────────────────────────
    elapsed = time.perf_counter() - t_start
    logger.info(
        "Done in %.1f s: %d stations processed, %d failed, %d flags written.",
        elapsed, n_processed, n_failed, len(all_flag_rows),
    )

    if n_failed > 0 and n_processed == 0:
        return 1
    if n_failed > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
