"""Coverage after treatment and prospective visit triage, independent of flag count."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import CheckerConfig
from .evidence import interval_mask
from .findings import PROFILES, FindingProfile, finding_kind
from .schema import NUMERIC_COLUMNS


@dataclass(frozen=True)
class ChannelSummary:
    channel: str
    total_records: int  # expected slots, including absent timestamps
    observed_records: int
    missing_records: int
    excluded_records: int
    pending_records: int
    usable_records: int
    corrected_records: int

    @property
    def usable_pct(self) -> float:
        return 100 * self.usable_records / self.total_records if self.total_records else 0.0


@dataclass(frozen=True)
class Finding:
    kind: str
    channel: str
    start: pd.Timestamp
    end: pd.Timestamp
    flags: tuple[dict, ...]
    affected_records: int

    @property
    def profile(self) -> FindingProfile:
        return PROFILES.get(self.kind, PROFILES["unknown"])


@dataclass(frozen=True)
class ClockAdjustment:
    minutes: float
    records: int


@dataclass
class StationSummary:
    station_id: str
    name: str
    total_records: int = 0  # unique observations retained by the temporal pipeline
    expected_records: int = 0
    interval_min: int = 10
    period_start: pd.Timestamp | None = None
    period_end: pd.Timestamp | None = None
    channels: dict[str, ChannelSummary] = field(default_factory=dict)
    exclude_flags: list[dict] = field(default_factory=list)
    correct_flags: list[dict] = field(default_factory=list)
    keep_flags: list[dict] = field(default_factory=list)
    metadata_flags: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    clock_adjustments: list[ClockAdjustment] = field(default_factory=list)
    replayed_records: int = 0
    has_missing_tail: bool = False
    analysis_failed: bool = False

    @property
    def overall_status(self) -> str:
        if self.analysis_failed:
            return "Hold"
        if not self.expected_records:
            return "Not assessed"
        if any(channel.pending_records for channel in self.channels.values()):
            return "Treatment pending"
        if any(channel.missing_records or channel.excluded_records for channel in self.channels.values()):
            return "Use selected data"
        return "Good"


def _expected_grid(start: pd.Timestamp | None, end: pd.Timestamp | None,
                   frame: pd.DataFrame, cadence: int, installed) -> pd.DatetimeIndex:
    if start is None or end is None:
        return pd.DatetimeIndex([])
    installation = pd.to_datetime(installed, errors="coerce")
    if pd.notna(installation):
        start = max(start, installation)
    if start > end:
        return pd.DatetimeIndex([])
    step = pd.Timedelta(minutes=cadence).value
    phase = pd.Timestamp(frame.timestamp_utc.iloc[0]).value % step if not frame.empty else start.value % step
    first = ((start.value - phase + step - 1) // step) * step + phase
    return pd.date_range(pd.Timestamp(first), end, freq=pd.Timedelta(minutes=cadence))


def _group_findings(flags: list[dict], grid: pd.Series) -> list[Finding]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for flag in flags:
        if flag.get("action") != "exclude" or flag.get("channel") == "metadata":
            continue
        groups.setdefault((finding_kind(flag.get("issue", "")), flag["channel"]), []).append(flag)
    findings = []
    for (kind, channel), members in groups.items():
        starts = [pd.Timestamp(flag.get("start_utc")) for flag in members]
        ends = [pd.Timestamp(flag.get("end_utc")) for flag in members]
        starts, ends = [time for time in starts if pd.notna(time)], [time for time in ends if pd.notna(time)]
        if starts and ends:
            findings.append(Finding(kind, channel, min(starts), max(ends), tuple(members),
                                    int(interval_mask(grid, members, channel).sum())))
    return findings


def compute_station_summaries(station_data: dict[str, pd.DataFrame], flags_df: pd.DataFrame,
                               stations_meta: pd.DataFrame,
                               configs: dict[str, CheckerConfig] | None = None) -> dict[str, StationSummary]:
    """Reindex one station at a time on its cadence within the common network window.

    Missing slots, excluded observations and pending treatments are disjoint. Keep
    flags are not uncertain data. Clock/replay treatments count as applied only when
    the temporal pipeline records that fact; CSV labels alone do not prove repair.
    """
    metadata = {str(row["station_id"]): row.to_dict() for _, row in stations_meta.iterrows()} if "station_id" in stations_meta else {}
    grouped = {str(sid): group.to_dict("records") for sid, group in flags_df.groupby("station_id")} if "station_id" in flags_df else {}
    available = [frame for frame in station_data.values() if not frame.empty]
    start = min(frame.timestamp_utc.min() for frame in available) if available else None
    end = max(frame.timestamp_utc.max() for frame in available) if available else None
    result = {}
    for sid in sorted(set(station_data) | set(metadata) | set(grouped)):
        meta, flags = metadata.get(sid, {}), grouped.get(sid, [])
        frame = station_data.get(sid, pd.DataFrame(columns=["timestamp_utc", *NUMERIC_COLUMNS]))
        failed = sid not in station_data or any(finding_kind(flag.get("issue", "")) in ("failed", "unresolved") for flag in flags)
        if frame.attrs.get("temporal_unresolved"):
            failed = True
        try:
            cadence = configs[sid].sampling_interval_min if configs and sid in configs else int(meta.get("logger_interval_min", 10))
            if cadence <= 0:
                raise ValueError("invalid cadence")
        except (TypeError, ValueError):
            cadence, failed = 10, True
        grid = _expected_grid(start, end, frame, cadence, meta.get("installed"))
        grid_series = pd.Series(grid)
        indexed = frame.set_index("timestamp_utc")
        if not indexed.index.is_unique:
            raise ValueError(f"{sid}: summaries require deduplicated observation timestamps")
        aligned = indexed.reindex(grid)
        present = pd.Series(grid.isin(indexed.index))
        weather = [flag for flag in flags if flag.get("channel") != "metadata"]
        applied = set(frame.attrs.get("treatments_applied", ()))
        pending = [flag for flag in weather if flag.get("action") == "correct" and
                   finding_kind(flag.get("issue", "")) not in applied]
        delta = ((aligned.index.to_series(index=aligned.index) - aligned.logger_timestamp_utc).dt.total_seconds() / 60
                 if "logger_timestamp_utc" in aligned else pd.Series(0.0, index=grid))
        shifted = pd.Series(delta.fillna(0).ne(0).to_numpy())
        channels = {}
        for channel in NUMERIC_COLUMNS:
            values = aligned[channel] if channel in aligned else pd.Series(float("nan"), index=grid)
            finite = pd.Series(np.isfinite(pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)))
            excluded = present & (interval_mask(grid_series, weather, channel) | ~finite | failed)
            treatment = present & ~excluded & interval_mask(grid_series, pending, channel, ("correct",))
            usable = present & ~excluded & ~treatment
            channels[channel] = ChannelSummary(channel, len(grid), int(present.sum()), int((~present).sum()),
                int(excluded.sum()), int(treatment.sum()), int(usable.sum()), int((usable & shifted).sum()))
        adjustments = [ClockAdjustment(float(minutes), int(count)) for minutes, count in delta.dropna().loc[lambda series: series.ne(0)].value_counts().sort_index().items()]
        name = meta.get("name", sid)
        summary = StationSummary(
            station_id=sid, name=str(name) if pd.notna(name) else sid,
            total_records=len(frame), expected_records=len(grid), interval_min=cadence,
            period_start=start, period_end=end, channels=channels,
            exclude_flags=[flag for flag in weather if flag.get("action") == "exclude"],
            correct_flags=[flag for flag in weather if flag.get("action") == "correct"],
            keep_flags=[flag for flag in weather if flag.get("action") == "keep"],
            metadata_flags=[flag for flag in flags if flag.get("channel") == "metadata"],
            findings=_group_findings(weather, grid_series), clock_adjustments=adjustments,
            replayed_records=int(frame.attrs.get("replayed_records", 0)),
            has_missing_tail=bool(not frame.empty and len(grid) and frame.timestamp_utc.max() < grid[-1]),
            analysis_failed=failed,
        )
        result[sid] = summary
    return result


@dataclass(frozen=True)
class VisitCandidate:
    station_id: str
    finding: Finding

    @property
    def profile(self) -> FindingProfile:
        return self.finding.profile


def visit_candidate(summary: StationSummary) -> VisitCandidate | None:
    """Recent recurring daylight faults remain live even when the pack ends at night."""
    if summary.analysis_failed or summary.period_end is None:
        return None
    active = [finding for finding in summary.findings if finding.affected_records > 0
              and finding.start <= summary.period_end and finding.end >= summary.period_end - pd.Timedelta(days=1)
              and finding.kind not in ("gap", "conflicts", "unresolved")]
    if summary.has_missing_tail:
        active.append(Finding("gap", "all", summary.period_end, summary.period_end, (), 0))
    if not active:
        return None
    best = max(active, key=lambda finding: (finding.profile.priority, finding.kind, finding.channel))
    return VisitCandidate(summary.station_id, best)


def recommend_technician_visit(summaries: dict[str, StationSummary]) -> tuple[str | None, str | None]:
    """Rank unresolved, on-site-checkable problems; splitting flags cannot raise rank."""
    candidates = [candidate for summary in summaries.values() if (candidate := visit_candidate(summary)) is not None]
    ranked = sorted(candidates, key=lambda candidate: (-candidate.profile.priority, candidate.station_id))
    first = ranked[0].station_id if ranked else None
    second = ranked[1].station_id if len(ranked) > 1 else None
    return first, second
