"""
Lightweight performance benchmark.

Generates synthetic weather-station data and measures:
  - Processing time per station
  - Memory usage
  - Scaling behaviour (8, 50, 100 stations)

Synthetic data is clearly labelled and NEVER mixed with real observations.
"""
from __future__ import annotations

import logging
import os
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Add project root to path when run standalone
_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from src.checks import run_all_checks
from src.flagging import aggregate_flags
from src.config import CheckerConfig


def _generate_synthetic_station(
    station_id: str,
    n_records: int = 6048,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Generate n_records rows of plausible but fully synthetic weather data.
    This data is NEVER presented as real observations.
    """
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2025-03-01 00:00")
    timestamps = pd.date_range(t0, periods=n_records, freq="10min")

    # Synthetic diurnal temperature
    hour = timestamps.hour + timestamps.minute / 60
    temp = 5.0 + 8.0 * np.sin((hour - 6) * np.pi / 12) + rng.normal(0, 0.5, n_records)

    rh   = 50.0 - 20.0 * np.sin((hour - 6) * np.pi / 12) + rng.normal(0, 3, n_records)
    rh   = np.clip(rh, 10, 100)

    pressure = 850.0 + rng.normal(0, 1.0, n_records).cumsum() * 0.01
    pressure = np.clip(pressure, 800, 900)

    wind = np.abs(rng.normal(5.0, 3.0, n_records))
    wind_dir = rng.integers(0, 360, n_records).astype(float)

    rain = np.where(rng.random(n_records) < 0.02, rng.exponential(0.2, n_records), 0.0)

    # GHI: zero at night, positive by day
    ghi = np.where(
        (hour >= 6) & (hour <= 20),
        np.clip(600 * np.sin((hour - 6) * np.pi / 14) + rng.normal(0, 30, n_records), 0, 1200),
        0.0,
    )

    df = pd.DataFrame({
        "timestamp_utc": timestamps,
        "temp_c":         temp,
        "rh_pct":         rh,
        "pressure_hpa":   pressure,
        "wind_kmh":       wind,
        "wind_dir_deg":   wind_dir,
        "rain_mm":        rain,
        "ghi_wm2":        ghi,
    })
    df["dup_ts"] = False
    return df


def run_benchmark(station_counts: List[int] = None) -> None:
    """
    Run the checker on synthetic data at multiple station counts.
    Prints a results table.
    """
    if station_counts is None:
        station_counts = [8, 50, 100]

    cfg = CheckerConfig()

    print("\n" + "=" * 60)
    print("SYNTHETIC PERFORMANCE BENCHMARK")
    print("(Data is entirely artificial — not real observations)")
    print("=" * 60)
    print(f"{'Stations':>10} {'Time (s)':>10} {'Peak RAM (MB)':>14}")
    print("-" * 40)

    for n_stations in station_counts:
        # Generate synthetic data outside the timed section
        datasets: Dict[str, pd.DataFrame] = {}
        for i in range(n_stations):
            sid = f"SY{i:03d}"
            datasets[sid] = _generate_synthetic_station(sid, n_records=6048, seed=i)

        # Time the checks
        tracemalloc.start()
        t0 = time.perf_counter()

        all_candidates = []
        for sid, df in datasets.items():
            candidates = run_all_checks(sid, df, cfg)
            merged = aggregate_flags(candidates, cfg)
            all_candidates.extend(merged)

        elapsed = time.perf_counter() - t0
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        peak_mb = peak_bytes / 1e6
        print(f"{n_stations:>10} {elapsed:>10.2f} {peak_mb:>14.1f}")

    print("=" * 60)
    print(f"Note: Each synthetic station has 6048 records (6 weeks × 10-min).")
    print("Real performance will differ based on hardware and I/O speed.\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run_benchmark([8, 50, 100])

