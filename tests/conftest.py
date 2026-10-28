"""Small independent fixtures: no assignment data or fixed station IDs."""
import pandas as pd
import pytest


@pytest.fixture
def nmea_sentence():
    def make(body, valid_checksum=True):
        checksum = 0
        for character in body:
            checksum ^= ord(character)
        if not valid_checksum:
            checksum ^= 0xFF
        return f"${body}*{checksum:02X}"
    return make


@pytest.fixture
def station_frame():
    def make(timestamps):
        frame = pd.DataFrame({"timestamp_utc": pd.to_datetime(timestamps)})
        frame["temp_c"] = 10 + frame.index * 0.01
        for channel, value in {"rh_pct": 50, "pressure_hpa": 850, "wind_kmh": 5,
                               "wind_dir_deg": 180, "rain_mm": 0, "ghi_wm2": 0}.items():
            frame[channel] = value
        return frame
    return make
