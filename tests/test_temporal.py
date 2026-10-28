from datetime import timedelta

import pandas as pd
import pytest

from src.checks import run_all_checks
from src.config import CheckerConfig
from src.gps import ClockSample
from src.temporal import prepare_observations


def clock_samples(times, correction_minutes):
    return [ClockSample((time - pd.Timedelta(minutes=offset)).to_pydatetime(),
                        time.to_pydatetime(), offset * 60)
            for time, offset in zip(times, correction_minutes)]


@pytest.mark.parametrize("shift", [120, -120])
def test_gps_and_source_break_repair_backward_and_forward_clocks(station_frame, shift):
    truth = pd.date_range("2026-01-10", periods=43, freq="10min")
    raw = station_frame(truth)
    raw.loc[19:, "timestamp_utc"] -= pd.Timedelta(minutes=shift)
    before = raw.copy(deep=True)
    gps_times = pd.date_range(truth[0], periods=8, freq="1h")
    samples = clock_samples(gps_times, [0] * 4 + [shift] * 4)
    prepared, flags = prepare_observations(raw, samples, CheckerConfig())
    assert prepared.timestamp_utc.tolist() == truth.tolist()
    pd.testing.assert_frame_equal(raw, before)
    assert prepared.source_row.tolist() == list(range(2, 45))
    assert prepared.loc[19, "logger_timestamp_utc"] == raw.loc[19, "timestamp_utc"]
    assert len(flags) == 1
    flag = flags[0]
    assert flag["action"] == "correct"
    assert f"add {shift:+g} minutes" in flag["note"]
    assert "source CSV rows 21-44" in flag["note"]
    assert flag["start_utc"] == raw.timestamp_utc.iloc[19]


def test_clock_repair_precedes_replay_detection_even_when_values_match(station_frame):
    truth = pd.date_range("2026-01-10", periods=43, freq="10min")
    raw = station_frame(truth)
    raw["temp_c"] = 10.0
    raw.loc[19:, "timestamp_utc"] -= pd.Timedelta(hours=2)
    samples = clock_samples(pd.date_range(truth[0], periods=8, freq="1h"), [0] * 4 + [120] * 4)
    prepared, flags = prepare_observations(raw, samples, CheckerConfig())
    assert len(prepared) == len(truth)
    assert prepared.timestamp_utc.tolist() == truth.tolist()
    assert not any(flag["issue"] == "Exact observation replay" for flag in flags)


def test_multiple_clock_changes_are_matched_in_source_order(station_frame):
    truth = pd.date_range("2026-01-10", periods=73, freq="10min")
    raw = station_frame(truth)
    raw.loc[19:42, "timestamp_utc"] -= pd.Timedelta(hours=2)
    raw.loc[43:, "timestamp_utc"] += pd.Timedelta(hours=1)
    samples = clock_samples(pd.date_range(truth[0], periods=13, freq="1h"),
                            [0] * 4 + [120] * 4 + [-60] * 5)
    prepared, flags = prepare_observations(raw, samples, CheckerConfig())
    assert prepared.timestamp_utc.tolist() == truth.tolist()
    assert len(flags) == 2
    assert all(flag["action"] == "correct" for flag in flags)


def test_constant_offset_from_start_can_cross_the_date_boundary(station_frame):
    truth = pd.date_range("2026-01-11", periods=19, freq="10min")
    raw = station_frame(truth - pd.Timedelta(hours=2))
    samples = clock_samples(pd.date_range(truth[0], periods=4, freq="1h"), [120] * 4)
    prepared, flags = prepare_observations(raw, samples, CheckerConfig())
    assert prepared.timestamp_utc.tolist() == truth.tolist()
    assert flags[0]["start_utc"].day == 10


@pytest.mark.parametrize("problem", ["too_few_fixes", "repeated_fixes", "drift", "gps_outage",
                                      "no_matching_break", "extrapolation", "gps_out_of_order"])
def test_unproven_clock_corrections_are_withheld(station_frame, problem):
    truth = pd.date_range("2026-01-10", periods=43, freq="10min")
    raw = station_frame(truth)
    raw.loc[19:, "timestamp_utc"] -= pd.Timedelta(hours=2)
    times = pd.date_range(truth[0], periods=8, freq="1h")
    samples = clock_samples(times, [0] * 4 + [120] * 4)
    if problem == "too_few_fixes":
        samples = samples[:5]
    elif problem == "repeated_fixes":
        samples = samples[:4] + [samples[4]] * 3
    elif problem == "drift":
        samples[4] = ClockSample(samples[4].logger_utc, samples[4].gps_utc, None)
    elif problem == "gps_outage":
        samples[5] = ClockSample(samples[5].logger_utc + timedelta(hours=4),
                                 samples[5].gps_utc + timedelta(hours=4), 7200)
    elif problem == "gps_out_of_order":
        samples[4], samples[5] = samples[5], samples[4]
    elif problem == "no_matching_break":
        raw = station_frame(truth)
    elif problem == "extrapolation":
        raw.loc[len(raw)] = raw.iloc[-1]
        raw.loc[len(raw) - 1, "timestamp_utc"] += pd.Timedelta(days=1)
    prepared, flags = prepare_observations(raw, samples, CheckerConfig())
    assert prepared.empty
    assert len(flags) == 1
    assert flags[0]["issue"] == "Unresolved logger clock evidence"
    assert flags[0]["action"] == "exclude"
    assert "no timestamp correction was applied" in flags[0]["note"]


def test_exact_replay_retains_first_source_occurrence(station_frame):
    raw = station_frame(pd.date_range("2026-01-10", periods=8, freq="10min"))
    replay = pd.concat([raw, raw.iloc[2:5]], ignore_index=True)
    prepared, flags = prepare_observations(replay, [], CheckerConfig())
    assert len(prepared) == len(raw)
    assert prepared.source_row.tolist() == list(range(2, 10))
    assert flags[0]["issue"] == "Exact observation replay"
    assert "Remove 3 replayed rows at 3 timestamps" in flags[0]["note"]
    assert flags[0]["action"] == "correct"


def test_conflicting_duplicates_are_not_guessed_away(station_frame):
    raw = station_frame(pd.date_range("2026-01-10", periods=8, freq="10min"))
    conflicting = raw.iloc[[3]].copy()
    conflicting["temp_c"] = 15.0
    replay = pd.concat([raw, conflicting], ignore_index=True)
    prepared, flags = prepare_observations(replay, [], CheckerConfig())
    assert len(prepared) == 7
    assert not prepared.timestamp_utc.eq(raw.timestamp_utc.iloc[3]).any()
    assert flags[0]["issue"] == "Conflicting duplicate timestamps"
    assert flags[0]["action"] == "exclude"


def test_internal_bookkeeping_columns_are_not_sensor_channels(station_frame):
    raw = station_frame(pd.date_range("2026-01-10", periods=8, freq="10min"))
    prepared, _ = prepare_observations(raw, [], CheckerConfig())
    prepared["source_row"] = None
    prepared["logger_timestamp_utc"] = None
    assert run_all_checks("S99", prepared, CheckerConfig()) == []


def test_shared_intervals_honor_a_different_logging_cadence(station_frame):
    times = pd.date_range("2026-01-10", periods=6, freq="30min")
    raw = station_frame(times)
    replay = pd.concat([raw, raw.iloc[2:4]], ignore_index=True)
    prepared, flags = prepare_observations(replay, [], CheckerConfig(sampling_interval_min=30, gap_threshold_min=30))
    assert len(flags) == 1
    assert flags[0]["start_utc"] == times[2]
    assert flags[0]["end_utc"] == times[3]
    assert prepared.timestamp_utc.tolist() == times.tolist()
