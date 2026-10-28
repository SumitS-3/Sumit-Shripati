"""Coordinator report: concise treatments and an evidence-specific next visit.

The static A4 template is separate from assessment and triage. Dynamic pack text is
escaped; no external fonts, scripts or assets are needed to read or print it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html
from pathlib import Path
from string import Template

import pandas as pd

from .findings import finding_kind
from .network_health import StationSummary, visit_candidate
from .output import atomic_text
from .schema import NUMERIC_COLUMNS

CHANNEL_LABELS = {
    "temp_c": "temperature", "rh_pct": "humidity", "pressure_hpa": "pressure",
    "wind_kmh": "wind speed", "wind_dir_deg": "wind direction", "rain_mm": "rain",
    "ghi_wm2": "sunlight",
}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _date(value: pd.Timestamp, time: bool = False) -> str:
    return value.strftime("%d %b %H:%M" if time else "%d %b").lstrip("0")


def _period(start: pd.Timestamp, end: pd.Timestamp, time: bool = False) -> str:
    return _date(start, time) if start == end else f"{_date(start, time)}–{_date(end, time)}"


def _treatment(summary: StationSummary) -> str:
    if summary.analysis_failed:
        return "Hold all channels. Analysis incomplete; rerun before use."
    if not summary.expected_records:
        return "No assessed observation window. Verify the input period."
    parts = []
    for adjustment in summary.clock_adjustments:
        amount = f"{adjustment.minutes / 60:+g} h" if adjustment.minutes % 60 == 0 else f"{adjustment.minutes:+g} min"
        parts.append(f"Use after {amount} clock repair ({adjustment.records:,} records).")
    if summary.replayed_records:
        parts.append(f"Remove {summary.replayed_records:,} exact replay rows before use.")
    for finding in summary.findings:
        label = CHANNEL_LABELS.get(finding.channel, "observations")
        if finding.kind == "thermal":
            parts.append(f"Withhold daytime temperature from {_date(finding.start)}; onset uncertain.")
        elif finding.kind == "rain":
            parts.append(f"Withhold rain {_period(finding.start, finding.end)}; drainage is undated.")
        elif finding.kind in ("pressure", "wind"):
            parts.append(f"Withhold {label} from {_date(finding.start, time=True)}.")
        elif finding.kind == "humidity":
            parts.append(f"Withhold humidity {_period(finding.start, finding.end)}.")
        elif finding.kind == "flatline":
            parts.append(f"Drop frozen {label} {_period(finding.start, finding.end)}.")
        elif finding.kind == "bounds":
            parts.append(f"Drop {finding.affected_records:,} invalid {label} reading(s).")
        elif finding.kind == "gap":
            continue  # Missing slots are counted once below, including edge gaps.
        else:
            parts.append(f"Withhold flagged {label} intervals.")
    missing = max((channel.missing_records for channel in summary.channels.values()), default=0)
    if missing:
        parts.append(f"Leave {missing:,} missing intervals blank.")
    if any(channel.pending_records for channel in summary.channels.values()):
        parts.append("Pending corrections must be reviewed before those intervals are used.")
    if not parts:
        parts.append("Use all channels after the stated checks.")
    if any(finding_kind(flag.get("issue", "")) == "location" for flag in summary.metadata_flags):
        parts.append("Confirm location before mapping.")
    treatment = " ".join(parts)
    if len(treatment) > 270:
        treatment = treatment[:230].rsplit(". ", 1)[0].rstrip(".") + ". Additional treatments: flags.csv."
    return treatment


def _station_row(summary: StationSummary) -> str:
    cells = []
    for channel in NUMERIC_COLUMNS:
        coverage = summary.channels[channel]
        if summary.analysis_failed or not coverage.total_records:
            number, style = "—", "unassessed"
        else:
            pct = coverage.usable_pct
            number = "100" if coverage.usable_records == coverage.total_records else (
                ">99.9" if pct > 99.95 else f"{pct:.1f}")
            style = "complete" if coverage.usable_records == coverage.total_records else "restricted"
        title = (f"{coverage.usable_records:,} of {coverage.total_records:,} expected: "
                 f"{coverage.missing_records:,} missing, {coverage.excluded_records:,} excluded, "
                 f"{coverage.pending_records:,} awaiting treatment")
        cells.append(f'<td class="coverage {style}" data-channel="{channel}" title="{_esc(title)}">{_esc(number)}</td>')
    badge = "Hold" if summary.analysis_failed else ("Fix first" if summary.correct_flags else "")
    badge_html = f'<span class="station-badge">{badge}</span>' if badge else ""
    name = summary.name if len(summary.name) <= 58 else summary.name[:55] + "…"
    return (f'<tr><th scope="row"><b>{_esc(summary.station_id)}</b>{badge_html}<span class="site-name">{_esc(name)}</span></th>'
            + "".join(cells) + f'<td class="treatment">{_esc(_treatment(summary))}</td></tr>')


def _visit(summaries: dict[str, StationSummary], tech: str | None, runner: str | None) -> tuple[str, str]:
    candidate = visit_candidate(summaries[tech]) if tech in summaries else None
    if candidate is None:
        return ('<h3>No station trip justified yet.</h3><p>No unresolved on-site problem is established at the pack end. '
                'Confirm current status with volunteers before allocating the hour.</p>',
                '<p>Historical gaps, recovered failures and discarded GPS packets do not by themselves justify a visit.</p>')
    summary, profile = summaries[tech], candidate.profile
    steps = "".join(f"<li>{_esc(step)}</li>" for step in profile.steps)
    content = (f'<h3>{_esc(summary.station_id)} <span>{_esc(summary.name)}</span></h3>'
               f'<p class="visit-reason">{_esc(profile.visit_reason)}</p>'
               f'<p class="first-check"><b>Check first</b> {_esc(profile.first_check)}</p>'
               f'<ol class="visit-steps">{steps}</ol><p class="bring"><b>Bring</b> {_esc(profile.bring)}</p>')
    runner_candidate = visit_candidate(summaries[runner]) if runner in summaries else None
    if runner_candidate:
        other = runner_candidate.profile
        comparison = (f'<h3>Why not {_esc(runner)} first?</h3><p>{_esc(summaries[runner].name)} has an unresolved '
                      f'{_esc(other.title)} problem.</p><p>{_esc(profile.priority_reason or profile.visit_reason)}</p>')
        if other.remote_first:
            comparison += f'<p><b>Before another trip</b> {_esc(other.remote_first)}</p>'
    else:
        comparison = '<h3>No second trip selected.</h3><p>No other unresolved on-site fault has enough evidence to rank.</p>'
    if profile.remote_first:
        comparison += f'<p class="confirm"><b>Confirm before travelling</b> {_esc(profile.remote_first)}</p>'
    return content, comparison


def _cautions(summaries: dict[str, StationSummary]) -> str:
    def sites(station_ids: list[str]) -> str:
        shown = ", ".join(station_ids[:3])
        return shown + (f" and {len(station_ids) - 3} other stations" if len(station_ids) > 3 else "")

    items = []
    if any(summary.analysis_failed for summary in summaries.values()):
        items.append("Stations marked Hold were not fully assessed; their observations must not enter the summary yet.")
    uncertain = [sid for sid, summary in summaries.items() if any(finding.kind in ("thermal", "rain") for finding in summary.findings)]
    if uncertain:
        items.append(f"{sites(uncertain)}: the flagged site-work or blocked-gauge boundaries are cautious exclusions, not known failure times; do not invent corrections or redistribute drainage.")
    locations = [sid for sid, summary in summaries.items() if any(finding_kind(flag.get("issue", "")) == "location" for flag in summary.metadata_flags)]
    if locations:
        items.append(f"{sites(locations)}: receiver and install-sheet positions disagree; verify the site before spatial use.")
    if any(summary.keep_flags for summary in summaries.values()):
        items.append("Peer-supported humid weather and coherent weather changes are retained, even when they look unusual.")
    items.append("Coverage is eligibility after treatment, not proof of calibration; unflagged records pass the applicable checks only.")
    return "".join(f"<li>{_esc(item)}</li>" for item in items)


def generate_html_report(summaries: dict[str, StationSummary], all_flags: list[dict],
                         tech_station: str | None, runner_up: str | None,
                         output_path: str, title: str = "Ridgeline Schools Weather Network") -> None:
    starts = [summary.period_start for summary in summaries.values() if summary.period_start is not None]
    ends = [summary.period_end for summary in summaries.values() if summary.period_end is not None]
    if starts and ends:
        first, last = min(starts), max(ends)
        period = f"{first.strftime('%d %b %Y')}–{last.strftime('%d %b %Y')} · UTC"
        days = (last.normalize() - first.normalize()).days + 1
        window = f"{days} days / {len(summaries)} stations"
    else:
        period, window = "Observation window unavailable", f"{len(summaries)} stations / assessment incomplete"
    ordered = sorted(summaries.values(), key=lambda summary: (
        not summary.analysis_failed, not bool(summary.exclude_flags or summary.correct_flags), summary.station_id))
    shown = sorted(ordered[:12], key=lambda summary: summary.station_id)
    overview_note = (f"Showing {len(shown)} of {len(summaries)} stations, prioritising holds and restrictions. "
                     "All flagged intervals are in flags.csv." if len(shown) < len(summaries) else
                     "Use unaffected channels. Set aside flagged intervals; apply clock and replay treatments first.")
    visit, comparison = _visit(summaries, tech_station, runner_up)
    decision = (f"Visit {tech_station}: {summaries[tech_station].name}." if tech_station in summaries else
                "Confirm present conditions before choosing a trip.")
    template = Template((Path(__file__).parent / "templates" / "report.html").read_text(encoding="utf-8"))
    content = template.substitute(
        title=_esc(title), period=_esc(period), window=_esc(window), decision=_esc(decision),
        overview_note=_esc(overview_note), station_rows="".join(_station_row(summary) for summary in shown),
        visit=visit, comparison=comparison, cautions=_cautions(summaries),
        generated=_esc(datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")),
        flag_count=len(all_flags),
    )
    with atomic_text(output_path) as stream:
        stream.write(content)
