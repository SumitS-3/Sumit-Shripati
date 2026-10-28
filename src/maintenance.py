"""
Parse and index volunteer maintenance notes.

The log has the format:
    YYYY-MM-DD  Sxx  Free-text note

This module extracts events and provides lookup by station_id.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Regex: date  station_id  note
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(S\d+)\s+(.+)$",
    re.MULTILINE,
)

# Keywords that indicate likely sensor changes affecting data quality
SENSOR_CHANGE_KEYWORDS = [
    "swap", "replaced", "fitted", "new", "calibrat",
    "moved", "relocated", "cleaned", "repaint",
]
DATA_INTERRUPTION_KEYWORDS = [
    "stopped", "flat battery", "battery", "reboot",
    "data.*stopped", "same day twice", "sim", "website",
]


@dataclass
class MaintenanceEvent:
    date: date
    station_id: str
    note: str
    is_sensor_change: bool
    is_data_interruption: bool


def parse_maintenance_log(text: str) -> Dict[str, List[MaintenanceEvent]]:
    """
    Parse maintenance log text.
    Returns {station_id: [MaintenanceEvent, ...]} sorted chronologically.
    """
    events: Dict[str, List[MaintenanceEvent]] = {}

    for m in _LINE_RE.finditer(text):
        date_str, sid, note = m.group(1), m.group(2), m.group(3).strip()
        try:
            evt_date = date.fromisoformat(date_str)
        except ValueError:
            logger.warning("Cannot parse maintenance date %r", date_str)
            continue

        note_lower = note.lower()
        is_sensor = any(kw in note_lower for kw in SENSOR_CHANGE_KEYWORDS)
        is_interruption = any(
            re.search(kw, note_lower) for kw in DATA_INTERRUPTION_KEYWORDS
        )

        event = MaintenanceEvent(
            date=evt_date,
            station_id=sid,
            note=note,
            is_sensor_change=is_sensor,
            is_data_interruption=is_interruption,
        )
        events.setdefault(sid, []).append(event)
        logger.debug("Maintenance event: %s %s — %s", date_str, sid, note[:60])

    return events


def find_nearby_events(
    events: Dict[str, List[MaintenanceEvent]],
    station_id: str,
    flag_date: date,
    window_days: int = 3,
) -> List[MaintenanceEvent]:
    """Return events for station_id within ±window_days of flag_date."""
    if station_id not in events:
        return []
    result = []
    for evt in events[station_id]:
        if abs((evt.date - flag_date).days) <= window_days:
            result.append(evt)
    return result

