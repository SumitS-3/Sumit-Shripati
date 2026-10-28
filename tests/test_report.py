import pandas as pd
import pytest

from src.network_health import compute_station_summaries, recommend_technician_visit
from src.output import atomic_text
from src.report import generate_html_report


def render(tmp_path, data, flags=(), metadata=None, title="A local network"):
    metadata = pd.DataFrame() if metadata is None else metadata
    summaries = compute_station_summaries(data, pd.DataFrame(flags), metadata)
    first, second = recommend_technician_visit(summaries)
    destination = tmp_path / "report.html"
    generate_html_report(summaries, list(flags), first, second, str(destination), title)
    return destination.read_text(encoding="utf-8")


def test_report_period_and_title_come_from_inputs(tmp_path, station_frame):
    frame = station_frame(pd.date_range("2027-09-04", periods=8, freq="10min"))
    text = render(tmp_path, {"S90": frame}, title="A different weather network")
    assert "04 Sep 2027" in text and "2025-03-01" not in text
    assert "A different weather network" in text
    assert "No station trip justified yet" in text
    assert 'data-channel="wind_dir_deg"' in text


def test_report_escapes_untrusted_pack_text(tmp_path, station_frame):
    frame = station_frame(pd.date_range("2027-01-01", periods=8, freq="10min"))
    metadata = pd.DataFrame([{"station_id": "S90", "name": '<script>alert("site")</script>'}])
    text = render(tmp_path, {"S90": frame}, metadata=metadata, title="<img src=x onerror=alert(1)>")
    assert '<script>' not in text and '<img src=x' not in text
    assert '&lt;script&gt;' in text and '&lt;img' in text


def test_failed_station_is_not_presented_as_usable(tmp_path):
    failure = {"station_id": "S90", "channel": "metadata", "action": "exclude", "issue": "Station analysis incomplete"}
    text = render(tmp_path, {}, [failure])
    assert "Hold all channels" in text
    assert "Observation window unavailable" in text
    assert "No station trip justified yet" in text


def test_large_report_limits_station_rows_and_discloses_omissions(tmp_path, station_frame):
    frame = station_frame(pd.date_range("2027-01-01", periods=8, freq="10min"))
    data = {f"S{index:03d}": frame for index in range(100)}
    text = render(tmp_path, data)
    assert text.count('<th scope="row">') == 12
    assert "Showing 12 of 100 stations" in text


def test_report_explains_actual_fault_and_runner_up(tmp_path, station_frame):
    frame = station_frame(pd.date_range("2027-01-01", periods=3 * 144, freq="10min"))
    flags = [
        {"station_id": "S90", "channel": "temp_c", "issue": "Persistent sunlight-linked temperature bias", "action": "exclude",
         "start_utc": frame.timestamp_utc.iloc[144], "end_utc": frame.timestamp_utc.iloc[-1]},
        {"station_id": "S91", "channel": "wind_kmh", "issue": "Wind scale changed after replacement", "action": "exclude",
         "start_utc": frame.timestamp_utc.iloc[144], "end_utc": frame.timestamp_utc.iloc[-1]},
    ]
    text = render(tmp_path, {"S90": frame, "S91": frame}, flags)
    assert "Visit S90" in text and "Why not S91 first?" in text
    assert "Check airflow and the radiation shield" in text
    assert "0–10 min" in text and "40–60 min" in text
    assert "sensor label" in text


def test_atomic_output_failure_preserves_existing_deliverable(tmp_path):
    path = tmp_path / "report.html"
    path.write_text("previous valid report", encoding="utf-8")
    with pytest.raises(RuntimeError):
        with atomic_text(path) as stream:
            stream.write("partial replacement")
            raise RuntimeError("render failed")
    assert path.read_text(encoding="utf-8") == "previous valid report"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_output_does_not_follow_a_file_symlink(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("original data", encoding="utf-8")
    output = tmp_path / "report.html"
    try:
        output.symlink_to(source)
    except OSError:
        pytest.skip("Creating file symlinks is not permitted on this platform")
    with atomic_text(output) as stream:
        stream.write("new report")
    assert source.read_text(encoding="utf-8") == "original data"
    assert output.read_text(encoding="utf-8") == "new report"
    assert not output.is_symlink()
