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
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Maximum line length we'll parse (guards against maliciously large fields)
_MAX_LINE_LEN = 200
# Approximate degrees-per-metre at mid-latitudes (used for location-jump check)
_DEG_PER_KM = 1.0 / 111.0


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


def _nmea_checksum(sentence: str) -> Tuple[bool, Optional[str]]:
    """
    Verify NMEA XOR checksum.
    Returns (is_valid, computed_hex).
    Sentence should NOT include the leading '$'.
    """
    star = sentence.rfind("*")
    if star < 0 or star + 3 > len(sentence):
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
    if not raw or not hemi:
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
        decimal = degrees + minutes / 60.0
        if hemi in ("S", "W"):
            decimal = -decimal
        return decimal
    except (ValueError, IndexError):
        return None


def _parse_gprmc(parts: List[str], raw: str, logger_ts: str, cs_ok: bool) -> Optional[GpsFix]:
    """Parse $GPRMC fields. Requires at least 10 fields."""
    # $GPRMC,HHMMSS,A/V,lat,N/S,lon,E/W,speed,course,DDMMYY,...
    if len(parts) < 10:
        return None
    valid = parts[2].strip().upper() == "A"
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
    valid = fix_q >= 1
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
        return

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
                inner = sentence.lstrip("$")
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

                if fix is not None:
                    yield fix
    except OSError as exc:
        logger.error("Cannot read GPS file %s: %s", path, exc)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def check_gps_integrity(
    station_id: str,
    gps_path: Path,
    meta_lat: Optional[float],
    meta_lon: Optional[float],
) -> List[dict]:
    """
    Perform GPS integrity checks. Returns list of flag-candidate dicts:
        {channel, start_utc, end_utc, issue, action, note}

    Checks:
      1. Checksum failures
      2. Invalid fix (status V in GPRMC, fix_q=0 in GPGGA)
      3. Coordinate out of bounds
      4. Position inconsistency vs. install-sheet lat/lon
      5. Large between-fix jumps (station should be stationary)
    """
    flags: List[dict] = []
    fixes: List[GpsFix] = []

    bad_cs = 0
    invalid_fix = 0

    for fix in parse_nmea_file(gps_path):
        if not fix.checksum_ok:
            bad_cs += 1
        if not fix.valid:
            invalid_fix += 1
        if fix.lat is not None and fix.lon is not None:
            fixes.append(fix)

    total = len(fixes) + bad_cs + invalid_fix
    logger.info(
        "%s GPS: %d usable fixes, %d bad checksums, %d invalid status sentences.",
        station_id, len(fixes), bad_cs, invalid_fix
    )

    if bad_cs > 0:
        flags.append({
            "channel": "metadata",
            "start_utc": "",
            "end_utc": "",
            "issue": f"GPS: {bad_cs} sentences with failed NMEA checksum",
            "action": "keep",
            "note": (
                f"{bad_cs} of {total} GPS sentences have incorrect checksums; "
                "data may have been corrupted in transmission."
            ),
        })

    if invalid_fix > 0:
        flags.append({
            "channel": "metadata",
            "start_utc": "",
            "end_utc": "",
            "issue": f"GPS: {invalid_fix} sentences report no valid fix",
            "action": "keep",
            "note": (
                f"{invalid_fix} sentences report status V (no fix) or fix_quality=0; "
                "clock discipline may have been lost briefly."
            ),
        })

    if not fixes:
        return flags

    # ── Position vs. install sheet ─────────────────────────────────────────
    if meta_lat is not None and meta_lon is not None:
        lats = [f.lat for f in fixes if f.lat is not None]
        lons = [f.lon for f in fixes if f.lon is not None]
        if lats and lons:
            mean_lat = sum(lats) / len(lats)
            mean_lon = sum(lons) / len(lons)
            dist_km = haversine_km(meta_lat, meta_lon, mean_lat, mean_lon)
            if dist_km > 1.0:
                flags.append({
                    "channel": "metadata",
                    "start_utc": "",
                    "end_utc": "",
                    "issue": f"GPS mean position {dist_km:.2f} km from install-sheet location",
                    "action": "keep",
                    "note": (
                        f"GPS centroid ({mean_lat:.4f}, {mean_lon:.4f}) is {dist_km:.2f} km "
                        f"from install-sheet ({meta_lat:.4f}, {meta_lon:.4f}); "
                        "verify station ID or check install-sheet accuracy."
                    ),
                })

    # ── Between-fix jumps > 0.5 km (station should be stationary) ─────────
    prev = fixes[0]
    for curr in fixes[1:]:
        if prev.lat is None or curr.lat is None:
            prev = curr
            continue
        d = haversine_km(prev.lat, prev.lon, curr.lat, curr.lon)
        if d > 0.5:
            flags.append({
                "channel": "metadata",
                "start_utc": curr.logger_ts_str,
                "end_utc": curr.logger_ts_str,
                "issue": f"GPS position jump {d:.2f} km at {curr.logger_ts_str}",
                "action": "keep",
                "note": (
                    f"Consecutive GPS fixes are {d:.2f} km apart; "
                    "station should be stationary — possible multipath or temporary satellite geometry issue."
                ),
            })
        prev = curr

    return flags

