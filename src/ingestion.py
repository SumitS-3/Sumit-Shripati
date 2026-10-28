"""
Input discovery, ingestion, and schema validation.

Responsibilities:
  - Safe path traversal protection
  - File discovery
  - CSV parsing with defensive error handling
  - Schema validation
  - Timestamp normalization
  - Initial duplicate / sort repair
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .config import CheckerConfig

logger = logging.getLogger(__name__)

# Expected CSV columns and their dtypes (post-parse)
REQUIRED_COLUMNS: List[str] = [
    "timestamp_utc",
    "temp_c",
    "rh_pct",
    "pressure_hpa",
    "wind_kmh",
    "wind_dir_deg",
    "rain_mm",
    "ghi_wm2",
]

NUMERIC_COLUMNS: List[str] = [c for c in REQUIRED_COLUMNS if c != "timestamp_utc"]

STATIONS_SCHEMA: List[str] = [
    "station_id", "name", "lat", "lon", "elevation_m", "installed", "logger_interval_min"
]


# ── Path safety ──────────────────────────────────────────────────────────────

def _safe_resolve(base: Path, child: Path) -> Path:
    """Resolve child path and ensure it stays within base. Raises ValueError otherwise."""
    resolved = (base / child).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        raise ValueError(f"Path traversal attempt: {child!r} escapes {base!r}")
    return resolved


def _is_safe_csv(path: Path) -> bool:
    """Check path is a regular file with .csv extension (no symlinks allowed)."""
    if path.is_symlink():
        logger.warning("Skipping symlink: %s", path)
        return False
    if not path.is_file():
        return False
    if path.suffix.lower() != ".csv":
        return False
    return True


def _is_safe_nmea(path: Path) -> bool:
    if path.is_symlink():
        logger.warning("Skipping symlink: %s", path)
        return False
    if not path.is_file():
        return False
    if path.suffix.lower() != ".nmea":
        return False
    return True


# ── Station metadata ──────────────────────────────────────────────────────────

def load_stations_meta(pack_root: Path) -> pd.DataFrame:
    """Load stations.csv and validate schema. Returns empty DataFrame on failure."""
    path = _safe_resolve(pack_root, Path("stations.csv"))
    if not path.exists():
        logger.warning("stations.csv not found at %s", path)
        return pd.DataFrame(columns=STATIONS_SCHEMA)

    try:
        df = pd.read_csv(path, dtype=str, nrows=10_000)
    except Exception as exc:
        logger.error("Cannot read stations.csv: %s", exc)
        return pd.DataFrame(columns=STATIONS_SCHEMA)

    missing = [c for c in STATIONS_SCHEMA if c not in df.columns]
    if missing:
        logger.warning("stations.csv missing columns: %s", missing)

    for col in ("lat", "lon", "elevation_m"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    logger.info("Loaded metadata for %d stations.", len(df))
    return df


# ── Observation data ──────────────────────────────────────────────────────────

def discover_station_files(pack_root: Path) -> Dict[str, Path]:
    """
    Discover data/Sxx.csv files.  Returns {station_id: path} dict sorted by ID.
    Rejects symlinks and paths outside pack_root.
    """
    data_dir = _safe_resolve(pack_root, Path("data"))
    if not data_dir.is_dir():
        logger.error("data/ directory not found under %s", pack_root)
        return {}

    result: Dict[str, Path] = {}
    for entry in sorted(data_dir.iterdir()):
        try:
            safe = _safe_resolve(data_dir, entry.relative_to(data_dir))
        except ValueError as exc:
            logger.warning("Skipping unsafe path: %s", exc)
            continue
        if not _is_safe_csv(safe):
            continue
        # Station ID = filename stem; must match pattern Sxx (flexible)
        stem = safe.stem
        if re.fullmatch(r"[A-Z][0-9]+", stem):
            result[stem] = safe
        else:
            logger.debug("Ignoring non-station CSV: %s", safe.name)

    logger.info("Discovered %d station data file(s): %s", len(result), list(result))
    return result


def load_station_data(
    station_id: str,
    path: Path,
    cfg: CheckerConfig,
) -> Tuple[Optional[pd.DataFrame], List[str]]:
    """
    Load and validate one station's CSV.

    Returns (DataFrame, warnings_list).
    DataFrame has:
      - timestamp_utc as tz-naive UTC datetime
      - all numeric columns as float64
      - is sorted by timestamp ascending
    Duplicate timestamps are kept with a 'dup_ts' boolean column added.
    Returns (None, errors) if the file is unreadable or has no valid rows.
    """
    warnings: List[str] = []

    # Size guard: 50 MB per file is generous for 6-week 10-min data
    try:
        size = path.stat().st_size
    except OSError:
        return None, [f"Cannot stat {path}"]
    if size > 50 * 1024 * 1024:
        warnings.append(f"File unexpectedly large ({size/1e6:.1f} MB); loading anyway.")

    # ── Read CSV ────────────────────────────────────────────────────────
    try:
        df = pd.read_csv(
            path,
            dtype=str,      # read everything as string first for safe validation
            nrows=100_000,  # resource guard
        )
    except Exception as exc:
        return None, [f"Cannot read {path.name}: {exc}"]

    if df.empty:
        return None, [f"{path.name} is empty."]

    # ── Column check ────────────────────────────────────────────────────
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        warnings.append(f"{station_id}: Missing columns {missing_cols}; continuing with available data.")

    # ── Timestamp parsing ────────────────────────────────────────────────
    if "timestamp_utc" not in df.columns:
        return None, [f"{station_id}: No timestamp_utc column."]

    # Sanitise: truncate very long strings before parsing (injection guard)
    df["timestamp_utc"] = df["timestamp_utc"].astype(str).str[:30]
    ts = pd.to_datetime(df["timestamp_utc"], format="%Y-%m-%d %H:%M", errors="coerce")
    bad_ts = ts.isna().sum()
    if bad_ts > 0:
        warnings.append(f"{station_id}: {bad_ts} unparseable timestamp(s) dropped.")
    df["timestamp_utc"] = ts
    df = df.dropna(subset=["timestamp_utc"])
    if df.empty:
        return None, [f"{station_id}: No parseable timestamps."]

    # ── Numeric conversion ───────────────────────────────────────────────
    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            # Guard against formula injection: truncate before numeric parse
            df[col] = df[col].astype(str).str[:30]
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = float("nan")

    # Replace infinities
    df = df.replace([float("inf"), float("-inf")], float("nan"))

    # ── Sort ──────────────────────────────────────────────────────────
    df = df.sort_values("timestamp_utc").reset_index(drop=True)

    # ── Mark duplicates ───────────────────────────────────────────────
    df["dup_ts"] = df.duplicated("timestamp_utc", keep=False)
    dup_count = df["dup_ts"].sum()
    if dup_count:
        warnings.append(
            f"{station_id}: {dup_count} rows share duplicate timestamps "
            f"(e.g. around {df.loc[df['dup_ts'], 'timestamp_utc'].iloc[0]})."
        )

    logger.info(
        "%s: loaded %d rows (%s to %s); %d dup_ts, %d missing-ts dropped.",
        station_id, len(df),
        df["timestamp_utc"].iloc[0].strftime("%Y-%m-%d"),
        df["timestamp_utc"].iloc[-1].strftime("%Y-%m-%d"),
        dup_count, bad_ts,
    )
    return df, warnings


# ── GPS data ──────────────────────────────────────────────────────────────────

def discover_gps_files(pack_root: Path) -> Dict[str, Path]:
    """Discover gps/Sxx.nmea files. Same safety rules as CSV discovery."""
    gps_dir = _safe_resolve(pack_root, Path("gps"))
    if not gps_dir.is_dir():
        logger.debug("No gps/ directory found.")
        return {}

    result: Dict[str, Path] = {}
    for entry in sorted(gps_dir.iterdir()):
        try:
            safe = _safe_resolve(gps_dir, entry.relative_to(gps_dir))
        except ValueError as exc:
            logger.warning("Skipping unsafe GPS path: %s", exc)
            continue
        if not _is_safe_nmea(safe):
            continue
        stem = safe.stem
        if re.fullmatch(r"[A-Z][0-9]+", stem):
            result[stem] = safe

    logger.info("Discovered %d GPS file(s).", len(result))
    return result


# ── Maintenance log ───────────────────────────────────────────────────────────

def load_maintenance_log(pack_root: Path) -> str:
    """Return raw maintenance log text. Returns empty string if not found."""
    path = _safe_resolve(pack_root, Path("maintenance_log.md"))
    if not path.exists():
        logger.warning("maintenance_log.md not found.")
        return ""
    try:
        # Size guard: maintenance log should be tiny
        if path.stat().st_size > 1 * 1024 * 1024:
            logger.warning("maintenance_log.md is unexpectedly large; truncating.")
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(512 * 1024)  # max 512 KB
        return text
    except OSError as exc:
        logger.warning("Cannot read maintenance_log.md: %s", exc)
        return ""

