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
from typing import Any, Dict, List

import pandas as pd

from .config import CheckerConfig
from .intervals import find_runs as _find_runs
from .schema import NUMERIC_COLUMNS
from .solar import interval_sun_masks

logger = logging.getLogger(__name__)

FlagCandidate = Dict[str, Any]


# ── Helper: run-length encoding ───────────────────────────────────────────────

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
      - NaN values in numeric columns

    Clock changes and duplicates are resolved by temporal.prepare_observations
    before these checks; this function never chooses between source rows.
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
            "start_utc": t_before + pd.Timedelta(seconds=interval_s),
            "end_utc":   t_after - pd.Timedelta(seconds=interval_s),
            "issue":     f"Data gap: {gap_min} min ({n_missing} missing intervals)",
            "action":    "exclude",
            "note":      (
                f"{gap_min}-minute gap in observations "
                f"({t_before.strftime('%Y-%m-%d %H:%M')} "
                f"to {t_after.strftime('%Y-%m-%d %H:%M')}); "
                f"{n_missing} expected records absent."
            ),
        })

    # ── Missing values ─────────────────────────────────────────────────────
    for col in NUMERIC_COLUMNS:
        if col not in df.columns:
            continue
        for si, ei, nan_count in _find_runs(df, df[col].isna(), cfg.sampling_interval_min):
            t0 = df["timestamp_utc"].iloc[si]
            t1 = df["timestamp_utc"].iloc[ei]
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
        # RH upper screening is emitted once by check_rh_bounds.
        bad_mask = (v < lo) | ((v > hi) if col != "rh_pct" else False)
        bad_mask = bad_mask.fillna(False)

        for si, ei, cnt in _find_runs(df, bad_mask, cfg.sampling_interval_min):
            val_min = v.iloc[si:ei + 1].min()
            val_max = v.iloc[si:ei + 1].max()
            flags.append(_flag_from_run(
                df, si, ei, col,
                issue=f"Out-of-range {col}: [{val_min:.2f}, {val_max:.2f}]",
                action="exclude",
                note=(
                    f"{cnt} record(s) in {col} outside physical bounds "
                    f"[{lo}, {hi}]; values [{val_min:.2f}, {val_max:.2f}] "
                    "are withheld pending verification."
                ),
            ))

    return flags


# ── Check 3: Flatlines (stuck sensor / logger freeze) ────────────────────────

def check_flatlines(df: pd.DataFrame, cfg: CheckerConfig,
                   daylight: pd.Series | None = None) -> List[FlagCandidate]:
    """
    Detect runs of identical values exceeding the configured minimum run length.

    Special cases:
      - wind_kmh=0 is exempted (genuine calm spells).
      - ghi_wm2=0 may be night or cloud and is not diagnosed from duration alone.
      - RH saturation requires network weather evidence, regardless of duration.
    """
    flags: List[FlagCandidate] = []

    for col, min_run in cfg.flatline_min_records.items():
        if col not in df.columns or col == "rh_pct":
            continue
        series = df[col]

        # Group consecutive identical values
        elapsed = df["timestamp_utc"].diff()
        time_break = (elapsed > pd.Timedelta(minutes=cfg.sampling_interval_min)) | (elapsed <= pd.Timedelta(0))
        group_ids = ((series != series.shift()) | time_break).cumsum()
        run_lens  = series.groupby(group_ids).transform("count")

        # Build mask of long runs
        flat_mask = run_lens >= min_run

        # Exempt channels where zero is physically meaningful
        if col in cfg.flatline_exempt_zeros:
            zero_mask = series == 0
            flat_mask = flat_mask & ~zero_mask

        if col == "ghi_wm2":
            # Zero can be night or heavy cloud; it requires peer evidence, not a
            # fixed UTC schedule. Nonzero freezes are screened in geometric daylight.
            flat_mask &= series.gt(0) & (daylight if daylight is not None else False)

        flat_mask = flat_mask.fillna(False)

        # Split at value changes too: adjacent constant runs are distinct evidence.
        for si, ei, cnt in _find_runs(df, flat_mask, cfg.sampling_interval_min,
                                     break_before=group_ids.ne(group_ids.shift())):
            val = series.iloc[si]
            dur_h = cnt * cfg.sampling_interval_min / 60
            ts = df["timestamp_utc"].iloc[si]
            te = df["timestamp_utc"].iloc[ei]

            action = "exclude"
            note = (f"{col} repeats exactly {val:g} for {cnt} consecutive records over {dur_h:.1f} h; "
                    "withhold the run as a possible stuck sensor or logger output, pending verification.")

            flags.append(_flag_from_run(
                df, si, ei, col,
                issue=f"Flatline: {col} = {val} for {dur_h:.1f} h ({cnt} records)",
                action=action,
                note=note,
            ))

    return flags


# ── Check 4: Pressure step changes ────────────────────────────────────────────

def check_pressure_steps(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """Distinguish isolated spikes from baseline changes; never invent a calibration."""
    flags: List[FlagCandidate] = []
    if "pressure_hpa" not in df.columns:
        return flags

    p = df["pressure_hpa"]
    delta = p.diff()
    regular = df.timestamp_utc.diff().eq(pd.Timedelta(minutes=cfg.sampling_interval_min))
    limit = cfg.pressure_max_step_hpa * cfg.sampling_interval_min / 10
    big = delta.abs().gt(limit) & regular
    isolated = (big & big.shift(-1, fill_value=False) & delta.mul(delta.shift(-1)).lt(0)
                & p.shift(-1).sub(p.shift(1)).abs().lt(limit / 2))
    for index in df.index[isolated]:
        flags.append(_flag_from_run(df, index, index, "pressure_hpa", "Pressure isolated spike", "exclude",
                     f"Pressure jumps by {delta.iloc[index]:+g} hPa then returns within {limit / 2:g} hPa of the preceding value at the next sample; withhold only the isolated reading."))
    transitions = df.index[big & ~isolated & ~isolated.shift(1, fill_value=False)].tolist()
    for position, index in enumerate(transitions):
        end = transitions[position + 1] - 1 if position + 1 < len(transitions) else len(df) - 1
        flags.append(_flag_from_run(df, index, end, "pressure_hpa", "Pressure step; calibration unresolved", "exclude",
                     f"Pressure changes by {delta.iloc[index]:+g} hPa in {cfg.sampling_interval_min} minutes; withhold the following regime until independent evidence establishes its calibration, rather than imposing an offset."))

    return flags


# ── Check 5: GHI nighttime non-zero ───────────────────────────────────────────

def check_ghi_night(df: pd.DataFrame, cfg: CheckerConfig,
                    night: pd.Series | None = None) -> List[FlagCandidate]:
    """
    Flag significant GHI values during hours when solar irradiance should be zero.
    The caller supplies a whole-interval geometric night mask; skip if location is unknown.
    """
    flags: List[FlagCandidate] = []
    if "ghi_wm2" not in df.columns or night is None:
        return flags

    bad   = night & (df["ghi_wm2"] > cfg.ghi_night_threshold)
    bad   = bad.fillna(False)

    for si, ei, cnt in _find_runs(df, bad, cfg.sampling_interval_min):
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
                f"{te.strftime('%H:%M')} UTC while the sun remains below {cfg.solar_night_elevation_deg:g} degrees across each logging interval; withhold irradiance and inspect the channel or clock."
            ),
        ))

    return flags


# ── Check 6: RH above physical maximum ────────────────────────────────────────

def check_rh_bounds(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """
    Withhold RH above the screening tolerance; capping is not a calibration.
    """
    flags: List[FlagCandidate] = []
    if "rh_pct" not in df.columns:
        return flags

    maximum = cfg.physical_bounds["rh_pct"]["max"]
    over = (df["rh_pct"] > maximum).fillna(False)

    for si, ei, cnt in _find_runs(df, over, cfg.sampling_interval_min):
        ts   = df["timestamp_utc"].iloc[si]
        te   = df["timestamp_utc"].iloc[ei]
        vmax = df["rh_pct"].iloc[si:ei + 1].max()
        flags.append(_flag_from_run(
            df, si, ei, "rh_pct",
            issue=f"RH above {maximum:g} %: max {vmax:g} % ({cnt} records)",
            action="exclude",
            note=(
                f"RH up to {vmax:.0f} % at {ts.strftime('%Y-%m-%d %H:%M')} to "
                f"{te.strftime('%Y-%m-%d %H:%M')}; withhold these values because no reference calibration supports an exact correction."
            ),
        ))

    return flags


# ── Check 7: Cross-channel consistency ────────────────────────────────────────

def check_cross_channel(df: pd.DataFrame, cfg: CheckerConfig) -> List[FlagCandidate]:
    """Retain rapid warming when drying and wind provide independent weather context."""
    flags: List[FlagCandidate] = []

    if any(column not in df for column in ("temp_c", "rh_pct", "wind_kmh")):
        return flags
    indexed = df.set_index("timestamp_utc")
    prior = indexed.reindex(indexed.index - pd.Timedelta(hours=1))
    warming = df.temp_c - prior.temp_c.to_numpy()
    drying = df.rh_pct - prior.rh_pct.to_numpy()
    coherent = (warming.ge(cfg.rapid_warming_c) & drying.le(-cfg.rapid_rh_drop_pct)
                & df.wind_kmh.ge(cfg.warm_wind_min_kmh))
    for si, ei, cnt in _find_runs(df, coherent, cfg.sampling_interval_min):
        flags.append(_flag_from_run(
            df, si, ei, "temp_c", "Rapid warming supported by drying wind", "keep",
            f"Within an hour temperature rises by up to {warming.iloc[si:ei+1].max():.1f} C while RH falls by at least {abs(drying.iloc[si:ei+1].max()):.0f} points and wind exceeds {cfg.warm_wind_min_kmh:g} km/h; retain this coherent weather signal without claiming a specific mechanism."
        ))

    return flags


# ── Run all checks for one station ───────────────────────────────────────────

def run_all_checks(
    station_id: str,
    df: pd.DataFrame,
    cfg: CheckerConfig,
    latitude: float | None = None,
    longitude: float | None = None,
) -> List[FlagCandidate]:
    """
    Execute all validation checks for one station DataFrame.
    Returns combined list of FlagCandidate dicts.
    """
    if df.empty:
        return []
    night, daylight = interval_sun_masks(df.timestamp_utc, latitude, longitude, cfg.sampling_interval_min,
                                        cfg.solar_night_elevation_deg, cfg.solar_day_elevation_deg)
    all_flags: List[FlagCandidate] = []

    checkers = [
        ("completeness", check_completeness),
        ("bounds", check_physical_bounds),
        ("flatline", lambda frame, config: check_flatlines(frame, config, daylight)),
        ("pressure", check_pressure_steps),
        ("night", lambda frame, config: check_ghi_night(frame, config, night)),
        ("humidity", check_rh_bounds),
        ("weather", check_cross_channel),
    ]

    for name, fn in checkers:
        try:
            results = fn(df, cfg)
            for candidate in results:
                candidate["check"] = name
            logger.debug("%s: %s found %d candidates.", station_id, name, len(results))
            all_flags.extend(results)
        except Exception as exc:
            logger.error(
                "%s: check '%s' failed: %s", station_id, name, exc, exc_info=True
            )
            raise

    logger.info(
        "%s: total %d raw flag candidates before aggregation.",
        station_id, len(all_flags)
    )
    return all_flags
