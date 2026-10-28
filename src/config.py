"""
Centralized configuration for the Ridgeline weather-station checker.

All thresholds documented with their physical justification.
Override via a config.json file in the data-pack root if needed.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, Any


# ── Physical bounds ──────────────────────────────────────────────────────────
# Sources: WMO No.8 (2018), ASCE 7 standards, Colorado climate extremes.
# These are *screening* bounds — values outside them are implausible, not
# necessarily wrong.  All-time Colorado records: -51 °C (1985), +47 °C (1933).

PHYSICAL_BOUNDS: Dict[str, Dict[str, float]] = {
    "temp_c":        {"min": -50.0,  "max": 50.0},
    "rh_pct":        {"min":   0.0,  "max": 105.0},   # allow slight sensor over-read
    "pressure_hpa":  {"min": 500.0,  "max": 1100.0},  # high-altitude stations valid ~600 hPa
    "wind_kmh":      {"min":   0.0,  "max": 250.0},   # WMO max gust record ~408 km/h
    "wind_dir_deg":  {"min":   0.0,  "max": 360.0},
    "rain_mm":       {"min":   0.0,  "max": 200.0},   # 200 mm/10 min is extreme but physically possible
    "ghi_wm2":       {"min":   0.0,  "max": 1500.0},  # extraterrestrial irradiance ~1361 W/m²
}

# ── Temporal anomaly thresholds ──────────────────────────────────────────────

@dataclass
class CheckerConfig:
    # Sampling
    sampling_interval_min: int = 10          # expected 10-minute intervals

    # Flatline detection: minimum consecutive identical readings to flag
    flatline_min_records: Dict[str, int] = field(default_factory=lambda: {
        "temp_c":       12,  # 2 h -- temperature changes slowly but 2h identical is suspicious
        "rh_pct":       18,  # 3 h -- RH can be stable for hours; only long runs are suspicious
        "pressure_hpa": 24,  # 4 h -- pressure can be very stable in anticyclonic conditions
        "wind_kmh":     18,  # 3 h -- calm spells real; longer flatline suggests stuck anemometer
        "ghi_wm2":      18,  # 3 h during expected daylight hours only
    })

    # Flatline exempt value: don't flag a channel when stuck at this value
    # (rain=0 at night is normal; wind=0 in a calm is plausible)
    flatline_exempt_zeros: set = field(default_factory=lambda: {"rain_mm", "wind_kmh"})

    # Spike detection: modified Z-score threshold (MAD-based)
    spike_mad_threshold: float = 6.0          # conservative; avoids genuine rapid changes
    spike_window_records: int = 12            # 2-hour rolling window

    # Pressure: max credible 10-minute change (hPa)
    pressure_max_step_hpa: float = 2.5

    # RH: flag persistent saturation above this level
    rh_saturation_threshold: float = 100.0
    rh_saturation_min_hours: float = 1.5      # 9 records

    # GHI: solar geometry tolerance
    # Stations in Colorado: UTC offset ~-7 (MST); sunrise approx 13:00 UTC, sunset approx 01:00 UTC
    ghi_night_start_utc: int = 2   # 02:00 UTC = ~19:00 local MST (well after sunset)
    ghi_night_end_utc:   int = 12  # 12:00 UTC = ~05:00 local MST (before sunrise)
    ghi_night_threshold: float = 200.0         # W/m2 -- below this can be twilight/dawn

    # Data gaps: gaps larger than this are flagged
    gap_threshold_min: int = 20              # more than 2 missed intervals

    # Interval aggregation: consecutive flags within this gap are merged
    merge_gap_min: int = 30                  # merge if ≤ 30 min between same-issue windows

    # Report
    report_title: str = "Ridgeline Schools Weather Network — Health Report"

    # Physical bounds (can be overridden per deployment)
    physical_bounds: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: PHYSICAL_BOUNDS
    )

    @classmethod
    def from_file(cls, path: str) -> "CheckerConfig":
        """Load config overrides from a JSON file (missing keys use defaults)."""
        cfg = cls()
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                overrides: Dict[str, Any] = json.load(f)
            for k, v in overrides.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
