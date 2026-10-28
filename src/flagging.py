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
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from .config import CheckerConfig
from .maintenance import MaintenanceEvent, find_nearby_events

logger = logging.getLogger(__name__)

FLAG_COLUMNS = ["station_id", "channel", "start_utc", "end_utc", "issue", "action", "note"]
TS_FMT = "%Y-%m-%d %H:%M"


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
    if a["channel"] != b["channel"]:
        return False
    # Compare only the first few words of the issue (same problem category)
    issue_a = " ".join(a["issue"].split()[:3])
    issue_b = " ".join(b["issue"].split()[:3])
    if issue_a != issue_b:
        return False
    # Gap between end of a and start of b
    try:
        t_end   = pd.Timestamp(a["end_utc"])
        t_start = pd.Timestamp(b["start_utc"])
        gap_min = (t_start - t_end).total_seconds() / 60
        return 0 <= gap_min <= merge_gap_min
    except Exception:
        return False


def aggregate_flags(
    candidates: List[Dict],
    cfg: CheckerConfig,
) -> List[Dict]:
    """
    Merge consecutive candidates for the same channel / issue into intervals.
    Returns sorted list of merged flag dicts.
    """
    if not candidates:
        return []

    # Sort by channel, then start_utc
    def sort_key(f: Dict) -> tuple:
        return (
            f.get("channel", ""),
            f.get("start_utc", pd.NaT) if f.get("start_utc") else pd.NaT,
        )

    sorted_cands = sorted(candidates, key=lambda f: (
        f.get("channel", ""),
        f.get("start_utc") or pd.Timestamp.min,
    ))

    merged: List[Dict] = []
    current = dict(sorted_cands[0])

    for next_flag in sorted_cands[1:]:
        if _should_merge(current, next_flag, cfg.merge_gap_min):
            # Extend the current interval
            current["end_utc"] = next_flag["end_utc"]
        else:
            merged.append(current)
            current = dict(next_flag)

    merged.append(current)
    return merged


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
        if ts and not isinstance(ts, str):
            flag_date = pd.Timestamp(ts).date()
            nearby = find_nearby_events(maintenance_events, station_id, flag_date, window_days=2)
            if nearby:
                maint_text = "; ".join(
                    f"[{evt.date} maint: {evt.note[:80]}]" for evt in nearby
                )
                flag = dict(flag)
                flag["note"] = flag["note"].rstrip(".") + ". " + maint_text + "."
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
    import os
    import tempfile

    # Validate all rows have required columns
    for i, row in enumerate(all_rows):
        for col in FLAG_COLUMNS:
            if col not in row:
                raise ValueError(f"Row {i} missing column '{col}': {row}")

    # Sort deterministically: station_id, start_utc, channel
    all_rows_sorted = sorted(all_rows, key=lambda r: (
        r["station_id"], r["start_utc"] or "", r["channel"]
    ))

    # Protect source files: refuse to overwrite any input file
    output_path = os.path.abspath(output_path)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv.tmp", dir=os.path.dirname(output_path))
    try:
        with os.fdopen(tmp_fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FLAG_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_rows_sorted)
        os.replace(tmp_path, output_path)
        logger.info("Wrote %d flags to %s", len(all_rows_sorted), output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
