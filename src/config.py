"""
Centralized configuration for the Ridgeline weather-station checker.

All thresholds documented with their physical justification.
Override via a config.json file in the data-pack root if needed.
"""
from __future__ import annotations
import json
import math
import os
from copy import deepcopy
from dataclasses import dataclass, field, asdict, fields
from typing import Dict, Any

from .schema import NUMERIC_COLUMNS


# ── Physical bounds ──────────────────────────────────────────────────────────
# Broad screening ranges, not instrument calibration specifications or universal
# physical impossibility claims. Humidity above its upper bound is never capped.

PHYSICAL_BOUNDS: Dict[str, Dict[str, float]] = {
    "temp_c":        {"min": -90.0,  "max": 65.0},
    "rh_pct":        {"min":   0.0,  "max": 101.0},
    "pressure_hpa":  {"min": 500.0,  "max": 1100.0},  # station pressure, never sea-level corrected
    "wind_kmh":      {"min":   0.0,  "max": 250.0},   # deployment screening limit for ten-minute means
    "wind_dir_deg":  {"min":   0.0,  "max": 360.0},
    "rain_mm":       {"min":   0.0,  "max": 200.0},   # generous interval-total screening range
    "ghi_wm2":       {"min": -10.0,  "max": 1600.0},  # allow small nighttime zero offsets
}

# ── Temporal anomaly thresholds ──────────────────────────────────────────────

@dataclass
class CheckerConfig:
    # Sampling
    sampling_interval_min: int = 10          # expected 10-minute intervals

    # GPS clock corrections require repeated, stable full-date RMC evidence.
    gps_clock_tolerance_seconds: float = 5.0
    gps_clock_min_fixes: int = 3
    gps_clock_max_gap_hours: float = 3.0
    gps_position_tolerance_km: float = 1.0
    gps_jump_tolerance_km: float = 0.5

    # Flatline detection: minimum consecutive identical readings to flag
    flatline_min_records: Dict[str, int] = field(default_factory=lambda: {
        "temp_c":       12,  # 2 h -- temperature changes slowly but 2h identical is suspicious
        "pressure_hpa": 144, # 24 h -- coarse pressure readings can remain stable for hours
        "wind_kmh":     18,  # 3 h -- calm spells real; longer flatline suggests stuck anemometer
        "ghi_wm2":      18,  # 3 h during expected daylight hours only
    })

    # Flatline exempt value: don't flag a channel when stuck at this value
    # RH saturation and dry rain intervals are not screened as flatlines.
    flatline_exempt_zeros: set = field(default_factory=lambda: {"wind_kmh"})

    # Pressure: max credible 10-minute change (hPa)
    pressure_max_step_hpa: float = 2.5

    # RH: flag persistent saturation above this level
    rh_saturation_threshold: float = 99.0
    rh_saturation_min_hours: float = 12.0
    rh_keep_min_hours: float = 2.0
    rh_dry_fraction: float = 0.5
    rh_bright_min_hours: float = 1.0
    dry_peer_rh_pct: float = 80.0
    humid_peer_rh_pct: float = 90.0

    # Generous geometric margins; no fixed UTC hours or deployment-specific timezone.
    solar_night_elevation_deg: float = -6.0
    solar_day_elevation_deg: float = 10.0
    ghi_night_threshold: float = 50.0
    sunny_ghi_wm2: float = 400.0

    # Bounded peer evidence; fixed site offsets are compared with their own baseline.
    peer_count: int = 5
    peer_min_count: int = 3
    peer_radius_km: float = 150.0
    peer_max_elevation_difference_m: float = 750.0
    change_window_days: int = 5
    minimum_peer_hours: int = 24
    event_search_days: int = 1
    wind_min_reference_kmh: float = 3.0
    wind_scale_change_ratio: float = 0.5
    thermal_baseline_days: int = 7
    thermal_delta_c: float = 2.0
    thermal_min_days: int = 3
    rain_peer_wet_mm: float = 1.0
    rain_wet_days: int = 2
    rain_cleaning_burst_mm: float = 5.0
    rain_burst_peer_ratio: float = 10.0
    rapid_warming_c: float = 7.0
    rapid_rh_drop_pct: float = 15.0
    warm_wind_min_kmh: float = 15.0

    # Data gaps: gaps larger than this are flagged
    gap_threshold_min: int = 10              # flag even a single missing 10-minute sample

    # Interval aggregation: capped at one sample interval to avoid flagging good data.
    merge_gap_min: int = 10

    # Report
    report_title: str = "Ridgeline Schools Weather Network — Health Report"

    # Physical bounds (can be overridden per deployment)
    physical_bounds: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: deepcopy(PHYSICAL_BOUNDS)
    )

    def __post_init__(self) -> None:
        # Validate every scalar threshold, including JSON overrides and station copies.
        signed = {"solar_night_elevation_deg", "solar_day_elevation_deg"}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.type in ("int", "float"):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{item.name} must be a finite number")
                if item.type == "int" and type(value) is not int:
                    raise ValueError(f"{item.name} must be an integer")
                lower_ok = value >= 0 if item.name == "merge_gap_min" else value > 0
                if item.name not in signed and not lower_ok:
                    raise ValueError(f"{item.name} must be positive (merge_gap_min may be zero)")
        if self.gps_clock_min_fixes < 3:
            raise ValueError("gps_clock_min_fixes must be at least 3 independent fixes")
        if self.gps_clock_tolerance_seconds >= 30:
            raise ValueError("gps_clock_tolerance_seconds must be below half a minute")
        if self.peer_min_count > self.peer_count or self.peer_min_count < 2:
            raise ValueError("peer_min_count must be between 2 and peer_count")
        if not 0 < self.wind_scale_change_ratio < 1 or not 0 < self.rh_dry_fraction <= 1:
            raise ValueError("wind_scale_change_ratio and rh_dry_fraction must be fractions")
        if not -90 <= self.solar_night_elevation_deg < self.solar_day_elevation_deg <= 90:
            raise ValueError("solar screening margins must be ordered within [-90, 90]")
        if not 0 < self.dry_peer_rh_pct < self.humid_peer_rh_pct <= 100:
            raise ValueError("humidity peer thresholds must be ordered within (0, 100]")
        if not isinstance(self.flatline_min_records, dict) or any(
            channel not in {"temp_c", "pressure_hpa", "wind_kmh", "ghi_wm2"} or type(count) is not int or count <= 0
            for channel, count in self.flatline_min_records.items()
        ):
            raise ValueError("flatline_min_records must map sensor channels to positive integer counts")
        if not isinstance(self.flatline_exempt_zeros, (list, set, tuple)) or not set(self.flatline_exempt_zeros) <= set(NUMERIC_COLUMNS):
            raise ValueError("flatline_exempt_zeros must contain only sensor channel names")
        self.flatline_exempt_zeros = set(self.flatline_exempt_zeros)
        if not isinstance(self.physical_bounds, dict) or set(self.physical_bounds) != set(NUMERIC_COLUMNS):
            raise ValueError("physical_bounds must cover exactly the sensor channels")
        for channel, bounds in self.physical_bounds.items():
            if not isinstance(bounds, dict) or set(bounds) != {"min", "max"} or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in bounds.values()
            ) or bounds["min"] >= bounds["max"]:
                raise ValueError(f"invalid screening bounds for {channel}")
        if not isinstance(self.report_title, str) or not self.report_title.strip():
            raise ValueError("report_title must be nonempty text")

    @classmethod
    def from_file(cls, path: str) -> "CheckerConfig":
        """Load config overrides from a JSON file (missing keys use defaults)."""
        if not os.path.isfile(path):
            return cls()
        with open(path, encoding="utf-8") as f:
            overrides = json.load(f)
        if not isinstance(overrides, dict):
            raise ValueError("configuration must be a JSON object")
        unknown = set(overrides) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"unknown configuration settings: {', '.join(sorted(unknown))}")
        if "physical_bounds" in overrides and isinstance(overrides["physical_bounds"], dict):
            overrides["physical_bounds"] = {**deepcopy(PHYSICAL_BOUNDS), **overrides["physical_bounds"]}
        return cls(**overrides)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["flatline_exempt_zeros"] = sorted(self.flatline_exempt_zeros)
        return result
