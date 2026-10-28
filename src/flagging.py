"""
Evidence-based flag classification and interval aggregation.

Responsibilities:
  - Merge consecutive flag candidates for the same (station, channel, issue type)
  - Apply maintenance-note context to refine action classification
  - Produce final flags.csv rows in the required schema
  - Output is deterministic (sorted by station_id, channel, start_utc)

Output schema:
    station_id, channel, start_utc, end_utc, issue, action, note
    Timestamps: "YYYY-MM-DD HH:MM"
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pandas as pd

from .config import CheckerConfig
from .maintenance import MaintenanceEvent, find_nearby_events
from .schema import FLAG_COLUMNS, TS_FMT
from .output import atomic_text

logger = logging.getLogger(__name__)


# ── CSV formula injection guard ───────────────────────────────────────────────

_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

def _sanitise_cell(value: str) -> str:
    """Prefix a single-quote if the cell could be interpreted as a spreadsheet formula."""
    s = str(value).strip()
    if s and s[0] in _INJECTION_PREFIXES:
        s = "'" + s
    return s


def _fmt_ts(ts: Any) -> str:
    """Format a timestamp to the required string format. Empty string if not a timestamp."""
    if isinstance(ts, str):
        return ts[:16]  # already formatted or empty
    if pd.isna(ts) or ts is None:
        return ""
    try:
        return pd.Timestamp(ts).strftime(TS_FMT)
    except Exception:
        return str(ts)[:16]


# ── Interval merging ──────────────────────────────────────────────────────────

def _should_merge(a: Dict, b: Dict, merge_gap_min: int) -> bool:
    """Return True if two flag candidates should be merged into one interval."""
    # Similar wording is not evidence that two problems have the same remedy.
    # Checks already emit complete runs, so merge only matching descriptions.
    if any(a.get(key) != b.get(key) for key in ("channel", "issue", "action")):
        return False
    # Gap between end of a and start of b
    try:
        t_end   = pd.Timestamp(a["end_utc"])
        t_start = pd.Timestamp(b["start_utc"])
        if pd.isna(t_end) or pd.isna(t_start):
            return False
        gap_min = (t_start - t_end).total_seconds() / 60
        return gap_min <= merge_gap_min
    except Exception:
        return False


def aggregate_flags(
    candidates: List[Dict],
    cfg: CheckerConfig,
) -> List[Dict]:
    """
    Merge overlapping or adjacent candidates with matching channel, issue and action.
    Never bridge more than one sample interval: intervening readings may be good.
    Returns sorted list of merged flag dicts.
    """
    if not candidates:
        return []

    def timestamp_key(value: Any) -> pd.Timestamp:
        return pd.Timestamp.min if value is None or pd.isna(value) else pd.Timestamp(value)

    sorted_cands = sorted(candidates, key=lambda f: (
        f.get("channel", ""),
        f.get("issue", ""),
        f.get("action", ""),
        timestamp_key(f.get("start_utc")),
        timestamp_key(f.get("end_utc")),
        f.get("note", ""),
    ))

    merged: List[Dict] = []
    current = dict(sorted_cands[0])

    for next_flag in sorted_cands[1:]:
        if _should_merge(current, next_flag, min(cfg.merge_gap_min, cfg.sampling_interval_min)):
            # Nested intervals must not shorten a previously detected problem.
            current["end_utc"] = max(pd.Timestamp(current["end_utc"]), pd.Timestamp(next_flag["end_utc"]))
            if next_flag.get("note") and next_flag["note"] != current.get("note"):
                current["note"] = current.get("note", "").rstrip(".") + "; " + next_flag["note"]
        else:
            merged.append(current)
            current = dict(next_flag)

    merged.append(current)
    return sorted(merged, key=lambda f: (
        f.get("channel", ""), timestamp_key(f.get("start_utc")),
        f.get("issue", ""), f.get("action", ""),
    ))


# ── Maintenance enrichment ────────────────────────────────────────────────────

def enrich_with_maintenance(
    flags: List[Dict],
    station_id: str,
    maintenance_events: Dict[str, List[MaintenanceEvent]],
    cfg: CheckerConfig,
) -> List[Dict]:
    """
    For each flag, look up nearby maintenance events and append context to the note.
    Does NOT change the action automatically — that requires human judgement.
    """
    enriched: List[Dict] = []
    for flag in flags:
        ts = flag.get("start_utc")
        if ts and not isinstance(ts, str) and flag.get("channel") != "metadata":
            flag_date = pd.Timestamp(ts).date()
            nearby = find_nearby_events(maintenance_events, station_id, flag_date, window_days=2)
            nearby = [event for event in nearby if (
                flag.get("channel") == "all" and event.is_data_interruption
            ) or event.mentions_channel(flag.get("channel", ""))]
            if nearby:
                # Preserve a concise provenance reference, not a truncated quote
                # that can omit the important part of the volunteer's evidence.
                maint_text = ", ".join(str(event.date) for event in nearby)
                flag = dict(flag)
                flag["note"] = flag["note"].rstrip(".") + f"; related maintenance record: {maint_text}."
        enriched.append(flag)
    return enriched


# ── Build final flag rows ─────────────────────────────────────────────────────

def build_flag_rows(
    station_id: str,
    candidates: List[Dict],
    maintenance_events: Dict[str, List[MaintenanceEvent]],
    cfg: CheckerConfig,
) -> List[Dict]:
    """
    Full pipeline: aggregate → enrich → format.
    Returns list of row dicts ready for flags.csv.
    """
    if not candidates:
        return []

    # 1. Aggregate intervals
    merged = aggregate_flags(candidates, cfg)

    # 2. Enrich with maintenance context
    merged = enrich_with_maintenance(merged, station_id, maintenance_events, cfg)

    # 3. Format output rows
    rows: List[Dict] = []
    for flag in merged:
        row = {
            "station_id": _sanitise_cell(station_id),
            "channel":    _sanitise_cell(flag.get("channel", "all")),
            "start_utc":  _fmt_ts(flag.get("start_utc", "")),
            "end_utc":    _fmt_ts(flag.get("end_utc", "")),
            "issue":      _sanitise_cell(flag.get("issue", "")),
            "action":     _sanitise_cell(flag.get("action", "keep")),
            "note":       _sanitise_cell(flag.get("note", "")),
        }
        # Validate action value
        if row["action"] not in ("exclude", "correct", "keep"):
            logger.warning("Invalid action %r for %s; defaulting to 'keep'.", row["action"], station_id)
            row["action"] = "keep"
        rows.append(row)

    return rows


# ── Write flags.csv ───────────────────────────────────────────────────────────

def write_flags_csv(all_rows: List[Dict], output_path: str) -> None:
    """
    Write flags.csv with atomic replacement (write to temp then rename).
    Validates output schema before writing.
    Raises on failure rather than producing a partial file.
    """
    import csv

    # Validate all rows have required columns
    for i, row in enumerate(all_rows):
        for col in FLAG_COLUMNS:
            if col not in row:
                raise ValueError(f"Row {i} missing column '{col}': {row}")

    # Sort deterministically: station_id, start_utc, channel
    all_rows_sorted = sorted(all_rows, key=lambda r: (
        r["station_id"], r["start_utc"] or "", r["channel"]
    ))

    with atomic_text(output_path) as stream:
        writer = csv.DictWriter(stream, fieldnames=FLAG_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows_sorted)
    logger.info("Wrote %d flags to %s", len(all_rows_sorted), output_path)
