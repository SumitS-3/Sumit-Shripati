"""
GPS sentence parsing and integrity checks.

Supports $GPRMC and $GPGGA (NMEA 0183).
Safe against malformed, injected, or oversized input.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Iterator, List, Optional, Tuple

from .config import CheckerConfig

logger = logging.getLogger(__name__)

# Maximum line length we'll parse (guards against maliciously large fields)
_MAX_LINE_LEN = 200


@dataclass(frozen=True)
class GpsFix:
    logger_ts_str: str          # raw logger timestamp string (from file)
    sentence_type: str          # "GPRMC" or "GPGGA"
    utc_time: str               # HHMMSS.ss from sentence
    valid: bool                 # A = valid (GPRMC), fix_quality >= 1 (GPGGA)
    lat: Optional[float]        # decimal degrees, positive = N
    lon: Optional[float]        # decimal degrees, negative = W
    alt_m: Optional[float]      # altitude in metres (GPGGA only)
    checksum_ok: bool
    raw_line: str               # preserved for debugging
    gps_utc: Optional[datetime] = None  # full RMC date/time; GGA has no date


@dataclass(frozen=True)
class ClockSample:
    logger_utc: datetime
    gps_utc: datetime
    correction_seconds: Optional[int]  # None means not close to a constant whole-minute offset


@dataclass
class GpsAnalysis:
    flags: List[dict]
    clock_samples: List[ClockSample]


def _nmea_checksum(sentence: str) -> Tuple[bool, Optional[str]]:
    """
    Verify NMEA XOR checksum.
    Returns (is_valid, computed_hex).
    Sentence includes the leading '$' and exactly two trailing checksum digits.
    """
    star = sentence.rfind("*")
    if not sentence.startswith("$") or star < 0 or star + 3 != len(sentence):
        return False, None
    body = sentence[1:star]           # between $ and *
    declared = sentence[star + 1: star + 3].upper()
    computed = 0
    for ch in body:
        computed ^= ord(ch)
    computed_hex = f"{computed:02X}"
    return computed_hex == declared, computed_hex


def _dddmm_to_decimal(raw: str, hemi: str) -> Optional[float]:
    """
    Convert NMEA DDDMM.MMMM + hemisphere to decimal degrees.
    Returns None on parse failure.
    """
    if not raw or hemi not in ("N", "S", "E", "W"):
        return None
    try:
        raw = raw.strip()
        dot = raw.find(".")
        if dot < 2:
            return None
        # Degrees are everything before the last 2 integer digits before the dot
        deg_digits = dot - 2
        if deg_digits < 1:
            return None
        degrees = float(raw[:deg_digits])
        minutes = float(raw[deg_digits:])
        limit = 90 if hemi in ("N", "S") else 180
        if not (0 <= degrees <= limit and 0 <= minutes < 60):
            return None
        decimal = degrees + minutes / 60.0
        if decimal > limit:
            return None
        if hemi in ("S", "W"):
            decimal = -decimal
        return decimal
    except (ValueError, IndexError):
        return None


def _rmc_datetime(time_field: str, date_field: str) -> Optional[datetime]:
    """Use both RMC fields; never invent a date for a time-only GGA sentence."""
    try:
        if not re.fullmatch(r"\d{6}(?:\.\d+)?", time_field) or not re.fullmatch(r"\d{6}", date_field):
            return None
        fmt = "%d%m%y%H%M%S.%f" if "." in time_field else "%d%m%y%H%M%S"
        return datetime.strptime(date_field + time_field, fmt)
    except ValueError:
        return None


def _parse_gprmc(parts: List[str], raw: str, logger_ts: str, cs_ok: bool) -> Optional[GpsFix]:
    """Parse $GPRMC fields. Requires at least 10 fields."""
    # $GPRMC,HHMMSS,A/V,lat,N/S,lon,E/W,speed,course,DDMMYY,...
    if len(parts) < 10:
        return None
    mode = parts[12].strip().upper() if len(parts) > 12 else ""
    valid = parts[2].strip().upper() == "A" and mode in ("", "A", "D")
    lat = _dddmm_to_decimal(parts[3], parts[4].strip().upper())
    lon = _dddmm_to_decimal(parts[5], parts[6].strip().upper())
    if lat is not None and (lat < -90 or lat > 90):
        lat = None
    if lon is not None and (lon < -180 or lon > 180):
        lon = None
    return GpsFix(
        logger_ts_str=logger_ts,
        sentence_type="GPRMC",
        utc_time=parts[1],
        valid=valid,
        lat=lat,
        lon=lon,
        alt_m=None,
        checksum_ok=cs_ok,
        raw_line=raw,
        gps_utc=_rmc_datetime(parts[1], parts[9]),
    )


def _parse_gpgga(parts: List[str], raw: str, logger_ts: str, cs_ok: bool) -> Optional[GpsFix]:
    """Parse $GPGGA fields. Requires at least 10 fields."""
    # $GPGGA,HHMMSS,lat,N,lon,W,fix_q,sats,hdop,alt,M,...
    if len(parts) < 10:
        return None
    try:
        fix_q = int(parts[6].strip()) if parts[6].strip() else 0
    except ValueError:
        fix_q = 0
    # Estimated, manual and simulated positions are not trusted GPS fixes.
    valid = fix_q in (1, 2, 3, 4, 5)
    lat = _dddmm_to_decimal(parts[2], parts[3].strip().upper())
    lon = _dddmm_to_decimal(parts[4], parts[5].strip().upper())
    if lat is not None and (lat < -90 or lat > 90):
        lat = None
    if lon is not None and (lon < -180 or lon > 180):
        lon = None
    try:
        alt_m = float(parts[9]) if parts[9].strip() else None
    except ValueError:
        alt_m = None
    return GpsFix(
        logger_ts_str=logger_ts,
        sentence_type="GPGGA",
        utc_time=parts[1],
        valid=valid,
        lat=lat,
        lon=lon,
        alt_m=alt_m,
        checksum_ok=cs_ok,
        raw_line=raw,
    )


def parse_nmea_file(path: Path) -> Iterator[GpsFix]:
    """
    Lazily yield GpsFix objects from an NMEA file.

    File format: `logger_timestamp,NMEA_sentence` per line.
    Lines exceeding _MAX_LINE_LEN are silently skipped (security guard).
    Malformed sentences produce a warning and are skipped.
    """
    try:
        file_size = path.stat().st_size
    except OSError:
        logger.warning("Cannot stat GPS file %s", path)
        raise

    if file_size > 20 * 1024 * 1024:  # 20 MB guard
        logger.warning("GPS file %s is unusually large (%d bytes); parsing anyway.", path, file_size)

    try:
        with open(path, encoding="ascii", errors="replace") as f:
            for lineno, raw_line in enumerate(f, 1):
                raw_line = raw_line.rstrip("\r\n")
                if len(raw_line) > _MAX_LINE_LEN:
                    logger.debug("GPS line %d too long (%d chars), skipping.", lineno, len(raw_line))
                    continue
                if not raw_line or raw_line.startswith("#"):
                    continue

                # Split into logger_ts and NMEA sentence
                comma_pos = raw_line.find(",")
                if comma_pos < 0:
                    continue
                logger_ts = raw_line[:comma_pos].strip()
                sentence = raw_line[comma_pos + 1:].strip()

                if not sentence.startswith("$"):
                    continue

                cs_ok, _ = _nmea_checksum(sentence)
                # Strip '$', split on ',' (ignore trailing * checksum field)
                inner = sentence[1:].split("*", 1)[0]
                parts = inner.split(",")
                if not parts:
                    continue

                stype = parts[0].strip().upper()
                fix: Optional[GpsFix] = None
                try:
                    if stype == "GPRMC":
                        fix = _parse_gprmc(parts, sentence, logger_ts, cs_ok)
                    elif stype == "GPGGA":
                        fix = _parse_gpgga(parts, sentence, logger_ts, cs_ok)
                except Exception as exc:
                    logger.debug("GPS parse error line %d: %s", lineno, exc)
                    continue

                if fix is None and stype in ("GPRMC", "GPGGA"):
                    # Keep truncated supported packets visible in integrity counts.
                    fix = GpsFix(logger_ts, stype, "", False, None, None, None, cs_ok, sentence)
                if fix is not None:
                    yield fix
    except OSError as exc:
        logger.error("Cannot read GPS file %s: %s", path, exc)
        raise


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def analyze_gps(
    station_id: str,
    gps_path: Path,
    meta_lat: Optional[float],
    meta_lon: Optional[float],
    cfg: CheckerConfig,
) -> GpsAnalysis:
    """Parse once per station; share trusted evidence with the temporal pipeline.

    Only checksum-passing valid fixes contribute to position or clock evidence.
    Reference for RMC date/time and validity: https://gpsd.io/NMEA.html
    """
    flags: List[dict] = []
    fixes = list(parse_nmea_file(gps_path))
    if not fixes:
        raise ValueError(f"{station_id}: no supported GPS sentences could be parsed")

    def metadata_flag(subset: List[GpsFix], issue: str, note: str, action: str = "exclude") -> None:
        times = []
        for fix in subset:
            try:
                value = datetime.fromisoformat(fix.logger_ts_str)
                if value.tzinfo is None:
                    times.append(value)
            except ValueError:
                pass
        flags.append({
            "channel": "metadata", "start_utc": min(times) if times else "",
            "end_utc": max(times) if times else "", "issue": issue,
            "action": action, "note": note,
        })

    bad_checksums = [fix for fix in fixes if not fix.checksum_ok]
    invalid_status = [fix for fix in fixes if fix.checksum_ok and not fix.valid]
    trusted = [fix for fix in fixes if fix.checksum_ok and fix.valid]

    def packet_flags(subset: List[GpsFix], issue: str, reason: str) -> None:
        by_time: dict[str, List[GpsFix]] = {}
        for fix in subset:
            by_time.setdefault(fix.logger_ts_str[:16], []).append(fix)
        for packets in by_time.values():
            metadata_flag(packets, issue,
                          f"Ignore {len(packets)} GPS sentence(s) at this logger time for {reason} ({len(subset)} of {len(fixes)} parsed sentences in this pack); this alone does not invalidate weather observations.")

    for subset, issue in [(bad_checksums, "GPS checksum failure"),
                          (invalid_status, "GPS fix not valid")]:
        if subset:
            packet_flags(subset, issue, "clock and position evidence")

    bad_fields = [fix for fix in trusted if fix.lat is None or fix.lon is None or
                  (fix.sentence_type == "GPRMC" and fix.gps_utc is None)]
    positions = [fix for fix in trusted if fix.lat is not None and fix.lon is not None and
                 (fix.sentence_type != "GPRMC" or fix.gps_utc is not None)]
    if bad_fields:
        packet_flags(bad_fields, "GPS field invalid", "invalid coordinates or full RMC date/time")

    if positions and meta_lat is not None and meta_lon is not None and math.isfinite(meta_lat) and math.isfinite(meta_lon):
        center_lat = median(fix.lat for fix in positions)
        center_lon = median(fix.lon for fix in positions)
        distance = haversine_km(meta_lat, meta_lon, center_lat, center_lon)
        if distance > cfg.gps_position_tolerance_km:
            metadata_flag(positions, "GPS and installation position disagree",
                          f"Median of {len(positions)} valid fixes is ({center_lat:.5f}, {center_lon:.5f}), {distance:.2f} km from the install sheet; retain weather values but verify the station location before spatial comparisons.",
                          action="keep")
    for previous, current in zip(positions, positions[1:]):
        distance = haversine_km(previous.lat, previous.lon, current.lat, current.lon)
        if distance > cfg.gps_jump_tolerance_km:
            metadata_flag([previous, current], "GPS position discontinuity",
                          f"Two checksum-passing valid positions differ by {distance:.2f} km; withhold these position fixes until checked.")

    samples = []
    for fix in trusted:
        if fix.gps_utc is None or fix.lat is None or fix.lon is None:
            continue
        try:
            logger_time = datetime.fromisoformat(fix.logger_ts_str)
            if logger_time.tzinfo is not None:
                raise ValueError("logger timestamp must be UTC without a timezone suffix")
        except ValueError:
            metadata_flag([fix], "GPS logger timestamp invalid", "Cannot compare this GPS sentence with a valid UTC logger timestamp.")
            continue
        seconds = (fix.gps_utc - logger_time).total_seconds()
        rounded = round(seconds / 60) * 60
        offset = rounded if abs(seconds - rounded) <= cfg.gps_clock_tolerance_seconds else None
        samples.append(ClockSample(logger_time, fix.gps_utc, offset))
    logger.info("%s GPS: %d parsed sentences, %d trusted positions, %d full-date clock samples.",
                station_id, len(fixes), len(positions), len(samples))
    return GpsAnalysis(flags, samples)

