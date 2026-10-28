"""One catalogue for coordinator wording and visit priorities, independent of site IDs.

Priorities express an explicit triage policy, not a statistical confidence score:
ongoing loss of the logger, temperature exposure, wind scaling, then calibration.
Historical incidents and corrections already resolved in software do not earn visits.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FindingProfile:
    key: str
    title: str
    priority: int
    visit_reason: str
    first_check: str
    steps: tuple[str, str, str]
    bring: str
    remote_first: str = ""
    priority_reason: str = ""


PROFILES = {
    "thermal": FindingProfile("thermal", "daytime temperature exposure", 4,
        "Temperature is still suspect in daylight; a physical exposure check can prevent further biased readings.",
        "Check airflow and the radiation shield before changing the logger settings.",
        ("0–10 min: photograph the shield, vents and painted support; check for blocked airflow or contact with the post.",
         "10–40 min: compare with a shaded, ventilated reference thermometer; clean or restore the shield as needed.",
         "40–60 min: record before/after readings and UTC time; leave the suspect period withheld until a later daylight comparison passes."),
        "Reference thermometer, cleaning brush and camera.",
        "Confirm that the recorded site work has not already been reversed.",
        "Air-temperature exposure is central to the climate summary and needs a physical shield check; that makes it the first use of the on-site hour."),
    "wind": FindingProfile("wind", "wind sensor scaling", 3,
        "The replacement still reads on a different relative scale; verify the sensor and logger pairing.",
        "Read the replacement sensor model and its pulse specification before editing any multiplier.",
        ("0–10 min: photograph the sensor label and record the logger input configuration.",
         "10–40 min: compare the manufacturer's pulse specification with the logger setting; correct a documented mismatch only.",
         "40–60 min: verify rotation and logged response, then record UTC time and settings; keep earlier uncertain wind withheld."),
        "Sensor documentation, logger access and camera.",
        "Request a photo of the sensor label and logger settings first; a documented mismatch may be resolved remotely."),
    "pressure": FindingProfile("pressure", "pressure calibration", 2,
        "The pressure regime remains uncalibrated; a reference comparison is needed before releasing it.",
        "Compare station pressure with a calibrated reference at the same height; do not compare with sea-level pressure.",
        ("0–10 min: record sensor identification, height and logger settings.",
         "10–40 min: compare stable readings with a calibrated reference at the same height.",
         "40–60 min: document any supported adjustment and UTC time; do not back-correct earlier data from a single comparison."),
        "Calibrated reference barometer and logger access.",
        "Arrange the calibrated reference before travelling."),
    "humidity": FindingProfile("humidity", "humidity sensor condition", 3,
        "The saturation contradiction remains unresolved and needs a probe check.",
        "Inspect the probe and filter for wetting or blockage before any calibration adjustment.",
        ("0–10 min: photograph the probe and filter; check for moisture or obstruction.",
         "10–40 min: clean or dry according to sensor instructions and compare with a reference hygrometer.",
         "40–60 min: record readings and UTC time; continue withholding data until the comparison is credible."),
        "Reference hygrometer, approved cleaning materials and camera."),
    "flatline": FindingProfile("flatline", "stuck sensor or logger", 3,
        "A stuck channel persists at the end of the pack; check the signal path on site.",
        "Compare the physical sensor and logger display before disconnecting or rebooting anything.",
        ("0–10 min: photograph readings, error codes, wiring and power status.",
         "10–40 min: inspect the affected connection and compare the sensor with an independent reference.",
         "40–60 min: record any repair and UTC time; verify that fresh readings vary plausibly."),
        "Reference instrument, basic connection tools and camera."),
    "rain": FindingProfile("rain", "rain-gauge obstruction", 3,
        "The rain record remains affected; restore a clear, functioning gauge without treating released water as rainfall.",
        "Photograph the funnel and bucket before clearing any blockage.",
        ("0–10 min: inspect and photograph the funnel, bucket and mounting.",
         "10–40 min: clear the obstruction and check tipping with a measured test volume.",
         "40–60 min: document the UTC boundary and any test/drainage tips so they can be excluded."),
        "Brush, measured water and camera."),
    "gap": FindingProfile("gap", "ongoing data loss", 5,
        "Expected observations are still missing at the network end; restoring the logger can recover every channel.",
        "Check whether data is stored locally before changing power or communication settings.",
        ("0–10 min: check the local display, storage, power and communication indicators.",
         "10–40 min: restore the evidenced power or link problem and preserve any buffered observations.",
         "40–60 min: verify fresh UTC-stamped records and note the repair boundary."),
        "Logger access and appropriate power/communication spares.",
        "Confirm that this is a live outage rather than a delayed upload before travelling."),
    "unknown": FindingProfile("unknown", "unresolved channel problem", 1,
        "A channel remains excluded, but its cause needs confirmation before a repair is promised.",
        "Review the flagged evidence and compare the affected channel with an independent reference.",
        ("0–10 min: record the affected channel, sensor identification and current readings.",
         "10–40 min: check the documented symptom against an independent reference.",
         "40–60 min: record findings and UTC time; release data only when the evidence supports it."),
        "An appropriate reference instrument and camera."),
}


def finding_kind(issue: str) -> str:
    """Serialized CSV issues are classified here once, rather than throughout the UI."""
    prefixes = {
        "Persistent sunlight-linked": "thermal", "Wind scale changed": "wind",
        "Pressure baseline shift": "pressure", "Pressure step": "pressure",
        "Humidity saturation contradicts": "humidity", "Flatline:": "flatline",
        "Blocked gauge": "rain", "Data gap:": "gap",
        "GPS-confirmed logger clock offset": "clock", "Exact observation replay": "replay",
        "Unresolved logger clock evidence": "unresolved", "Station analysis incomplete": "failed",
        "GPS and installation position disagree": "location", "Out-of-range": "bounds",
        "RH above": "bounds", "Conflicting duplicate timestamps": "conflicts",
    }
    return next((kind for prefix, kind in prefixes.items() if str(issue).startswith(prefix)), "unknown")
