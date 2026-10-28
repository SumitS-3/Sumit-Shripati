"""Shared interval masking for peer evidence and subsequent quality summaries."""
from __future__ import annotations

import numpy as np
import pandas as pd


def interval_mask(timestamps: pd.Series, flags: list[dict], channel: str,
                  actions: tuple[str, ...] = ("exclude",)) -> pd.Series:
    """Union of selected flag intervals on sorted UTC timestamps, counting overlap once."""
    times = pd.DatetimeIndex(timestamps)
    if not times.is_monotonic_increasing:
        raise ValueError("interval masking requires sorted timestamps")
    starts, ends = [], []
    for flag in flags:
        if flag.get("channel") not in (channel, "all") or flag.get("action") not in actions:
            continue
        start, end = pd.Timestamp(flag.get("start_utc")), pd.Timestamp(flag.get("end_utc"))
        if pd.notna(start) and pd.notna(end) and start <= end:
            starts.append(start)
            ends.append(end)
    events = np.zeros(len(times) + 1, dtype=np.int64)
    if starts:
        np.add.at(events, times.searchsorted(starts, side="left"), 1)
        np.add.at(events, times.searchsorted(ends, side="right"), -1)
    return pd.Series(events[:-1].cumsum() > 0, index=timestamps.index)
