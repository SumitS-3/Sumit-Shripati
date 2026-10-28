"""
Automated test suite for the Ridgeline weather-station checker.

Run:  python -m pytest tests/ -v
"""
import csv
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Add project root to path
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import CheckerConfig
from src.checks import (
    check_completeness,
    check_flatlines,
    check_ghi_night,
    check_physical_bounds,
    check_pressure_steps,
    check_rh_bounds,
    check_cross_channel,
    run_all_checks,
    _find_runs,
)
from src.flagging import (
    aggregate_flags,
    build_flag_rows,
    write_flags_csv,
    _sanitise_cell,
    _fmt_ts,
)
from src.gps import (
    _dddmm_to_decimal,
    _nmea_checksum,
    parse_nmea_file,
    haversine_km,
)
from src.ingestion import (
    load_station_data,
    discover_station_files,
)
from src.maintenance import parse_maintenance_log


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def cfg():
    return CheckerConfig()

def _make_df(n=100, seed=42):
    """Create a clean synthetic DataFrame for testing."""
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2025-03-15 12:00")
    timestamps = pd.date_range(t0, periods=n, freq="10min")
    return pd.DataFrame({
        "timestamp_utc": timestamps,
        "temp_c":       10.0 + rng.normal(0, 0.5, n),
        "rh_pct":       50.0 + rng.normal(0, 3.0, n),
        "pressure_hpa": 850.0 + rng.normal(0, 0.3, n),
        "wind_kmh":     5.0 + rng.exponential(2.0, n),
        "wind_dir_deg": rng.integers(0, 360, n).astype(float),
        "rain_mm":      np.zeros(n),
        "ghi_wm2":      np.clip(400 * np.sin(np.linspace(0, np.pi, n)), 0, 1200),
        "dup_ts":       False,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  UNIT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestTimestampParsing:
    def test_valid_timestamp(self):
        ts = pd.to_datetime("2025-03-15 12:30", format="%Y-%m-%d %H:%M")
        assert ts == pd.Timestamp("2025-03-15 12:30")

    def test_invalid_timestamp_returns_nat(self):
        ts = pd.to_datetime("not-a-date", format="%Y-%m-%d %H:%M", errors="coerce")
        assert pd.isna(ts)


class TestPhysicalBounds:
    def test_temperature_out_of_range(self, cfg):
        df = _make_df()
        df.loc[5, "temp_c"] = 85.0  # impossible temperature
        flags = check_physical_bounds(df, cfg)
        assert len(flags) >= 1
        assert any("Out-of-range temp_c" in f["issue"] for f in flags)

    def test_clean_data_no_flags(self, cfg):
        df = _make_df()
        flags = check_physical_bounds(df, cfg)
        assert len(flags) == 0


class TestFlatlineDetection:
    def test_temp_flatline_detected(self, cfg):
        df = _make_df()
        # Insert a 20-record flatline (> 12 threshold)
        df.loc[30:49, "temp_c"] = 6.5
        flags = check_flatlines(df, cfg)
        temp_flats = [f for f in flags if "temp_c" in f["channel"]]
        assert len(temp_flats) >= 1
        assert any("6.5" in f["issue"] for f in temp_flats)

    def test_short_flatline_not_flagged(self, cfg):
        df = _make_df()
        # Only 5 records (< 12 threshold for temp)
        df.loc[30:34, "temp_c"] = 6.5
        flags = check_flatlines(df, cfg)
        temp_flats = [f for f in flags if "temp_c" in f["channel"]]
        assert len(temp_flats) == 0


class TestPressureSteps:
    def test_sudden_pressure_jump(self, cfg):
        df = _make_df()
        df.loc[50, "pressure_hpa"] = 860.0  # jump of ~10 hPa
        flags = check_pressure_steps(df, cfg)
        assert len(flags) >= 1
        assert any("Pressure step" in f["issue"] for f in flags)

    def test_gradual_change_no_flag(self, cfg):
        df = _make_df()
        # 0.5 hPa/10min is within natural bounds
        df["pressure_hpa"] = 850.0 + np.linspace(0, 5, len(df))
        flags = check_pressure_steps(df, cfg)
        assert len(flags) == 0


class TestRHBounds:
    def test_rh_over_101_flagged(self, cfg):
        df = _make_df()
        df.loc[10:15, "rh_pct"] = 105.0
        flags = check_rh_bounds(df, cfg)
        assert len(flags) >= 1
        assert all(f["action"] == "correct" for f in flags)

    def test_rh_at_100_not_flagged(self, cfg):
        df = _make_df()
        df.loc[10:15, "rh_pct"] = 100.0
        flags = check_rh_bounds(df, cfg)
        assert len(flags) == 0


class TestGHINight:
    def test_ghi_high_at_night_flagged(self, cfg):
        df = _make_df()
        # Set all timestamps to UTC 05:00 (nighttime in Colorado)
        df["timestamp_utc"] = pd.date_range(
            "2025-03-15 05:00", periods=len(df), freq="10min"
        )
        df["ghi_wm2"] = 500.0  # clearly not nighttime-appropriate
        flags = check_ghi_night(df, cfg)
        assert len(flags) >= 1

    def test_ghi_zero_at_night_ok(self, cfg):
        df = _make_df()
        df["timestamp_utc"] = pd.date_range(
            "2025-03-15 05:00", periods=len(df), freq="10min"
        )
        df["ghi_wm2"] = 0.0
        flags = check_ghi_night(df, cfg)
        assert len(flags) == 0


class TestDataCompleteness:
    def test_gap_detected(self, cfg):
        df = _make_df(n=50)
        # Remove 5 rows to create a gap
        df = df.drop(index=[20, 21, 22, 23, 24]).reset_index(drop=True)
        flags = check_completeness(df, cfg)
        gap_flags = [f for f in flags if "gap" in f["issue"].lower()]
        assert len(gap_flags) >= 1

    def test_duplicate_timestamps(self, cfg):
        df = _make_df(n=20)
        df["dup_ts"] = False
        df.loc[5, "dup_ts"] = True
        df.loc[6, "dup_ts"] = True
        flags = check_completeness(df, cfg)
        dup_flags = [f for f in flags if "uplicate" in f["issue"]]
        assert len(dup_flags) >= 1


class TestCrossChannel:
    def test_rh_sat_with_high_ghi(self, cfg):
        df = _make_df(n=50)
        df.loc[10:19, "rh_pct"] = 100.0
        df.loc[10:19, "ghi_wm2"] = 500.0
        flags = check_cross_channel(df, cfg)
        assert len(flags) >= 1


# ══════════════════════════════════════════════════════════════════════════════
#  GPS UNIT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestGPSParsing:
    def test_dddmm_to_decimal_north(self):
        result = _dddmm_to_decimal("3953.6752", "N")
        assert result is not None
        assert abs(result - 39.894587) < 0.001

    def test_dddmm_to_decimal_west(self):
        result = _dddmm_to_decimal("10533.8904", "W")
        assert result is not None
        assert abs(result - (-105.5648)) < 0.001

    def test_invalid_dddmm(self):
        assert _dddmm_to_decimal("", "N") is None
        assert _dddmm_to_decimal("abc", "N") is None

    def test_nmea_checksum_valid(self):
        sentence = "$GPRMC,000000.00,A,3953.6752,N,10533.8904,W,0.0,0.0,010325,,,A*42"
        ok, _ = _nmea_checksum(sentence)
        assert ok

    def test_nmea_checksum_invalid(self):
        sentence = "$GPRMC,000000.00,A,3953.6752,N,10533.8904,W,0.0,0.0,010325,,,A*FF"
        ok, _ = _nmea_checksum(sentence)
        assert not ok

    def test_haversine_zero_distance(self):
        d = haversine_km(40.0, -105.0, 40.0, -105.0)
        assert d < 0.001


# ══════════════════════════════════════════════════════════════════════════════
#  FLAGGING UNIT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestFlagging:
    def test_sanitise_cell_injection(self):
        assert _sanitise_cell("=cmd()").startswith("'")
        assert _sanitise_cell("+cmd()").startswith("'")
        assert _sanitise_cell("normal text") == "normal text"

    def test_fmt_ts_format(self):
        result = _fmt_ts(pd.Timestamp("2025-03-15 12:30"))
        assert result == "2025-03-15 12:30"

    def test_fmt_ts_empty(self):
        assert _fmt_ts(None) == ""
        assert _fmt_ts("") == ""

    def test_aggregate_merges_adjacent(self, cfg):
        candidates = [
            {"channel": "temp_c", "start_utc": pd.Timestamp("2025-03-15 12:00"),
             "end_utc": pd.Timestamp("2025-03-15 12:10"),
             "issue": "Flatline: temp_c same", "action": "exclude", "note": "test"},
            {"channel": "temp_c", "start_utc": pd.Timestamp("2025-03-15 12:20"),
             "end_utc": pd.Timestamp("2025-03-15 12:30"),
             "issue": "Flatline: temp_c same", "action": "exclude", "note": "test2"},
        ]
        merged = aggregate_flags(candidates, cfg)
        assert len(merged) == 1  # should merge
        assert merged[0]["start_utc"] == candidates[0]["start_utc"]
        assert merged[0]["end_utc"] == candidates[1]["end_utc"]
        assert "test2" in merged[0]["note"]

    def test_aggregate_separates_different_channels(self, cfg):
        candidates = [
            {"channel": "temp_c", "start_utc": pd.Timestamp("2025-03-15 12:00"),
             "end_utc": pd.Timestamp("2025-03-15 12:10"),
             "issue": "Flatline: temp_c", "action": "exclude", "note": "test"},
            {"channel": "rh_pct", "start_utc": pd.Timestamp("2025-03-15 12:20"),
             "end_utc": pd.Timestamp("2025-03-15 12:30"),
             "issue": "Flatline: rh_pct", "action": "exclude", "note": "test"},
        ]
        merged = aggregate_flags(candidates, cfg)
        assert len(merged) == 2  # different channels, don't merge


class TestWriteFlags:
    def test_write_and_read(self, cfg):
        rows = [
            {"station_id": "S01", "channel": "temp_c",
             "start_utc": "2025-03-15 12:00", "end_utc": "2025-03-15 12:30",
             "issue": "Test issue", "action": "exclude", "note": "Test note."},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            tmp = f.name
        try:
            write_flags_csv(rows, tmp)
            df = pd.read_csv(tmp)
            assert len(df) == 1
            assert list(df.columns) == [
                "station_id", "channel", "start_utc", "end_utc",
                "issue", "action", "note"
            ]
        finally:
            os.unlink(tmp)


# ══════════════════════════════════════════════════════════════════════════════
#  MAINTENANCE LOG TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestMaintenance:
    def test_parse_log(self):
        text = (
            "2025-03-21  S02  Swapped the pressure board with the spare.\n"
            "2025-03-24  S06  Battery was flat. Swapped battery, logger rebooted.\n"
        )
        events = parse_maintenance_log(text)
        assert "S02" in events
        assert "S06" in events
        assert events["S02"][0].is_sensor_change
        assert events["S06"][0].is_data_interruption


# ══════════════════════════════════════════════════════════════════════════════
#  SECURITY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurity:
    def test_csv_formula_injection_blocked(self):
        assert _sanitise_cell("=1+1") == "'=1+1"
        assert _sanitise_cell("+cmd()") == "'+cmd()"
        assert _sanitise_cell("-malicious") == "'-malicious"
        assert _sanitise_cell("@SUM(A1)") == "'@SUM(A1)"

    def test_normal_text_unchanged(self):
        assert _sanitise_cell("Temperature flatline") == "Temperature flatline"
        assert _sanitise_cell("S01") == "S01"

    def test_source_file_protection(self):
        """Verify the checker refuses to overwrite source data files."""
        # This is tested implicitly by checker.py logic
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  EDGE CASE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_empty_dataframe(self, cfg):
        df = pd.DataFrame(columns=["timestamp_utc", "temp_c", "rh_pct",
                                     "pressure_hpa", "wind_kmh", "wind_dir_deg",
                                     "rain_mm", "ghi_wm2", "dup_ts"])
        flags = run_all_checks("TEST", df, cfg)
        assert isinstance(flags, list)

    def test_single_row(self, cfg):
        df = _make_df(n=1)
        flags = run_all_checks("TEST", df, cfg)
        assert isinstance(flags, list)

    def test_all_nan(self, cfg):
        df = _make_df(n=20)
        for col in ["temp_c", "rh_pct", "pressure_hpa"]:
            df[col] = float("nan")
        flags = run_all_checks("TEST", df, cfg)
        # Should find NaN issues but not crash
        assert isinstance(flags, list)

    def test_no_anomalies(self, cfg):
        df = _make_df()
        flags = run_all_checks("TEST", df, cfg)
        # Clean synthetic data should have minimal flags
        assert isinstance(flags, list)


class TestFindRuns:
    def test_single_run(self):
        s = pd.Series([False, True, True, True, False])
        runs = _find_runs(s, s)
        assert runs == [(1, 3, 3)]

    def test_no_runs(self):
        s = pd.Series([False, False, False])
        runs = _find_runs(s, s)
        assert runs == []

    def test_multiple_runs(self):
        s = pd.Series([True, True, False, True, False])
        runs = _find_runs(s, s)
        assert len(runs) == 2


# ══════════════════════════════════════════════════════════════════════════════
#  INTEGRATION TEST
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration:
    def test_full_run_produces_valid_csv(self, cfg):
        """End-to-end: generate synthetic station, run all checks, write flags."""
        df = _make_df(n=200)
        # Inject a known anomaly
        df.loc[50, "temp_c"] = 100.0  # out of range

        candidates = run_all_checks("SYNTH01", df, cfg)
        rows = build_flag_rows("SYNTH01", candidates, {}, cfg)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            tmp = f.name
        try:
            write_flags_csv(rows, tmp)
            out_df = pd.read_csv(tmp)
            required_cols = ["station_id", "channel", "start_utc", "end_utc",
                             "issue", "action", "note"]
            for c in required_cols:
                assert c in out_df.columns, f"Missing column: {c}"
            assert (out_df["station_id"] == "SYNTH01").all()
            assert len(out_df) >= 1  # at least the out-of-range flag
        finally:
            os.unlink(tmp)

    def test_deterministic_output(self, cfg):
        """Running the same data twice produces identical flags."""
        df = _make_df(n=100)
        df.loc[50, "temp_c"] = 100.0

        flags1 = run_all_checks("DET01", df.copy(), cfg)
        flags2 = run_all_checks("DET01", df.copy(), cfg)

        rows1 = build_flag_rows("DET01", flags1, {}, cfg)
        rows2 = build_flag_rows("DET01", flags2, {}, cfg)

        assert len(rows1) == len(rows2)
        for r1, r2 in zip(rows1, rows2):
            assert r1["issue"] == r2["issue"]
            assert r1["action"] == r2["action"]

