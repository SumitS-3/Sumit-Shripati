import json

import pandas as pd
import pytest

import checker
import src.checks as checks
from src.config import CheckerConfig
from src.ingestion import load_station_data
from src.schema import TS_FMT
from src.network_health import compute_station_summaries, recommend_technician_visit


@pytest.fixture
def pack(tmp_path, station_frame):
    directory = tmp_path / "pack"
    (directory / "data").mkdir(parents=True)
    frame = station_frame(pd.date_range("2026-01-10 15:00", periods=8, freq="10min"))
    frame.to_csv(directory / "data" / "S99.csv", index=False, date_format=TS_FMT)
    return directory


def test_source_file_protection_is_enforced(pack):
    source = pack / "data" / "flags.csv"
    source.write_text("source data that must survive\n")
    assert checker.main([str(pack), "--output-dir", str(pack / "data")]) == 1
    assert source.read_text() == "source data that must survive\n"


def test_one_failed_station_produces_partial_exit_and_visible_flag(pack, tmp_path):
    (pack / "data" / "S98.csv").write_text("timestamp_utc,temp_c\nnot-a-date,10\n")
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 2
    flags = pd.read_csv(tmp_path / "flags.csv")
    failure = flags.loc[flags.station_id.eq("S98")].iloc[0]
    assert failure.issue == "Station analysis incomplete"
    assert failure.action == "exclude"
    assert "unparseable timestamp" in failure.note


def test_station_missing_from_pack_is_not_silently_ignored(pack, tmp_path):
    (pack / "stations.csv").write_text("station_id,name,logger_interval_min\nS99,Present,10\nS98,Absent,10\n")
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 2
    flags = pd.read_csv(tmp_path / "flags.csv")
    assert flags.station_id.tolist() == ["S98"]
    assert "no observation file" in flags.note.iloc[0]


def test_all_stations_failed_returns_fatal_exit(pack, tmp_path):
    (pack / "data" / "S99.csv").write_text("timestamp_utc\ninvalid\n")
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 1


def test_unreadable_or_unparseable_gps_does_not_claim_success(pack, tmp_path):
    (pack / "gps").mkdir()
    (pack / "gps" / "S99.nmea").write_text("not a GPS sentence\n")
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 1
    flags = pd.read_csv(tmp_path / "flags.csv")
    assert flags.issue.tolist() == ["Station analysis incomplete"]


def test_report_failure_is_reported_after_flags_are_written(pack, tmp_path, monkeypatch):
    def fail_report(*args):
        raise OSError("report write failed")
    monkeypatch.setattr(checker, "generate_html_report", fail_report)
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 2
    assert (tmp_path / "flags.csv").is_file()


def test_network_failure_is_visible_and_returns_partial_exit(pack, tmp_path, monkeypatch):
    from src.network_checks import NetworkCheckResult
    def fail_network(data, metadata, flags, configs, events):
        return NetworkCheckResult(flags, {"S99": "peer evidence failed"})
    monkeypatch.setattr(checker, "run_network_checks", fail_network)
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 2
    flags = pd.read_csv(tmp_path / "flags.csv")
    assert flags.issue.tolist() == ["Station analysis incomplete"]
    assert "peer evidence failed" in flags.note.iloc[0]


def test_sensor_check_failure_is_not_swallowed(pack, tmp_path, monkeypatch):
    def fail_check(*args):
        raise RuntimeError("check failed")
    monkeypatch.setattr(checks, "check_flatlines", fail_check)
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 1
    flags = pd.read_csv(tmp_path / "flags.csv")
    assert flags.issue.tolist() == ["Station analysis incomplete"]


def test_ingestion_preserves_source_order_and_csv_row_numbers(pack):
    path = pack / "data" / "S99.csv"
    source = pd.read_csv(path).iloc[[0, 3, 1, 2]]
    source.to_csv(path, index=False)
    loaded, _ = load_station_data("S99", path, CheckerConfig())
    assert loaded.timestamp_utc.tolist() == pd.to_datetime(source.timestamp_utc).tolist()
    assert loaded.source_row.tolist() == [2, 3, 4, 5]


def test_invalid_timestamp_is_not_silently_dropped(pack):
    path = pack / "data" / "S99.csv"
    source = pd.read_csv(path)
    source.loc[3, "timestamp_utc"] = "invalid"
    source.to_csv(path, index=False)
    loaded, errors = load_station_data("S99", path, CheckerConfig())
    assert loaded is None
    assert "refusing to silently discard" in errors[0]


def test_ingestion_does_not_silently_stop_at_one_hundred_thousand_rows(tmp_path):
    path = tmp_path / "S99.csv"
    frame = pd.DataFrame({"timestamp_utc": pd.date_range("2026-01-10", periods=100_001, freq="10min"),
                          "temp_c": 10.0})
    frame.to_csv(path, index=False, date_format=TS_FMT)
    loaded, _ = load_station_data("S99", path, CheckerConfig())
    assert len(loaded) == 100_001
    assert loaded.timestamp_utc.iloc[-1] == frame.timestamp_utc.iloc[-1]


def test_station_metadata_cadence_controls_gap_detection(pack, tmp_path):
    path = pack / "data" / "S99.csv"
    source = pd.read_csv(path)
    source.timestamp_utc = pd.date_range("2026-01-10 15:00", periods=len(source), freq="30min").strftime(TS_FMT)
    source.to_csv(path, index=False)
    (pack / "stations.csv").write_text("station_id,name,logger_interval_min\nS99,Half-hour station,30\n")
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 0
    flags = pd.read_csv(tmp_path / "flags.csv")
    assert flags.empty


def test_cli_clock_correction_has_no_station_specific_assumptions(pack, tmp_path, station_frame, nmea_sentence):
    truth = pd.date_range("2026-08-20", periods=43, freq="10min")
    raw = station_frame(truth)
    for channel, base in {"rh_pct": 50, "pressure_hpa": 850, "wind_kmh": 5}.items():
        raw[channel] = base + raw.index * 0.01
    raw.loc[19:, "timestamp_utc"] -= pd.Timedelta(hours=2)
    path = pack / "data" / "S99.csv"
    raw.to_csv(path, index=False, date_format=TS_FMT)
    before = path.read_bytes()
    (pack / "gps").mkdir()
    lines = []
    for hour, time in enumerate(pd.date_range(truth[0], periods=8, freq="1h")):
        logger = time - pd.Timedelta(hours=2 if hour >= 4 else 0)
        body = f"GPRMC,{time.strftime('%H%M%S')}.00,A,4000.0000,N,10500.0000,W,0,0,{time.strftime('%d%m%y')},,,A"
        lines.append(f"{logger.strftime('%Y-%m-%d %H:%M:%S')},{nmea_sentence(body)}")
    (pack / "gps" / "S99.nmea").write_text("\n".join(lines) + "\n")
    assert checker.main([str(pack), "--output-dir", str(tmp_path)]) == 0
    flags = pd.read_csv(tmp_path / "flags.csv")
    assert flags.issue.tolist() == ["GPS-confirmed logger clock offset"]
    assert "add +120 minutes" in flags.note.iloc[0]
    assert path.read_bytes() == before


@pytest.mark.parametrize("overrides", [
    {"gps_clock_min_fixes": 1}, {"gps_clock_min_fixes": True},
    {"gps_clock_tolerance_seconds": 30}, {"gps_clock_max_gap_hours": 0},
    {"sampling_interval_min": -10}, {"gps_jump_tolerance_km": float("nan")},
    {"gps_clock_tolerance_second": 5},
])
def test_invalid_clock_configuration_is_rejected(tmp_path, overrides):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(overrides))
    with pytest.raises(ValueError):
        CheckerConfig.from_file(str(path))


def test_invalid_config_is_a_clean_cli_error(pack, tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"gps_clock_min_fixes": 1}')
    assert checker.main([str(pack), "--config", str(path), "--output-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "flags.csv").exists()


def test_default_configs_do_not_share_mutable_bounds():
    first, second = CheckerConfig(), CheckerConfig()
    first.physical_bounds["temp_c"]["min"] = -999
    assert second.physical_bounds["temp_c"]["min"] == -90


def test_bad_gps_packets_do_not_invalidate_weather_or_dominate_visit_ranking(station_frame):
    frame = station_frame(pd.date_range("2026-01-10", periods=8, freq="10min"))
    flags = pd.DataFrame([
        {"station_id": "S98", "channel": "metadata", "action": "exclude", "issue": "GPS checksum failure",
         "start_utc": frame.timestamp_utc.iloc[0], "end_utc": frame.timestamp_utc.iloc[0]}
    ] * 100 + [
        {"station_id": "S99", "channel": "temp_c", "action": "exclude", "issue": "Sensor failure",
         "start_utc": frame.timestamp_utc.iloc[0], "end_utc": frame.timestamp_utc.iloc[1]}
    ])
    summaries = compute_station_summaries({"S98": frame, "S99": frame}, flags, pd.DataFrame())
    assert summaries["S98"].overall_status == "Good"
    assert summaries["S98"].exclude_flags == []
    assert len(summaries["S98"].metadata_flags) == 100
    assert recommend_technician_visit(summaries)[0] == "S99"
