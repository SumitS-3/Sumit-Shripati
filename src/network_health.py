"""
Network health analysis.

Computes per-station and per-channel quality summaries from flags.csv data.
Answers the two coordinator decisions:
  1. Which station data is suitable for the spring climate summary?
  2. Which single station should receive the technician visit?
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ChannelSummary:
    channel: str
    total_records: int
    excluded_records: int
    uncertain_records: int
    correctable_records: int

    @property
    def usable_records(self) -> int:
        return max(0, self.total_records - self.excluded_records)

    @property
    def usable_pct(self) -> float:
        if self.total_records == 0:
            return 0.0
        return 100.0 * self.usable_records / self.total_records

    @property
    def status(self) -> str:
        pct = self.usable_pct
        if pct >= 95.0:
            return "Good"
        elif pct >= 80.0:
            return "Usable (with caveats)"
        elif pct >= 50.0:
            return "Partial"
        else:
            return "Unreliable"


@dataclass
class StationSummary:
    station_id: str
    name: str
    total_records: int
    channels: Dict[str, ChannelSummary] = field(default_factory=dict)
    exclude_flags: List[dict] = field(default_factory=list)
    correct_flags: List[dict] = field(default_factory=list)
    keep_flags: List[dict] = field(default_factory=list)
    metadata_flags: List[dict] = field(default_factory=list)
    has_duplicate_timestamps: bool = False
    has_data_gap: bool = False
    maintenance_notes: List[str] = field(default_factory=list)

    @property
    def critical_issue_count(self) -> int:
        return len(self.exclude_flags)

    @property
    def overall_status(self) -> str:
        # Determine worst channel status
        if not self.channels:
            return "Unknown"
        statuses = [cs.status for cs in self.channels.values()]
        if "Unreliable" in statuses:
            return "Unreliable"
        if "Partial" in statuses:
            return "Partial"
        if "Usable (with caveats)" in statuses:
            return "Usable (with caveats)"
        return "Good"

    @property
    def climate_summary_recommendation(self) -> str:
        status = self.overall_status
        if status == "Good":
            return "Include"
        elif status == "Usable (with caveats)":
            return "Include with corrections noted"
        elif status == "Partial":
            return "Include usable channels; exclude flagged periods"
        else:
            return "Exclude or manual review required"


def compute_station_summaries(
    station_data: Dict[str, pd.DataFrame],
    flags_df: pd.DataFrame,
    stations_meta: pd.DataFrame,
) -> Dict[str, StationSummary]:
    """
    Build StationSummary for each station based on loaded data and flags.
    """
    summaries: Dict[str, StationSummary] = {}

    # Build meta lookup
    meta_lookup: Dict[str, str] = {}
    if not stations_meta.empty and "station_id" in stations_meta.columns:
        for _, row in stations_meta.iterrows():
            meta_lookup[row["station_id"]] = row.get("name", row["station_id"])

    # Channels that matter for climate summary
    CLIMATE_CHANNELS = ["temp_c", "rh_pct", "pressure_hpa", "wind_kmh", "rain_mm", "ghi_wm2"]

    for sid, df in station_data.items():
        name = meta_lookup.get(sid, sid)
        n_total = len(df)

        # Flags for this station
        if not flags_df.empty and "station_id" in flags_df.columns:
            st_flags = flags_df[flags_df["station_id"] == sid].to_dict("records")
        else:
            st_flags = []

        # Discarded GPS packets are not discarded weather observations or evidence
        # of a repairable weather-sensor fault.
        metadata_flags = [f for f in st_flags if f.get("channel") == "metadata"]
        weather_flags = [f for f in st_flags if f.get("channel") != "metadata"]
        exclude_flags = [f for f in weather_flags if f.get("action") == "exclude"]
        correct_flags = [f for f in weather_flags if f.get("action") == "correct"]
        keep_flags    = [f for f in weather_flags if f.get("action") == "keep"]
        has_gap       = any("gap" in f.get("issue", "").lower() for f in exclude_flags)
        has_dup       = any(any(word in f.get("issue", "").lower() for word in ("duplicate", "replay"))
                            for f in (exclude_flags + correct_flags))

        # Per-channel summaries
        channel_sums: Dict[str, ChannelSummary] = {}
        for col in CLIMATE_CHANNELS:
            if col not in df.columns:
                continue
            col_flags = [f for f in st_flags if f.get("channel") in (col, "all")]
            excluded = 0
            uncertain = 0
            correctable = 0
            for f in col_flags:
                # Estimate records covered by this flag
                try:
                    t0 = pd.Timestamp(f.get("start_utc", ""))
                    t1 = pd.Timestamp(f.get("end_utc", "") or f.get("start_utc", ""))
                    covered = max(1, int((t1 - t0).total_seconds() / 600) + 1)
                except Exception:
                    covered = 1
                if f.get("action") == "exclude":
                    excluded += covered
                elif f.get("action") == "correct":
                    correctable += covered
                elif f.get("action") == "keep":
                    uncertain += covered
            excluded = min(excluded, n_total)
            channel_sums[col] = ChannelSummary(
                channel=col,
                total_records=n_total,
                excluded_records=excluded,
                uncertain_records=uncertain,
                correctable_records=correctable,
            )

        summaries[sid] = StationSummary(
            station_id=sid,
            name=name,
            total_records=n_total,
            channels=channel_sums,
            exclude_flags=exclude_flags,
            correct_flags=correct_flags,
            keep_flags=keep_flags,
            metadata_flags=metadata_flags,
            has_duplicate_timestamps=has_dup,
            has_data_gap=has_gap,
        )

    return summaries


def recommend_technician_visit(
    summaries: Dict[str, StationSummary],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Select the station that would benefit most from a 1-hour technician visit.

    Prioritises:
      - Hardware failures that cannot be corrected remotely
      - Persistent multi-channel failures
      - Issues with on-site-fixable root causes
      - High evidence of sensor malfunction (not just data gaps)

    Returns (best_station_id, runner_up_station_id).
    """
    # Score each station by problem severity
    scores: Dict[str, float] = {}
    for sid, summary in summaries.items():
        score = 0.0
        # Severe problems: many excluded flags
        score += len(summary.exclude_flags) * 2.0
        # Correction-needed flags (less severe)
        score += len(summary.correct_flags) * 1.0
        # Data gap penalty
        if summary.has_data_gap:
            score += 5.0
        # Duplicate-timestamp penalty
        if summary.has_duplicate_timestamps:
            score += 3.0
        # Multi-channel failures (exponential penalty)
        unreliable_channels = sum(
            1 for cs in summary.channels.values() if cs.status in ("Unreliable", "Partial")
        )
        score += unreliable_channels ** 2
        scores[sid] = score

    ranked = sorted(scores, key=lambda s: scores[s], reverse=True)
    best    = ranked[0] if len(ranked) >= 1 else None
    runner  = ranked[1] if len(ranked) >= 2 else None
    return best, runner

