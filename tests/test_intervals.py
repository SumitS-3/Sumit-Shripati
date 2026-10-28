"""Regression cases for flags that used to cover healthy observations."""
import copy
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.checks import check_completeness, check_flatlines, check_physical_bounds
from src.config import CheckerConfig
from src.flagging import aggregate_flags, build_flag_rows
from src.temporal import prepare_observations


def observations(times, values=None):
    frame = pd.DataFrame({"timestamp_utc": pd.to_datetime(times)})
    frame["temp_c"] = values if values is not None else range(len(frame))
    frame["dup_ts"] = frame.duplicated("timestamp_utc", keep=False)
    return frame


def candidate(start, end=None, **overrides):
    flag = {
        "channel": "temp_c", "issue": "Suspect reading", "action": "exclude",
        "start_utc": pd.Timestamp(start), "end_utc": pd.Timestamp(end or start),
        "note": "Reading failed the check.",
    }
    flag.update(overrides)
    return flag


def test_missing_values_do_not_cover_healthy_readings():
    times = pd.date_range("2025-03-01", periods=8, freq="10min")
    frame = observations(times, [None, None, 12, 13, 14, 15, None, 17])
    flags = check_completeness(frame, CheckerConfig())
    assert [(f["start_utc"], f["end_utc"]) for f in flags] == [
        (times[0], times[1]), (times[6], times[6]),
    ]


def test_gap_flags_only_missing_slots():
    frame = observations(["2025-03-01 00:00", "2025-03-01 00:40"])
    flags = check_completeness(frame, CheckerConfig())
    gap = next(f for f in flags if f["channel"] == "all")
    assert gap["start_utc"] == pd.Timestamp("2025-03-01 00:10")
    assert gap["end_utc"] == pd.Timestamp("2025-03-01 00:30")
    assert "3 missing intervals" in gap["issue"]


def test_single_missing_sample_is_flagged():
    frame = observations(["2025-03-01 00:00", "2025-03-01 00:20"])
    flags = check_completeness(frame, CheckerConfig())
    assert len(flags) == 1
    assert flags[0]["start_utc"] == flags[0]["end_utc"] == pd.Timestamp("2025-03-01 00:10")


def test_separate_duplicate_incidents_and_triplicate_count():
    times = ["2025-03-01 00:00"] * 3 + ["2025-03-01 00:10"]
    times += ["2025-03-01 00:20"] * 2
    _, flags = prepare_observations(observations(times), [], CheckerConfig())
    duplicates = [f for f in flags if f["issue"] == "Conflicting duplicate timestamps"]
    assert len(duplicates) == 2
    assert "all 3 different observations at 1 shared timestamps" in duplicates[0]["note"]
    assert "all 2 different observations at 1 shared timestamps" in duplicates[1]["note"]
    assert all(f["start_utc"] == f["end_utc"] for f in duplicates)


@pytest.mark.parametrize("check,values,issue", [
    (check_completeness, [None, None], "Missing values"),
    (check_physical_bounds, [85, 85], "Out-of-range"),
])
def test_anomalies_on_either_side_of_gap_are_separate(check, values, issue):
    frame = observations(["2025-03-01 00:00", "2025-03-01 01:00"], values)
    flags = [f for f in check(frame, CheckerConfig()) if f["issue"].startswith(issue)]
    assert len(flags) == 2
    assert all(f["start_utc"] == f["end_utc"] for f in flags)


def test_flatline_duration_does_not_cross_missing_time():
    times = list(pd.date_range("2025-03-01 00:00", periods=6, freq="10min"))
    times += list(pd.date_range("2025-03-01 02:00", periods=6, freq="10min"))
    assert check_flatlines(observations(times, [6.5] * 12), CheckerConfig()) == []


def test_adjacent_flatlines_with_different_values_are_separate():
    times = pd.date_range("2025-03-01", periods=24, freq="10min")
    flags = check_flatlines(observations(times, [6.5] * 12 + [7.5] * 12), CheckerConfig())
    assert len(flags) == 2
    assert flags[0]["end_utc"] == times[11]
    assert flags[1]["start_utc"] == times[12]
    assert "6.5" in flags[0]["issue"] and "7.5" in flags[1]["issue"]


def test_flatline_duration_does_not_count_duplicate_timestamps():
    times = list(pd.date_range("2025-03-01", periods=6, freq="10min")) * 2
    frame = observations(sorted(times), [6.5] * 12)
    assert check_flatlines(frame, CheckerConfig()) == []


def test_merging_never_bridges_a_healthy_sample_even_with_large_config_gap():
    cfg = CheckerConfig(merge_gap_min=60)
    flags = [candidate("2025-03-01 00:00"), candidate("2025-03-01 00:20")]
    assert len(aggregate_flags(flags, cfg)) == 2


@pytest.mark.parametrize("overrides", [
    {"action": "keep"},
    {"issue": "Suspect reading with different evidence"},
    {"channel": "rh_pct"},
])
def test_merging_preserves_distinct_actions_issues_and_channels(overrides):
    flags = [candidate("2025-03-01 00:00"), candidate("2025-03-01 00:10", **overrides)]
    assert len(aggregate_flags(flags, CheckerConfig())) == 2


def test_nested_overlapping_intervals_never_shrink_and_keep_both_notes():
    flags = [
        candidate("2025-03-01 00:00", "2025-03-01 01:00"),
        candidate("2025-03-01 00:20", "2025-03-01 00:30", note="A second check agrees."),
    ]
    original = copy.deepcopy(flags)
    merged = aggregate_flags(flags, CheckerConfig())
    assert len(merged) == 1
    assert merged[0]["end_utc"] == pd.Timestamp("2025-03-01 01:00")
    assert "Reading failed" in merged[0]["note"]
    assert "A second check agrees" in merged[0]["note"]
    assert flags == original
    assert aggregate_flags(list(reversed(flags)), CheckerConfig()) == merged


def test_interleaved_different_issue_does_not_prevent_merging_same_problem():
    flags = [
        candidate("2025-03-01 00:00"),
        candidate("2025-03-01 00:00", issue="Other issue"),
        candidate("2025-03-01 00:10"),
    ]
    merged = aggregate_flags(flags, CheckerConfig())
    assert len(merged) == 2
    assert next(f for f in merged if f["issue"] == "Suspect reading")["end_utc"] == flags[2]["end_utc"]


def test_untimed_metadata_flags_are_never_merged():
    flags = [candidate(None, start_utc="", end_utc="", channel="metadata")] * 2
    rows = build_flag_rows("S01", flags, {}, CheckerConfig())
    assert len(rows) == 2
    assert all(row["start_utc"] == row["end_utc"] == "" for row in rows)


def test_cli_writes_outputs_in_working_directory_for_an_unseen_pack(tmp_path):
    pack = tmp_path / "another pack"
    (pack / "data").mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()
    frame = observations(pd.date_range("2026-01-10 15:00", periods=8, freq="10min"))
    frame["temp_c"] = [5, 6, 85, 7, 8, 9, 10, 11]
    for channel, value in {"rh_pct": 50, "pressure_hpa": 850, "wind_kmh": 5,
                           "wind_dir_deg": 180, "rain_mm": 0, "ghi_wm2": 300}.items():
        frame[channel] = value
    source = pack / "data" / "S99.csv"
    frame.drop(columns="dup_ts").to_csv(source, index=False, date_format="%Y-%m-%d %H:%M")
    before = source.read_bytes()
    checker = Path(__file__).resolve().parents[1] / "checker.py"
    result = subprocess.run([sys.executable, str(checker), str(pack)], cwd=output,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    flags = pd.read_csv(output / "flags.csv")
    assert flags.station_id.tolist() == ["S99"]
    assert flags.start_utc.tolist() == ["2026-01-10 15:20"]
    assert flags.end_utc.tolist() == ["2026-01-10 15:20"]
    assert flags.action.tolist() == ["exclude"]
    assert (output / "report.html").is_file()
    assert not (pack / "flags.csv").exists()
    assert source.read_bytes() == before
