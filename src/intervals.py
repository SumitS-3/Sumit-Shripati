"""Positional runs shared by observation checks and timestamp normalization."""
from __future__ import annotations

import numpy as np
import pandas as pd


def find_runs(
    series: pd.Series | pd.DataFrame,
    condition: pd.Series,
    max_gap_min: int | None = None,
    break_before: pd.Series | None = None,
) -> list[tuple[int, int, int]]:
    """Return inclusive (start, end, count) runs without bridging time or value breaks."""
    mask = condition.fillna(False).to_numpy(dtype=bool)
    breaks = np.zeros(len(mask), dtype=bool)
    if break_before is not None:
        breaks |= break_before.to_numpy(dtype=bool)
    if max_gap_min is not None:
        timestamps = series["timestamp_utc"] if isinstance(series, pd.DataFrame) else series
        elapsed = timestamps.diff()
        breaks |= ((elapsed > pd.Timedelta(minutes=max_gap_min)) |
                   (elapsed <= pd.Timedelta(0))).to_numpy()
    starts = np.flatnonzero(mask & (~np.r_[False, mask[:-1]] | breaks)) if len(mask) else []
    ends = np.flatnonzero(mask & (~np.r_[mask[1:], False] | np.r_[breaks[1:], False])) if len(mask) else []
    return [(int(start), int(end), int(end - start + 1)) for start, end in zip(starts, ends)]
