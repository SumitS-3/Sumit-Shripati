"""Vectorized screening geometry from NOAA's General Solar Position Calculations.

https://gml.noaa.gov/grad/solcalc/solareqns.PDF
This approximation supports generous night/day margins, not precision ephemerides.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def solar_elevation(timestamps, latitude: float, longitude: float) -> np.ndarray:
    """Geometric elevation in degrees for UTC timestamps and east-positive longitude."""
    if not np.isfinite([latitude, longitude]).all() or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("solar coordinates must be finite degrees within latitude/longitude bounds")
    times = pd.DatetimeIndex(timestamps)
    if times.tz is not None:
        times = times.tz_convert("UTC").tz_localize(None)
    hour = times.hour.to_numpy() + times.minute.to_numpy() / 60 + times.second.to_numpy() / 3600
    year_days = np.where(times.is_leap_year, 366, 365)
    gamma = 2 * np.pi / year_days * (times.dayofyear.to_numpy() - 1 + (hour - 12) / 24)
    equation = 229.18 * (0.000075 + 0.001868 * np.cos(gamma) - 0.032077 * np.sin(gamma)
                         - 0.014615 * np.cos(2 * gamma) - 0.040849 * np.sin(2 * gamma))
    declination = (0.006918 - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma)
                   - 0.006758 * np.cos(2 * gamma) + 0.000907 * np.sin(2 * gamma)
                   - 0.002697 * np.cos(3 * gamma) + 0.00148 * np.sin(3 * gamma))
    angle = np.deg2rad((hour * 60 + equation + 4 * longitude) / 4 - 180)
    lat = np.deg2rad(latitude)
    cosine = np.sin(lat) * np.sin(declination) + np.cos(lat) * np.cos(declination) * np.cos(angle)
    return 90 - np.rad2deg(np.arccos(np.clip(cosine, -1, 1)))


def interval_sun_masks(timestamps: pd.Series, latitude: float | None, longitude: float | None,
                       interval_minutes: int, night_degrees: float = -6,
                       day_degrees: float = 10) -> tuple[pd.Series, pd.Series]:
    """Require the whole end-labelled interval to clear the screening margin."""
    if latitude is None or longitude is None or not np.isfinite([latitude, longitude]).all():
        unknown = pd.Series(False, index=timestamps.index)
        return unknown, unknown.copy()
    elevations = np.vstack([
        solar_elevation(timestamps - pd.Timedelta(minutes=interval_minutes * fraction), latitude, longitude)
        for fraction in (0, 0.5, 1)
    ])
    return (pd.Series(elevations.max(axis=0) < night_degrees, index=timestamps.index),
            pd.Series(elevations.min(axis=0) > day_degrees, index=timestamps.index))
