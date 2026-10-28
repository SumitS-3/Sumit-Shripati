import pandas as pd
import pytest

from src.network_health import compute_station_summaries, recommend_technician_visit
from src.temporal import prepare_observations
from src.config import CheckerConfig


def flag(frame, start=0, end=None, channel="temp_c", action="exclude", issue="Sensor failure", station="S90"):
    return {"station_id": station, "channel": channel, "action": action, "issue": issue,
            "start_utc": frame.timestamp_utc.iloc[start],
            "end_utc": frame.timestamp_utc.iloc[start if end is None else end], "note": "Independent test evidence."}


def summarize(data, flags=(), metadata=None):
    return compute_station_summaries(data, pd.DataFrame(flags), pd.DataFrame() if metadata is None else metadata)


def test_overlap_and_all_channel_exclusions_are_counted_once(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=6, freq="10min"))
    flags = [flag(frame, 1, 3), flag(frame, 2, 4, channel="all")]
    summary = summarize({"S90": frame}, flags)["S90"]
    assert summary.channels["temp_c"].excluded_records == 4
    assert summary.channels["temp_c"].usable_records == 2
    assert summary.channels["rh_pct"].excluded_records == 3
    assert summary.channels["temp_c"].usable_pct == pytest.approx(100 / 3)


def test_missing_times_do_not_disappear_from_denominator(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    missing = frame.drop(index=[2, 3]).reset_index(drop=True)
    summary = summarize({"S90": missing})["S90"]
    coverage = summary.channels["temp_c"]
    assert coverage.total_records == 8
    assert coverage.missing_records == 2
    assert coverage.excluded_records == 0
    assert coverage.usable_pct == 75


def test_missing_edges_use_common_network_window(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    partial = frame.iloc[2:6].reset_index(drop=True)
    summary = summarize({"S90": partial, "S91": frame})["S90"]
    assert summary.expected_records == 8
    assert summary.channels["temp_c"].missing_records == 4
    assert summary.has_missing_tail
    assert recommend_technician_visit({"S90": summary}) == ("S90", None)


def test_missing_and_excluded_are_disjoint(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    partial = frame.drop(index=[2, 3]).reset_index(drop=True)
    summary = summarize({"S90": partial}, [flag(frame, 1, 4)])["S90"]
    coverage = summary.channels["temp_c"]
    assert coverage.missing_records == 2
    assert coverage.excluded_records == 2
    assert coverage.usable_records == 4


def test_missing_values_are_unusable_even_without_a_flag(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    frame.loc[3, "temp_c"] = float("nan")
    coverage = summarize({"S90": frame})["S90"].channels["temp_c"]
    assert coverage.excluded_records == 1
    assert coverage.missing_records == 0
    assert coverage.usable_records == 7


def test_keep_does_not_reduce_coverage_or_count_as_uncertainty(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    summary = summarize({"S90": frame}, [flag(frame, 0, 7, action="keep")])["S90"]
    assert summary.channels["temp_c"].usable_records == 8
    assert summary.overall_status == "Good"


def test_raw_correct_label_does_not_claim_treatment_has_happened(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    correction = flag(frame, 2, 5, action="correct", channel="all", issue="GPS-confirmed logger clock offset")
    summary = summarize({"S90": frame}, [correction])["S90"]
    assert summary.channels["temp_c"].pending_records == 4
    assert summary.channels["temp_c"].usable_records == 4
    assert summary.overall_status == "Treatment pending"


def test_applied_clock_shift_counts_the_corrected_timeline(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    frame["logger_timestamp_utc"] = frame.timestamp_utc
    frame.loc[4:, "logger_timestamp_utc"] -= pd.Timedelta(hours=2)
    frame.attrs["treatments_applied"] = ("clock",)
    correction = flag(frame, 4, 7, action="correct", channel="all", issue="GPS-confirmed logger clock offset")
    summary = summarize({"S90": frame}, [correction])["S90"]
    assert summary.channels["temp_c"].usable_records == 8
    assert summary.channels["temp_c"].corrected_records == 4
    assert summary.clock_adjustments[0].minutes == 120
    assert summary.clock_adjustments[0].records == 4


def test_replay_removal_is_not_missing_or_pending_data(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    replay = pd.concat([frame, frame.iloc[2:5]], ignore_index=True)
    prepared, candidates = prepare_observations(replay, [], CheckerConfig())
    summary = summarize({"S90": prepared}, [{"station_id": "S90", **candidate} for candidate in candidates])["S90"]
    assert summary.replayed_records == 3
    assert summary.channels["temp_c"].usable_records == 8
    assert summary.channels["temp_c"].pending_records == 0
    assert summary.expected_records == 8


def test_failed_station_remains_present_and_held(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    failure = {"station_id": "S91", "channel": "metadata", "action": "exclude", "issue": "Station analysis incomplete", "note": "No data file."}
    summaries = summarize({"S90": frame}, [failure])
    assert summaries["S91"].analysis_failed
    assert summaries["S91"].overall_status == "Hold"
    assert summaries["S91"].channels["temp_c"].usable_records == 0
    assert recommend_technician_visit(summaries) == (None, None)


def test_wind_direction_is_included_in_quality_decisions(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    summary = summarize({"S90": frame}, [flag(frame, 3, channel="wind_dir_deg")])["S90"]
    assert summary.channels["wind_dir_deg"].usable_records == 7
    assert summary.channels["wind_kmh"].usable_records == 8


def test_different_cadences_and_logging_phases_do_not_create_phantom_gaps(station_frame):
    full = station_frame(pd.date_range("2026-01-01", periods=13, freq="10min"))
    half_hour = station_frame(pd.date_range("2026-01-01", periods=5, freq="30min"))
    offset = station_frame(pd.date_range("2026-01-01 00:05", periods=12, freq="10min"))
    metadata = pd.DataFrame([{"station_id": "S91", "logger_interval_min": 30}])
    summaries = summarize({"S90": full, "S91": half_hour, "S92": offset}, metadata=metadata)
    assert summaries["S91"].expected_records == 5
    assert summaries["S91"].channels["temp_c"].usable_pct == 100
    assert summaries["S92"].expected_records == 12
    assert summaries["S92"].channels["temp_c"].usable_pct == 100


def test_installation_date_crops_expected_window(station_frame):
    full = station_frame(pd.date_range("2026-01-01", periods=3 * 144, freq="10min"))
    later = full.iloc[144:].reset_index(drop=True)
    metadata = pd.DataFrame([{"station_id": "S91", "installed": "2026-01-02"}])
    summary = summarize({"S90": full, "S91": later}, metadata=metadata)["S91"]
    assert summary.expected_records == 288
    assert summary.channels["temp_c"].missing_records == 0


def test_any_restriction_prevents_unqualified_good_status(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=100, freq="10min"))
    summary = summarize({"S90": frame}, [flag(frame, 5)])["S90"]
    assert summary.channels["temp_c"].usable_pct == 99
    assert summary.overall_status == "Use selected data"


def test_visit_priority_ignores_fragmentation_and_historical_gaps(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=3 * 144, freq="10min"))
    data = {sid: frame for sid in ("S90", "S91", "S92")}
    thermal = flag(frame, 300, 400, issue="Persistent sunlight-linked temperature bias")
    wind = flag(frame, 144, 431, channel="wind_kmh", issue="Wind scale changed after replacement", station="S91")
    gap = flag(frame, 0, 24, channel="all", issue="Data gap: 240 min", station="S92")
    first = recommend_technician_visit(summarize(data, [thermal, wind, gap]))
    duplicated = recommend_technician_visit(summarize(data, [thermal, *([wind] * 100), gap]))
    assert first == duplicated == ("S90", "S91")


def test_historical_sensor_failure_does_not_force_a_visit(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=10 * 144, freq="10min"))
    old = flag(frame, 10, 100, issue="Flatline: temp_c")
    assert recommend_technician_visit(summarize({"S90": frame}, [old])) == (None, None)


def test_empty_failed_pack_does_not_invent_a_period():
    failure = {"station_id": "S90", "channel": "metadata", "action": "exclude", "issue": "Station analysis incomplete"}
    summary = summarize({}, [failure])["S90"]
    assert summary.period_start is None and summary.period_end is None
    assert summary.expected_records == 0 and summary.analysis_failed


def test_outside_window_flag_does_not_invent_loss_or_a_visit(station_frame):
    frame = station_frame(pd.date_range("2026-01-01", periods=8, freq="10min"))
    future = flag(frame, 0, 7, issue="Wind scale changed after replacement", channel="wind_kmh")
    future["start_utc"] += pd.Timedelta(days=100)
    future["end_utc"] += pd.Timedelta(days=100)
    summaries = summarize({"S90": frame}, [future])
    assert summaries["S90"].channels["wind_kmh"].usable_pct == 100
    assert recommend_technician_visit(summaries) == (None, None)
