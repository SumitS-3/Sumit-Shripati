from datetime import datetime

import pytest

from src.config import CheckerConfig
from src.gps import _dddmm_to_decimal, _nmea_checksum, analyze_gps, parse_nmea_file


def rmc(time="120000.00", date="100126", status="A", lat="4000.0000", mode="A"):
    return f"GPRMC,{time},{status},{lat},N,10500.0000,W,0.0,0.0,{date},,,{mode}"


def test_full_rmc_date_and_fractional_seconds_cross_midnight(tmp_path, nmea_sentence):
    path = tmp_path / "gps.nmea"
    path.write_text("2026-01-10 23:00:00," + nmea_sentence(rmc(time="010000.25", date="110126")) + "\n")
    fix = next(parse_nmea_file(path))
    assert fix.gps_utc == datetime(2026, 1, 11, 1, 0, 0, 250000)
    analysis = analyze_gps("S99", path, 40, -105, CheckerConfig())
    assert analysis.clock_samples[0].correction_seconds == 7200


def test_bad_checksum_and_invalid_status_cannot_influence_clock_or_position(tmp_path, nmea_sentence):
    path = tmp_path / "gps.nmea"
    lines = [
        "2026-01-10 12:00:00," + nmea_sentence(rmc()),
        "2026-01-10 06:00:00," + nmea_sentence(rmc(status="V", lat="2000.0000")),
        "2026-01-10 06:00:00," + nmea_sentence(rmc(status="V", lat="1000.0000"), False),
    ]
    path.write_text("\n".join(lines) + "\n")
    analysis = analyze_gps("S99", path, 40, -105, CheckerConfig())
    assert len(analysis.clock_samples) == 1
    assert analysis.clock_samples[0].correction_seconds == 0
    assert {flag["issue"] for flag in analysis.flags} == {"GPS checksum failure", "GPS fix not valid"}
    assert all("1 of 3" in flag["note"] for flag in analysis.flags)
    assert all(flag["action"] == "exclude" and flag["channel"] == "metadata" for flag in analysis.flags)


@pytest.mark.parametrize("time,date", [("250000", "100126"), ("120000", "310226"), ("12", "100126")])
def test_invalid_rmc_datetime_is_not_clock_evidence(tmp_path, nmea_sentence, time, date):
    path = tmp_path / "gps.nmea"
    path.write_text("2026-01-10 12:00:00," + nmea_sentence(rmc(time=time, date=date)) + "\n")
    analysis = analyze_gps("S99", path, 40, -105, CheckerConfig())
    assert analysis.clock_samples == []
    assert any(flag["issue"] == "GPS field invalid" for flag in analysis.flags)


def test_gga_has_no_invented_date(tmp_path, nmea_sentence):
    path = tmp_path / "gps.nmea"
    path.write_text("2026-01-10 12:00:00," + nmea_sentence("GPGGA,120000.00,4000.0000,N,10500.0000,W,1,10,1.0,1500.0,M,0,M,,") + "\n")
    assert next(parse_nmea_file(path)).gps_utc is None
    assert analyze_gps("S99", path, 40, -105, CheckerConfig()).clock_samples == []


@pytest.mark.parametrize("mode", ["N", "E", "M", "S"])
def test_simulated_estimated_or_invalid_rmc_mode_is_not_trusted(tmp_path, nmea_sentence, mode):
    path = tmp_path / "gps.nmea"
    path.write_text("2026-01-10 12:00:00," + nmea_sentence(rmc(mode=mode)) + "\n")
    assert analyze_gps("S99", path, 40, -105, CheckerConfig()).clock_samples == []


@pytest.mark.parametrize("raw,hemisphere", [("4060.00", "N"), ("9000.01", "N"),
                                          ("18000.01", "W"), ("4000.00", "X"), ("-1000.00", "N")])
def test_invalid_coordinate_fields(raw, hemisphere):
    assert _dddmm_to_decimal(raw, hemisphere) is None


def test_checksum_must_cover_the_complete_sentence(nmea_sentence):
    sentence = nmea_sentence(rmc())
    assert _nmea_checksum(sentence)[0]
    assert not _nmea_checksum(sentence + "junk")[0]
    assert not _nmea_checksum(sentence[1:])[0]


def test_position_uses_trusted_fixes_and_flags_large_installation_offset(tmp_path, nmea_sentence):
    path = tmp_path / "gps.nmea"
    path.write_text("2026-01-10 12:00:00," + nmea_sentence(rmc()) + "\n")
    flags = analyze_gps("S99", path, 39, -105, CheckerConfig()).flags
    flag = next(flag for flag in flags if flag["issue"] == "GPS and installation position disagree")
    assert "111.19 km" in flag["note"]
    assert flag["start_utc"] == datetime(2026, 1, 10, 12)


def test_separate_bad_packets_do_not_exclude_valid_gps_between_them(tmp_path, nmea_sentence):
    path = tmp_path / "gps.nmea"
    lines = [f"2026-01-10 {hour:02d}:00:00," + nmea_sentence(rmc(time=f"{hour:02d}0000.00"), valid_checksum=hour == 12)
             for hour in [11, 12, 13]]
    path.write_text("\n".join(lines) + "\n")
    flags = analyze_gps("S99", path, 40, -105, CheckerConfig()).flags
    assert len(flags) == 2
    assert all(flag["start_utc"] == flag["end_utc"] for flag in flags)
    assert not any(flag["start_utc"].hour == 12 for flag in flags)


def test_unreadable_gps_is_a_failure(tmp_path):
    with pytest.raises(OSError):
        analyze_gps("S99", tmp_path / "missing.nmea", None, None, CheckerConfig())


def test_truncated_supported_packet_is_not_silently_discarded(tmp_path, nmea_sentence):
    path = tmp_path / "gps.nmea"
    path.write_text("2026-01-10 12:00:00," + nmea_sentence("GPRMC,120000.00,A") + "\n")
    analysis = analyze_gps("S99", path, 40, -105, CheckerConfig())
    assert analysis.clock_samples == []
    assert analysis.flags[0]["issue"] == "GPS fix not valid"
    assert "1 of 1 parsed sentences" in analysis.flags[0]["note"]
