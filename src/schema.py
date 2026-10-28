"""Shared input and output contracts; internal bookkeeping is never a sensor channel."""

TS_FMT = "%Y-%m-%d %H:%M"
NUMERIC_COLUMNS = [
    "temp_c", "rh_pct", "pressure_hpa", "wind_kmh", "wind_dir_deg", "rain_mm", "ghi_wm2",
]
REQUIRED_COLUMNS = ["timestamp_utc", *NUMERIC_COLUMNS]
FLAG_COLUMNS = ["station_id", "channel", "start_utc", "end_utc", "issue", "action", "note"]
