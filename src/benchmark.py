"""Synthetic validation benchmark, including retained frame allocation.

Includes generation, temporal normalization, local checks and network evidence;
excludes CSV/NMEA parsing and HTML rendering. Traced allocations are not process RSS.
No synthetic record is written to flags.csv or mixed with assignment observations.
"""
from __future__ import annotations

import time
import tracemalloc

import numpy as np
import pandas as pd

from .checks import run_all_checks
from .config import CheckerConfig
from .maintenance import parse_maintenance_log
from .network_checks import run_network_checks
from .solar import solar_elevation
from .temporal import prepare_observations


def _generate_synthetic_station(n_records: int = 6048, seed: int = 42,
                                latitude: float = 40, longitude: float = -105) -> pd.DataFrame:
    """Artificial shared weather with independent small sensor variation."""
    rng = np.random.default_rng(seed)
    times = pd.date_range("2026-06-01", periods=n_records, freq="10min")
    hour = times.hour.to_numpy() + times.minute.to_numpy() / 60
    local_hour = (hour + longitude / 15) % 24
    cycle = np.sin((local_hour - 9) * np.pi / 12)
    elevation = solar_elevation(times - pd.Timedelta(minutes=5), latitude, longitude)
    ghi = np.maximum(0, np.sin(np.deg2rad(elevation))) * 1000
    ghi = np.where(elevation > 0, np.maximum(0, ghi + rng.normal(0, 5, n_records)), 0)
    return pd.DataFrame({
        "timestamp_utc": times, "temp_c": 15 + 7 * cycle + rng.normal(0, 0.1, n_records),
        "rh_pct": 55 - 20 * cycle + rng.normal(0, 0.5, n_records),
        "pressure_hpa": 850 + np.sin(np.arange(n_records) / 500) + rng.normal(0, 0.03, n_records),
        "wind_kmh": 10 + 2 * np.sin(np.arange(n_records) / 50) + rng.normal(0, 0.2, n_records),
        "wind_dir_deg": rng.integers(180, 300, n_records).astype(float),
        "rain_mm": np.where(rng.random(n_records) < 0.01, 0.2, 0.0), "ghi_wm2": ghi,
    })


def run_benchmark(station_counts: list[int] | None = None, n_records: int = 6048) -> list[dict]:
    """Run stations with a small proportion of documented wind-scale changes."""
    counts = station_counts if station_counts is not None else [8, 100, 500]
    results = []
    print("SYNTHETIC VALIDATION BENCHMARK")
    print("Includes frame allocation and validation; excludes file parsing and HTML; memory is traced MiB.")
    print(f"{'Stations':>10} {'Time (s)':>10} {'Traced MiB':>12} {'Flags':>9}")
    for count in counts:
        if type(count) is not int or count <= 0:
            raise ValueError("station counts must be positive integers")
        tracemalloc.start()
        started = time.perf_counter()
        try:
            data, metadata, flags, configs, notes = {}, {}, {}, {}, []
            for number in range(count):
                sid = f"S{number + 1000}"
                lat, lon = 40 + (number % 20) * 0.01, -105 + (number // 20) * 0.01
                cfg = CheckerConfig()
                frame = _generate_synthetic_station(n_records, number, lat, lon)
                if number % 20 == 0:
                    changed = frame.timestamp_utc >= pd.Timestamp("2026-06-21 12:00")
                    frame.loc[changed, "wind_kmh"] *= 0.3
                    notes.append(f"2026-06-21  {sid}  Fitted replacement anemometer with a different pulse specification.")
                frame, temporal_flags = prepare_observations(frame, [], cfg)
                data[sid], configs[sid] = frame, cfg
                metadata[sid] = {"lat": lat, "lon": lon, "elevation_m": 1600}
                flags[sid] = temporal_flags + run_all_checks(sid, frame, cfg, lat, lon)
            final = run_network_checks(data, metadata, flags, configs, parse_maintenance_log("\n".join(notes)))
            if final.failures:
                raise RuntimeError(f"synthetic validation failed: {final.failures}")
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            result = {"stations": count, "seconds": elapsed, "traced_mib": peak / 1024**2,
                      "flags": sum(len(items) for items in final.flags.values())}
            results.append(result)
        finally:
            tracemalloc.stop()
        # Avoid retaining the previous network while allocating the next run.
        del data, metadata, flags, configs, final, frame
        print(f"{count:>10} {elapsed:>10.2f} {result['traced_mib']:>12.2f} {result['flags']:>9}")
    return results


if __name__ == "__main__":
    run_benchmark()
