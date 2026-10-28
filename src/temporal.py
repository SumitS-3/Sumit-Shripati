"""Resolve time before interpreting sensor values; never modify the source files.

Exact replays can be removed without choosing between different measurements.
Clock changes require repeated full-date GPS evidence and a unique matching break
in source order. Conflicting duplicates are withheld rather than guessed away.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from typing import Callable

import numpy as np
import pandas as pd

from .config import CheckerConfig
from .gps import ClockSample
from .intervals import find_runs
from .schema import NUMERIC_COLUMNS, TS_FMT


@dataclass(frozen=True)
class ClockRegime:
    samples: tuple[ClockSample, ...]

    @property
    def offset(self) -> int | None:
        return self.samples[0].correction_seconds


def _incident_flags(frame: pd.DataFrame, cfg: CheckerConfig, issue: str,
                    action: str, evidence: Callable[[int, int], str]) -> list[dict]:
    """Summarize selected source rows into genuinely contiguous timestamp incidents."""
    if frame.empty:
        return []
    counts = frame.groupby("timestamp_utc", sort=True).size().rename("count").reset_index()
    flags = []
    for start, end, _ in find_runs(counts, pd.Series(True, index=counts.index), cfg.sampling_interval_min):
        rows = counts.iloc[start:end + 1]
        flags.append({
            "channel": "all", "start_utc": rows.timestamp_utc.iloc[0],
            "end_utc": rows.timestamp_utc.iloc[-1], "issue": issue, "action": action,
            "note": evidence(int(rows["count"].sum()), len(rows)),
        })
    return flags


def _clock_offsets(frame: pd.DataFrame, samples: list[ClockSample], cfg: CheckerConfig) -> tuple[np.ndarray, list[dict]]:
    """Return an offset per row, or fail when a proposed correction is ambiguous."""
    offsets = np.zeros(len(frame), dtype=np.int64)
    if not samples or all(sample.correction_seconds == 0 for sample in samples):
        return offsets, []
    regimes = [ClockRegime(tuple(group)) for _, group in groupby(samples, key=lambda sample: sample.correction_seconds)]
    max_gap = pd.Timedelta(hours=cfg.gps_clock_max_gap_hours)
    cadence = pd.Timedelta(minutes=cfg.sampling_interval_min)
    for regime in regimes:
        if regime.offset is None or len(regime.samples) < cfg.gps_clock_min_fixes:
            raise ValueError("offset is drifting or has too few independent RMC fixes")
        times = pd.Series([sample.gps_utc for sample in regime.samples])
        gaps = times.diff().dropna()
        if (gaps <= pd.Timedelta(0)).any() or (gaps > max_gap).any():
            raise ValueError("GPS evidence is repeated, out of order, or too widely separated")

    timestamps = frame.timestamp_utc
    differences = timestamps.diff()
    boundaries = [0]
    for before, after in zip(regimes, regimes[1:]):
        # An observation break must exactly compensate the independently observed
        # GPS offset change, and fall inside the GPS transition bracket.
        adjustment = pd.Timedelta(seconds=after.offset - before.offset)
        corrected = timestamps + pd.Timedelta(seconds=after.offset)
        matches = (differences + adjustment).eq(cadence)
        matches &= corrected.gt(before.samples[-1].gps_utc)
        matches &= corrected.le(after.samples[0].gps_utc)
        matches &= frame.index > boundaries[-1]
        locations = frame.index[matches].tolist()
        if len(locations) != 1:
            raise ValueError("GPS offset change has no unique matching observation break")
        if after.samples[0].gps_utc - before.samples[-1].gps_utc > max_gap:
            raise ValueError("GPS transition bracket is too wide")
        boundaries.append(locations[0])
    boundaries.append(len(frame))

    flags = []
    for regime, start, end in zip(regimes, boundaries, boundaries[1:]):
        offsets[start:end] = regime.offset
        if regime.offset == 0:
            continue
        block = frame.iloc[start:end]
        if block.empty:
            raise ValueError("GPS clock regime has no matching observations")
        corrected = block.timestamp_utc + pd.Timedelta(seconds=regime.offset)
        if corrected.min() < regime.samples[0].gps_utc - max_gap or corrected.max() > regime.samples[-1].gps_utc + max_gap:
            raise ValueError("correction would extrapolate beyond GPS coverage")
        first_row, last_row = int(block.source_row.iloc[0]), int(block.source_row.iloc[-1])
        flags.append({
            "channel": "all", "start_utc": block.timestamp_utc.min(),
            "end_utc": block.timestamp_utc.max(), "issue": "GPS-confirmed logger clock offset",
            "action": "correct",
            "note": (
                f"For source CSV rows {first_row}-{last_row} (header is row 1), add {regime.offset / 60:+g} minutes to logger timestamps before sorting; "
                f"{len(regime.samples)} valid RMC fixes and the source sequence support corrected UTC {corrected.min().strftime(TS_FMT)} to {corrected.max().strftime(TS_FMT)}."
            ),
        })
    return offsets, flags


def prepare_observations(frame: pd.DataFrame, samples: list[ClockSample],
                         cfg: CheckerConfig) -> tuple[pd.DataFrame, list[dict]]:
    """Return sorted, unique observations and auditable flags for every time treatment."""
    frame = frame.copy().reset_index(drop=True)
    if "source_row" not in frame:
        frame["source_row"] = frame.index + 2
    frame["logger_timestamp_utc"] = frame.timestamp_utc
    channels = [column for column in NUMERIC_COLUMNS if column in frame]
    flags = []

    try:
        offsets, clock_flags = _clock_offsets(frame, samples, cfg)
    except ValueError as exc:
        flags.append({
            "channel": "all", "start_utc": frame.timestamp_utc.min(),
            "end_utc": frame.timestamp_utc.max(), "issue": "Unresolved logger clock evidence",
            "action": "exclude",
            "note": f"Withhold this station's timed observations pending review because {exc}; no timestamp correction was applied.",
        })
        return frame.iloc[:0], flags
    flags.extend(clock_flags)
    frame["timestamp_utc"] += pd.to_timedelta(offsets, unit="s")
    # A rollback can legitimately produce identical values at two different true
    # times, so replay removal must follow any independently supported clock repair.
    repeated = frame.duplicated(["timestamp_utc", *channels], keep="first")
    flags.extend(_incident_flags(
        frame.loc[repeated], cfg, "Exact observation replay", "correct",
        lambda count, times:
            f"Remove {count} replayed rows at {times} timestamps by retaining the first source occurrence of each identical timestamp and all recorded sensor values."
    ))
    frame = frame.loc[~repeated].reset_index(drop=True)
    frame = frame.sort_values("timestamp_utc", kind="stable").reset_index(drop=True)

    conflicts = frame.duplicated("timestamp_utc", keep=False)
    flags.extend(_incident_flags(
        frame.loc[conflicts], cfg, "Conflicting duplicate timestamps", "exclude",
        lambda count, times:
            f"Withhold all {count} different observations at {times} shared timestamps; neither sensor plausibility nor sorting identifies the original measurement."
    ))
    frame = frame.loc[~conflicts].reset_index(drop=True)
    frame["dup_ts"] = False
    return frame, flags
