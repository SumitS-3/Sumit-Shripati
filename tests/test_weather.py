import numpy as np
import pandas as pd
import pytest

from src.checks import run_all_checks
from src.config import CheckerConfig
from src.evidence import interval_mask
from src.maintenance import parse_maintenance_log
from src.network_checks import PeerIndex, run_network_checks
from src.solar import interval_sun_masks, solar_elevation


@pytest.fixture
def network():
    times = pd.date_range("2026-07-01", periods=20 * 144, freq="10min")
    hours = np.arange(len(times)) / 6
    sun = np.maximum(0, np.sin((times.hour.to_numpy() + times.minute.to_numpy() / 60 - 6) * np.pi / 12))
    data, metadata = {}, {}
    for number in range(5):
        sid = f"S{90 + number}"
        data[sid] = pd.DataFrame({
            "timestamp_utc": times, "temp_c": 20 + 5 * np.sin(hours * np.pi / 12) + number / 5,
            "rh_pct": 60 + 5 * np.cos(hours * np.pi / 12),
            "pressure_hpa": 850 + number * 10 + np.sin(hours / 12),
            "wind_kmh": 10 + np.sin(hours / 3), "wind_dir_deg": 180,
            "rain_mm": 0.0, "ghi_wm2": sun * 700,
        })
        metadata[sid] = {"lat": 0.0, "lon": number / 10, "elevation_m": number * 10}
    return data, metadata


def evaluate(network, notes="", cfg=None):
    data, metadata = network
    cfg = cfg or CheckerConfig()
    configs = {sid: cfg for sid in data}
    local = {sid: run_all_checks(sid, frame, cfg, metadata[sid]["lat"], metadata[sid]["lon"])
             for sid, frame in data.items()}
    result = run_network_checks(data, metadata, local, configs, parse_maintenance_log(notes))
    assert not result.failures, result.failures
    return result.flags


def channel_flags(results, channel, action=None):
    return [flag for flag in results["S90"] if flag["channel"] == channel and
            (action is None or flag["action"] == action)]


def test_station_pressure_offset_is_not_confused_with_sensor_failure(network):
    network[0]["S90"]["pressure_hpa"] -= 100
    assert channel_flags(evaluate(network), "pressure_hpa") == []


def test_persistent_pressure_step_is_withheld_without_inventing_offset(network):
    target = network[0]["S90"]
    target.loc[target.timestamp_utc >= "2026-07-11 12:00", "pressure_hpa"] += 6
    flags = channel_flags(evaluate(network), "pressure_hpa")
    assert len(flags) == 1
    assert flags[0]["issue"] == "Pressure baseline shift"
    assert flags[0]["start_utc"] == pd.Timestamp("2026-07-11 12:00")
    assert flags[0]["end_utc"] == target.timestamp_utc.iloc[-1]
    assert flags[0]["action"] == "exclude"


def test_shared_pressure_jump_is_retained_as_weather(network):
    for target in network[0].values():
        target.loc[target.timestamp_utc >= "2026-07-11 12:00", "pressure_hpa"] += 6
    flags = channel_flags(evaluate(network), "pressure_hpa")
    assert len(flags) == 1
    assert flags[0]["action"] == "keep"
    assert flags[0]["start_utc"] == flags[0]["end_utc"]


def test_supersaturation_is_not_capped_and_failure_includes_night(network):
    target = network[0]["S90"]
    mask = target.timestamp_utc.between("2026-07-05 12:00", "2026-07-09 00:00")
    target.loc[mask, "rh_pct"] = np.linspace(99, 104, mask.sum())
    flags = channel_flags(evaluate(network), "rh_pct", "exclude")
    assert len(flags) == 1
    assert flags[0]["start_utc"] == pd.Timestamp("2026-07-05 12:00")
    assert flags[0]["end_utc"] == pd.Timestamp("2026-07-09")
    assert "including night" in flags[0]["note"]


def test_long_fog_supported_by_peers_is_retained(network):
    for sid, target in network[0].items():
        mask = target.timestamp_utc.between("2026-07-05", "2026-07-07")
        target.loc[mask, "rh_pct"] = 100 if sid == "S90" else 95
    flags = channel_flags(evaluate(network), "rh_pct")
    assert flags and all(flag["action"] == "keep" for flag in flags)


def test_median_dryness_does_not_claim_three_dry_votes(network):
    for sid, target in network[0].items():
        target["rh_pct"] = 100 if sid == "S90" else (50 if sid in ("S91", "S92") else 95)
    assert channel_flags(evaluate(network), "rh_pct", "exclude") == []


def test_saturation_without_eligible_peers_does_not_prove_blockage(network):
    network[0]["S90"]["rh_pct"] = 100
    for sid in network[1]:
        if sid != "S90":
            network[1][sid]["elevation_m"] = 3000
    assert channel_flags(evaluate(network), "rh_pct") == []


def test_wind_scale_change_requires_evidence_and_never_assumes_units(network):
    target = network[0]["S90"]
    target.loc[target.timestamp_utc >= "2026-07-11 12:00", "wind_kmh"] *= 0.3
    notes = "2026-07-11  S90  Fitted replacement anemometer from a different manufacturer."
    flags = channel_flags(evaluate(network, notes), "wind_kmh")
    assert len(flags) == 1 and flags[0]["action"] == "exclude"
    assert "factor of 0.30" in flags[0]["note"]
    assert "without assuming a unit conversion" in flags[0]["note"]
    assert flags[0]["start_utc"] <= pd.Timestamp("2026-07-11 12:00")


def test_documented_wind_replacement_without_scale_change_is_not_flagged(network):
    network[0]["S90"]["wind_kmh"] *= 2  # A fixed exposure difference is not a change.
    notes = "2026-07-11  S90  Fitted replacement anemometer."
    assert channel_flags(evaluate(network, notes), "wind_kmh") == []


def test_daylight_temperature_bias_is_withheld_while_nights_remain_usable(network):
    target = network[0]["S90"]
    sunny = target.ghi_wm2 > 400
    target.loc[sunny & target.timestamp_utc.ge("2026-07-11"), "temp_c"] += 4
    notes = "2026-07-11  S90  Repainted the temperature shield mounting post."
    flags = channel_flags(evaluate(network, notes), "temp_c", "exclude")
    assert flags
    excluded = interval_mask(target.timestamp_utc, flags, "temp_c")
    assert excluded.any()
    _, daylight = interval_sun_masks(target.timestamp_utc, 0, 0, 10)
    assert not (excluded & ~daylight).any()
    assert all(flag["issue"] == "Persistent sunlight-linked temperature bias" for flag in flags)


def test_local_warming_shared_by_day_and_night_is_not_called_solar_bias(network):
    target = network[0]["S90"]
    target.loc[target.timestamp_utc.ge("2026-07-11"), "temp_c"] += 4
    notes = "2026-07-11  S90  Repainted the temperature shield mounting post."
    assert channel_flags(evaluate(network, notes), "temp_c", "exclude") == []


def test_late_thermal_confirmation_does_not_certify_earlier_post_work_data(network):
    target = network[0]["S90"]
    target.loc[target.timestamp_utc.ge("2026-07-17") & target.ghi_wm2.gt(400), "temp_c"] += 4
    notes = "2026-07-11  S90  Repainted the temperature shield mounting post."
    flags = channel_flags(evaluate(network, notes), "temp_c", "exclude")
    assert flags[0]["start_utc"].date() == pd.Timestamp("2026-07-11").date()
    assert "onset" in flags[0]["note"] and "unknown" in flags[0]["note"]


def test_single_warm_day_does_not_establish_persistent_bias(network):
    target = network[0]["S90"]
    mask = target.timestamp_utc.between("2026-07-11", "2026-07-11 23:50") & target.ghi_wm2.gt(400)
    target.loc[mask, "temp_c"] += 4
    notes = "2026-07-11  S90  Repainted the temperature shield mounting post."
    assert channel_flags(evaluate(network, notes), "temp_c", "exclude") == []


def test_rain_blockage_and_cleaning_drainage_are_not_redistributed(network):
    for sid, target in network[0].items():
        if sid != "S90":
            target.loc[target.timestamp_utc.isin(pd.to_datetime(["2026-07-08 12:00", "2026-07-09 12:00"])), "rain_mm"] = 2
    target = network[0]["S90"]
    target.loc[target.timestamp_utc.eq("2026-07-04 12:00"), "rain_mm"] = 0.2
    target.loc[target.timestamp_utc.between("2026-07-11 12:00", "2026-07-11 12:20"), "rain_mm"] = 6
    notes = "2026-07-11  S90  Rain gauge funnel was blocked with leaves; cleaned it."
    flags = channel_flags(evaluate(network, notes), "rain_mm")
    assert len(flags) == 1
    assert flags[0]["start_utc"] == pd.Timestamp("2026-07-04 12:10")
    assert flags[0]["end_utc"] == pd.Timestamp("2026-07-11 12:20")
    assert flags[0]["action"] == "exclude"
    assert "onset is uncertain" in flags[0]["note"]


def test_documented_blockage_without_flush_has_an_explicit_uncertain_end(network):
    for sid, target in network[0].items():
        if sid != "S90":
            target.loc[target.timestamp_utc.isin(pd.to_datetime(["2026-07-08 12:00", "2026-07-09 12:00"])), "rain_mm"] = 2
    notes = "2026-07-11  S90  Rain gauge funnel was blocked with leaves; cleaned it."
    flags = channel_flags(evaluate(network, notes), "rain_mm")
    assert len(flags) == 1
    assert flags[0]["issue"] == "Blocked gauge; cleaning time uncertain"
    assert flags[0]["end_utc"] == pd.Timestamp("2026-07-12 23:50")
    assert "no observed drainage burst" in flags[0]["note"]
    assert "no earlier positive tip" in flags[0]["note"]


def test_dry_spell_and_negated_maintenance_notes_do_not_prove_rain_blockage(network):
    notes = "2026-07-11  S90  Rain gauge was not blocked; cleaned it as a precaution."
    assert channel_flags(evaluate(network, notes), "rain_mm") == []


def test_peer_masking_removes_invalid_values_and_caps_neighbors(network):
    data, metadata = network
    cfg = CheckerConfig(peer_count=3)
    flags = {sid: [] for sid in data}
    flags["S91"] = [{"channel": "temp_c", "action": "exclude", "start_utc": data["S91"].timestamp_utc.min(),
                     "end_utc": data["S91"].timestamp_utc.max()}]
    index = PeerIndex(data, metadata, flags, {sid: cfg for sid in data})
    evidence = index.reference("S90", "temp_c")
    assert len(evidence.stations) == 3
    assert evidence.count.eq(2).all()
    assert evidence.values.isna().all()


def test_overlapping_intervals_are_counted_once_and_metadata_is_ignored():
    times = pd.Series(pd.date_range("2026-01-01", periods=6, freq="10min"))
    flags = [{"channel": channel, "action": "exclude", "start_utc": times.iloc[start], "end_utc": times.iloc[end]}
             for channel, start, end in [("temp_c", 1, 3), ("all", 2, 4), ("metadata", 0, 5)]]
    assert interval_mask(times, flags, "temp_c").tolist() == [False, True, True, True, True, False]


def test_solar_screening_uses_location_and_season():
    times = pd.Series(pd.date_range("2026-06-21", periods=24, freq="1h"))
    night, daylight = interval_sun_masks(times, 80, 0, 60)
    assert not night.any() and daylight.all()
    winter_night, _ = interval_sun_masks(times + pd.DateOffset(months=6), 80, 0, 60)
    assert winter_night.all()
    west = solar_elevation(pd.to_datetime(["2026-07-01 12:00"]), 0, 0)
    east = solar_elevation(pd.to_datetime(["2026-07-01 12:00"]), 0, 180)
    assert west[0] > 60 and east[0] < -60


def test_sunset_spanning_interval_and_unknown_location_are_not_called_night():
    times = pd.Series(pd.to_datetime(["2026-07-01 19:00"]))
    night, _ = interval_sun_masks(times, 0, 0, 60)
    assert not night.any()
    unknown, day = interval_sun_masks(times, None, None, 60)
    assert not unknown.any() and not day.any()


def test_small_negative_nighttime_irradiance_is_not_a_sensor_failure(station_frame):
    frame = station_frame(pd.date_range("2026-07-01", periods=8, freq="10min"))
    frame["ghi_wm2"] = -3
    assert run_all_checks("S90", frame, CheckerConfig(), 0, 0) == []


@pytest.mark.parametrize("overrides", [
    {"peer_count": 1}, {"thermal_min_days": 1.5}, {"pressure_max_step_hpa": "3"},
    {"rh_dry_fraction": 1.1}, {"solar_night_elevation_deg": 30, "solar_day_elevation_deg": 10},
    {"flatline_min_records": {"temp_c": -1}}, {"flatline_exempt_zeros": ["unknown"]},
    {"wind_scale_change_ratio": 1}, {"sunny_ghi_wm2": float("inf")},
])
def test_invalid_weather_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        CheckerConfig(**overrides)


def test_partial_json_bounds_merge_with_defaults_and_config_roundtrips(tmp_path):
    import json
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"physical_bounds": {"temp_c": {"min": -80, "max": 60}},
                                "flatline_exempt_zeros": ["wind_kmh"]}))
    cfg = CheckerConfig.from_file(str(path))
    assert cfg.physical_bounds["temp_c"]["max"] == 60
    assert cfg.physical_bounds["pressure_hpa"]["min"] == 500
    path.write_text(json.dumps(cfg.to_dict()))
    assert CheckerConfig.from_file(str(path)).to_dict() == cfg.to_dict()


def test_benchmark_uses_solar_geometry_and_writes_no_deliverables(tmp_path, monkeypatch):
    from src.benchmark import _generate_synthetic_station, run_benchmark
    monkeypatch.chdir(tmp_path)
    frame = _generate_synthetic_station(n_records=288)
    night, _ = interval_sun_masks(frame.timestamp_utc, 40, -105, 10)
    assert frame.loc[night, "ghi_wm2"].eq(0).all()
    results = run_benchmark([2], n_records=288)
    assert results[0]["stations"] == 2 and results[0]["traced_mib"] > 0
    assert not (tmp_path / "flags.csv").exists()
    assert not (tmp_path / "report.html").exists()


def test_benchmark_cli_needs_no_data_pack(monkeypatch):
    import checker
    import src.benchmark
    calls = []
    monkeypatch.setattr(src.benchmark, "run_benchmark", lambda: calls.append(True))
    assert checker.main(["--benchmark"]) == 0
    assert calls == [True]


def test_one_failed_network_rule_does_not_fail_other_stations(network, monkeypatch):
    import src.network_checks as checks
    data, metadata = network
    cfg = CheckerConfig()
    local = {sid: [] for sid in data}
    notes = parse_maintenance_log("2026-07-11  S90  Fitted replacement anemometer.")
    def fail(*args):
        raise RuntimeError("wind evidence unavailable")
    monkeypatch.setattr(checks, "_wind", fail)
    result = checks.run_network_checks(data, metadata, local, {sid: cfg for sid in data}, notes)
    assert result.failures == {"S90": "wind evidence unavailable"}
    assert set(result.flags) == set(data)
