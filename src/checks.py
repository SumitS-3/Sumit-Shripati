"""
Physical-plausibility and temporal-anomaly validation rules.

Design principles:
  - Each check is a pure function: (DataFrame, CheckerConfig) -> list[flag_candidates]
  - No station-specific hardcoding; all thresholds come from config.
  - Vectorised pandas operations; no row-by-row Python loops over data.
  - Reusable helper functions avoid duplicated logic.
  - Scientific conservatism: only flag what is clearly anomalous.

Flag candidate dict keys:
    channel      : str   -- column name, "all", or "metadata"
    start_utc    : pd.Timestamp
    end_utc      : pd.Timestamp
    issue        : str   -- concise description
    action       : str   -- "exclude", "correct", or "keep"
    note         : str   -- one supporting-evidence sentence
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from .config import CheckerConfig

logger = logging.getLogger(__name__)

FlagCandidate = Dict[str, Any]


# ── Helper: run-length encoding ───────────────────────────────────────────────

def _find_runs(series: pd.Series, condition: pd.Series) -> List[Tuple[int, int, int]]:
    """
    Find contiguous runs where `condition` is True.
    Returns list of (start_iloc, end_iloc, run_length).  Both bounds inclusive.
    """
    runs: List[Tuple[int, int, int]] = []
    arr = condition.values
    n = len(arr)
    i = 0
    while i < n:
        if arr[i]:
            j = i + 1
            while j < n and arr[j]:
                j += 1
            runs.append((i, j - 1, j - i))
            i = j
        else:
            i += 1
    return runs


def _flag_from_run(
    df: pd.DataFrame,
    start_i: int,
    end_i: int,
    channel: str,
    issue: str,
    action: str,
    note: str,
) -> FlagCandidate:
    return {
        "channel":   channel,
        "start_utc": df["timestamp_utc"].iloc[start_i],
        "end_utc":   df["timestamp_utc"].iloc[end_i],
        "issue":     issue,
        "action":    action,
        "note":      note,
    }


# ── Check 1: Data completeness ────────────────────────────────────────────────

def check_completeness(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """
    Detect:
      - Gaps larger than cfg.gap_threshold_min
      - Duplicate timestamps
      - NaN values in numeric columns
    """
    flags: List[FlagCandidate] = []
    interval_s = cfg.sampling_interval_min * 60
    threshold_s = cfg.gap_threshold_min * 60

    diffs = df["timestamp_utc"].diff().dt.total_seconds()

    # ── Gaps ──────────────────────────────────────────────────────────────
    gap_mask = diffs > threshold_s
    gap_indices = df.index[gap_mask].tolist()
    for idx in gap_indices:
        t_before = df["timestamp_utc"].iloc[idx - 1]
        t_after  = df["timestamp_utc"].iloc[idx]
        gap_min  = int(diffs.iloc[idx] / 60)
        n_missing = int(diffs.iloc[idx] / interval_s) - 1
        flags.append({
            "channel":   "all",
            "start_utc": t_before,
            "end_utc":   t_after,
            "issue":     f"Data gap: {gap_min} min ({n_missing} missing intervals)",
            "action":    "exclude",
            "note":      (
                f"{gap_min}-minute gap in observations "
                f"({t_before.strftime('%Y-%m-%d %H:%M')} "
                f"to {t_after.strftime('%Y-%m-%d %H:%M')}); "
                f"{n_missing} expected records absent."
            ),
        })

    # ── Duplicate timestamps ───────────────────────────────────────────────
    dup_col = df.get("dup_ts", pd.Series(False, index=df.index))
    dup_rows = df[dup_col]
    if len(dup_rows) > 0:
        dup_times = dup_rows["timestamp_utc"].sort_values()
        t0 = dup_times.iloc[0]
        t1 = dup_times.iloc[-1]
        n  = len(dup_rows)
        flags.append({
            "channel":   "all",
            "start_utc": t0,
            "end_utc":   t1,
            "issue":     f"Duplicate timestamps: {n} rows share {n // 2} timestamp(s)",
            "action":    "correct",
            "note":      (
                f"{n} rows have duplicate timestamps "
                f"({t0.strftime('%Y-%m-%d %H:%M')} to {t1.strftime('%Y-%m-%d %H:%M')}); "
                "retain the row with plausible sensor values and discard the other."
            ),
        })

    # ── Missing values ─────────────────────────────────────────────────────
    numeric_cols = [c for c in df.columns if c not in ("timestamp_utc", "dup_ts")]
    for col in numeric_cols:
        if col not in df.columns:
            continue
        nan_count = df[col].isna().sum()
        if nan_count > 0:
            nan_rows = df[df[col].isna()]
            t0 = nan_rows["timestamp_utc"].iloc[0]
            t1 = nan_rows["timestamp_utc"].iloc[-1]
            flags.append({
                "channel":   col,
                "start_utc": t0,
                "end_utc":   t1,
                "issue":     f"Missing values: {nan_count} NaN in {col}",
                "action":    "exclude",
                "note":      (
                    f"{nan_count} records have no value for {col}; "
                    "these intervals cannot be used in the climate summary."
                ),
            })

    return flags


# ── Check 2: Physical plausibility ────────────────────────────────────────────

def check_physical_bounds(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """
    Flag values outside documented physical bounds for each channel.
    Bounds are wide screening limits, not tight quality thresholds.
    """
    flags: List[FlagCandidate] = []

    for col, bounds in cfg.physical_bounds.items():
        if col not in df.columns:
            continue
        v = df[col]
        lo, hi = bounds["min"], bounds["max"]
        bad_mask = (v < lo) | (v > hi)
        bad_mask = bad_mask.fillna(False)

        for si, ei, cnt in _find_runs(df, bad_mask):
            val_min = v.iloc[si:ei + 1].min()
            val_max = v.iloc[si:ei + 1].max()
            flags.append(_flag_from_run(
                df, si, ei, col,
                issue=f"Out-of-range {col}: [{val_min:.2f}, {val_max:.2f}]",
                action="exclude",
                note=(
                    f"{cnt} record(s) in {col} outside physical bounds "
                    f"[{lo}, {hi}]; values [{val_min:.2f}, {val_max:.2f}] "
                    "are not physically credible."
                ),
            ))

    return flags


# ── Check 3: Flatlines (stuck sensor / logger freeze) ────────────────────────

def check_flatlines(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """
    Detect runs of identical values exceeding the configured minimum run length.

    Special cases:
      - rain_mm=0 and wind_kmh=0 are exempted (genuine calm/dry spells).
      - ghi_wm2=0 at night is expected; only flag during expected daylight.
      - rh_pct=100: short episodes are consistent with fog; longer ones suspect.
    """
    flags: List[FlagCandidate] = []

    for col, min_run in cfg.flatline_min_records.items():
        if col not in df.columns:
            continue
        series = df[col]

        # Group consecutive identical values
        group_ids = (series != series.shift()).cumsum()
        run_lens  = series.groupby(group_ids).transform("count")

        # Build mask of long runs
        flat_mask = run_lens >= min_run

        # Exempt channels where zero is physically meaningful
        if col in cfg.flatline_exempt_zeros:
            zero_mask = series == 0
            flat_mask = flat_mask & ~zero_mask

        # For GHI: only flag zero flatlines during expected daylight hours
        # Night window for zero-exemption is more generous than the nighttime-GHI check
        # (UTC 0-13 covers full night + dawn/dusk transition for Colorado)
        if col == "ghi_wm2":
            hour = df["timestamp_utc"].dt.hour
            night_or_twilight = (hour >= 0) & (hour <= 13)
            zero_at_night = (series == 0) & night_or_twilight
            flat_mask = flat_mask & ~zero_at_night

        flat_mask = flat_mask.fillna(False)

        for si, ei, cnt in _find_runs(df, flat_mask):
            val = series.iloc[si]
            dur_h = cnt * cfg.sampling_interval_min / 60
            ts = df["timestamp_utc"].iloc[si]
            te = df["timestamp_utc"].iloc[ei]

            # For RH=100: differentiate by duration
            if col == "rh_pct" and abs(val - 100.0) < 0.5:
                action = "exclude" if dur_h > 4.0 else "keep"
                note = (
                    f"RH stuck at {val:.0f} % for {dur_h:.1f} h "
                    f"({ts.strftime('%Y-%m-%d %H:%M')} to {te.strftime('%Y-%m-%d %H:%M')}); "
                    + ("persistent saturation >4 h is more consistent with sensor blockage than fog."
                       if dur_h > 4.0
                       else "short saturation consistent with fog or rain; flagged for awareness.")
                )
            else:
                action = "exclude"
                note = (
                    f"{col} unchanged at {val} for {dur_h:.1f} h "
                    f"({ts.strftime('%Y-%m-%d %H:%M')} to {te.strftime('%Y-%m-%d %H:%M')}); "
                    "indicates logger freeze, sensor failure, or stuck analogue output."
                )

            flags.append(_flag_from_run(
                df, si, ei, col,
                issue=f"Flatline: {col} = {val} for {dur_h:.1f} h ({cnt} records)",
                action=action,
                note=note,
            ))

    return flags


# ── Check 4: Pressure step changes ────────────────────────────────────────────

def check_pressure_steps(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """
    Flag abrupt pressure jumps exceeding cfg.pressure_max_step_hpa in one interval.

    Natural frontal passage: ~2-3 hPa/hour = ~0.3 hPa/10 min.
    A step > 2.5 hPa/10 min is physically implausible and suggests:
      - sensor replacement or logger restart
      - wiring fault
    """
    flags: List[FlagCandidate] = []
    if "pressure_hpa" not in df.columns:
        return flags

    p = df["pressure_hpa"]
    step = p.diff().abs()
    big_step = (step > cfg.pressure_max_step_hpa).fillna(False)

    # Merge adjacent steps into one flag
    for si, ei, cnt in _find_runs(df, big_step):
        delta = step.iloc[si:ei + 1].max()
        p_before = p.iloc[si - 1] if si > 0 else float("nan")
        p_after  = p.iloc[ei]
        ts_start = df["timestamp_utc"].iloc[si]
        ts_end   = df["timestamp_utc"].iloc[ei]
        flags.append({
            "channel":   "pressure_hpa",
            "start_utc": ts_start,
            "end_utc":   ts_end,
            "issue":     (
                f"Pressure step {p_before:.1f} to {p_after:.1f} hPa "
                f"({delta:.1f} hPa in {cnt * cfg.sampling_interval_min} min)"
            ),
            "action":    "correct",
            "note":      (
                f"Pressure changes by {delta:.1f} hPa in {cnt} intervals at "
                f"{ts_start.strftime('%Y-%m-%d %H:%M')}; natural front < 0.5 hPa/10 min. "
                "Check sensor replacement or logger restart. "
                "Correct by applying a constant offset to align with neighbouring intervals."
            ),
        })

    return flags


# ── Check 5: GHI nighttime non-zero ───────────────────────────────────────────

def check_ghi_night(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """
    Flag significant GHI values during hours when solar irradiance should be zero.
    For Colorado stations (UTC-7 MST): sunrise ~12-13 UTC, sunset ~01-02 UTC.
    Night = UTC 02 to 11 inclusive.
    """
    flags: List[FlagCandidate] = []
    if "ghi_wm2" not in df.columns:
        return flags

    hour = df["timestamp_utc"].dt.hour
    night = (hour >= cfg.ghi_night_start_utc) & (hour <= cfg.ghi_night_end_utc)
    bad   = night & (df["ghi_wm2"] > cfg.ghi_night_threshold)
    bad   = bad.fillna(False)

    for si, ei, cnt in _find_runs(df, bad):
        ts   = df["timestamp_utc"].iloc[si]
        te   = df["timestamp_utc"].iloc[ei]
        vmax = df["ghi_wm2"].iloc[si:ei + 1].max()
        # Only flag multi-record sustained events (not single-record instrument noise)
        if cnt < 3:
            continue
        flags.append(_flag_from_run(
            df, si, ei, "ghi_wm2",
            issue=(
                f"GHI {vmax:.0f} W/m2 during expected nighttime ({cnt} records)"
            ),
            action="exclude",
            note=(
                f"GHI up to {vmax:.0f} W/m2 recorded {ts.strftime('%H:%M')}-"
                f"{te.strftime('%H:%M')} UTC, corresponding to local nighttime; "
                "sensor offset or radiation-shield obstruction."
            ),
        ))

    return flags


# ── Check 6: RH above physical maximum ────────────────────────────────────────

def check_rh_bounds(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """
    Relative humidity > 101 % is physically impossible even for cheap sensors.
    Correct by capping to 100.0 %.
    """
    flags: List[FlagCandidate] = []
    if "rh_pct" not in df.columns:
        return flags

    over = (df["rh_pct"] > 101.0).fillna(False)

    for si, ei, cnt in _find_runs(df, over):
        ts   = df["timestamp_utc"].iloc[si]
        te   = df["timestamp_utc"].iloc[ei]
        vmax = df["rh_pct"].iloc[si:ei + 1].max()
        flags.append(_flag_from_run(
            df, si, ei, "rh_pct",
            issue=f"RH above 101 %: max {vmax:.0f} % ({cnt} records)",
            action="correct",
            note=(
                f"RH up to {vmax:.0f} % at {ts.strftime('%Y-%m-%d %H:%M')} to "
                f"{te.strftime('%Y-%m-%d %H:%M')}; cap to 100.0 % "
                "(sensor reads slightly above saturation due to calibration offset)."
            ),
        ))

    return flags


# ── Check 7: Cross-channel consistency ────────────────────────────────────────

def check_cross_channel(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """
    RH at saturation coinciding with high GHI suggests sensor blockage.
    Genuine fog/rain does not normally coexist with > 200 W/m2.
    """
    flags: List[FlagCandidate] = []

    if "rh_pct" not in df.columns or "ghi_wm2" not in df.columns:
        return flags

    rh_sat   = df["rh_pct"] >= 100.0
    ghi_high = df["ghi_wm2"] > 200.0
    suspect  = (rh_sat & ghi_high).fillna(False)

    for si, ei, cnt in _find_runs(df, suspect):
        # Only flag multi-record events to avoid single-record coincidences
        if cnt < 6:
            continue
        ts = df["timestamp_utc"].iloc[si]
        te = df["timestamp_utc"].iloc[ei]
        dur_h = cnt * cfg.sampling_interval_min / 60
        flags.append(_flag_from_run(
            df, si, ei, "rh_pct",
            issue=(
                f"RH at saturation with GHI > 200 W/m2 for {dur_h:.1f} h"
                " -- sensor blockage suspected"
            ),
            action="exclude",
            note=(
                f"RH >= 100 % and GHI > 200 W/m2 for {cnt} intervals "
                f"({ts.strftime('%Y-%m-%d %H:%M')} to {te.strftime('%Y-%m-%d %H:%M')}); "
                "sunny conditions are inconsistent with genuine fog; suspect blocked or wet sensor."
            ),
        ))

    return flags


# ── Run all checks for one station ───────────────────────────────────────────

def run_all_checks(
    station_id: str,
    df: pd.DataFrame,
    cfg: CheckerConfig,
) -> List[FlagCandidate]:
    """
    Execute all validation checks for one station DataFrame.
    Returns combined list of FlagCandidate dicts.
    """
    all_flags: List[FlagCandidate] = []

    checkers = [
        ("completeness",    check_completeness),
        ("physical bounds", check_physical_bounds),
        ("flatlines",       check_flatlines),
        ("pressure steps",  check_pressure_steps),
        ("GHI night",       check_ghi_night),
        ("RH bounds",       check_rh_bounds),
        ("cross-channel",   check_cross_channel),
    ]

    for name, fn in checkers:
        try:
            results = fn(df, cfg)
            logger.debug("%s: %s found %d candidates.", station_id, name, len(results))
            all_flags.extend(results)
        except Exception as exc:
            logger.error(
                "%s: check '%s' failed: %s", station_id, name, exc, exc_info=True
            )

    logger.info(
        "%s: total %d raw flag candidates before aggregation.",
        station_id, len(all_flags)
    )
    return all_flags
