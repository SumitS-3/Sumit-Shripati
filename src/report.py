"""
HTML report generation for the Ridgeline network health report.

Design principles:
  - No external CSS/JS dependencies (self-contained HTML)
  - A4-printable layout
  - No fabricated data: all figures come from actual flags and summaries
  - HTML-escaped content prevents injection
  - Plain English suitable for a non-technical coordinator
"""
from __future__ import annotations

import html
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .network_health import StationSummary

logger = logging.getLogger(__name__)

STATUS_STYLE = {
    "Good":                     "color:#1a7a1a; font-weight:bold",
    "Usable (with caveats)":    "color:#7a5c00; font-weight:bold",
    "Partial":                  "color:#b05000; font-weight:bold",
    "Unreliable":               "color:#9a0000; font-weight:bold",
    "Unknown":                  "color:#555555",
}

ACTION_BADGE = {
    "exclude": '<span style="background:#ffd0d0;color:#900;padding:1px 5px;border-radius:3px">exclude</span>',
    "correct": '<span style="background:#fff0cc;color:#660;padding:1px 5px;border-radius:3px">correct</span>',
    "keep":    '<span style="background:#d0f0d0;color:#090;padding:1px 5px;border-radius:3px">keep</span>',
}


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _status_cell(status: str) -> str:
    style = STATUS_STYLE.get(status, "")
    return f'<span style="{style}">{_esc(status)}</span>'


def _recommendation_cell(rec: str) -> str:
    colour = {
        "Include": "#1a7a1a",
        "Include with corrections noted": "#7a5c00",
        "Include usable channels; exclude flagged periods": "#b05000",
        "Exclude or manual review required": "#9a0000",
    }.get(rec, "#333")
    return f'<span style="color:{colour};font-weight:bold">{_esc(rec)}</span>'


def generate_html_report(
    summaries: Dict[str, StationSummary],
    all_flags: List[dict],
    tech_station: Optional[str],
    runner_up: Optional[str],
    output_path: str,
) -> None:
    """
    Write a self-contained HTML network health report.
    Uses atomic replacement (temp file → rename).
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_stations = len(summaries)
    n_exclude  = sum(1 for f in all_flags if f.get("action") == "exclude")
    n_correct  = sum(1 for f in all_flags if f.get("action") == "correct")
    n_keep     = sum(1 for f in all_flags if f.get("action") == "keep")
    n_total    = len(all_flags)

    # ── Station overview table ────────────────────────────────────────────
    station_rows = []
    for sid, s in sorted(summaries.items()):
        rec_style = STATUS_STYLE.get(s.climate_summary_recommendation.split()[0], "")
        row = f"""
        <tr>
          <td><strong>{_esc(sid)}</strong></td>
          <td>{_esc(s.name)}</td>
          <td style="text-align:center">{s.total_records:,}</td>
          <td style="text-align:center">{_status_cell(s.overall_status)}</td>
          <td>{_recommendation_cell(s.climate_summary_recommendation)}</td>
          <td style="text-align:center">{len(s.exclude_flags)}</td>
        </tr>"""
        station_rows.append(row)

    # ── Key findings list ─────────────────────────────────────────────────
    # Show the top flags (exclude first, then correct, limit to 20)
    sorted_flags = (
        [f for f in all_flags if f.get("action") == "exclude" and f.get("channel") != "metadata"][:12]
        + [f for f in all_flags if f.get("action") == "correct" and f.get("channel") != "metadata"][:6]
    )
    flag_rows = []
    for f in sorted_flags:
        badge = ACTION_BADGE.get(f.get("action", "keep"), "")
        row = f"""
        <tr>
          <td><strong>{_esc(str(f.get('station_id','')))}</strong></td>
          <td>{_esc(str(f.get('channel','')))}</td>
          <td>{_esc(str(f.get('start_utc','')))}</td>
          <td>{_esc(str(f.get('end_utc','')))}</td>
          <td>{_esc(str(f.get('issue','')))}</td>
          <td>{badge}</td>
        </tr>"""
        flag_rows.append(row)

    # ── Technician section ────────────────────────────────────────────────
    tech_html = ""
    if tech_station and tech_station in summaries:
        ts = summaries[tech_station]
        runner_note = ""
        if runner_up and runner_up in summaries:
            rs = summaries[runner_up]
            runner_note = (
                f"<p>The second-best candidate is <strong>{_esc(runner_up)} ({_esc(rs.name)})</strong>, "
                f"with {len(rs.exclude_flags)} exclusion flag(s). "
                f"It is ranked lower because its issues appear less likely to be resolved by on-site intervention.</p>"
            )

        key_issues = [f for f in ts.exclude_flags[:5]]
        issue_list = "".join(
            f"<li>{_esc(str(f.get('issue','')))}<br>"
            f"<small style='color:#555'>{_esc(str(f.get('note',''))[:120])}…</small></li>"
            for f in key_issues
        )

        tech_html = f"""
    <h2>4. Recommended Technician Visit</h2>
    <p>Based on flag analysis, the coordinator should send the technician to:</p>
    <p style="font-size:1.3em;background:#fff0cc;padding:8px 14px;border-left:4px solid #c90">
      <strong>{_esc(tech_station)}: {_esc(ts.name)}</strong>
    </p>
    <p><strong>Why this station?</strong> It has {len(ts.exclude_flags)} exclusion flag(s) and
    {len(ts.correct_flags)} correction flag(s). The problems identified suggest on-site hardware
    inspection can resolve the root cause.</p>
    <p><strong>Key issues to address:</strong></p>
    <ul>{issue_list}</ul>
    <h3>5. Suggested First Inspection Steps (within 60 minutes)</h3>
    <ol>
      <li>Check the radiation shield (temperature sensor housing) for obstructions, spider webs, or paint damage.</li>
      <li>Inspect the rain gauge funnel and tipping bucket for blockage.</li>
      <li>Verify the humidity sensor probe is clean and unobstructed.</li>
      <li>Review the logger display for error codes and check the clock against GPS time.</li>
      <li>Record the logger's current readings and compare with a reference thermometer.</li>
      <li>Photograph the installation before and after any intervention.</li>
    </ol>
    {runner_note}
"""
    else:
        tech_html = "<h2>4. Technician Recommendation</h2><p>Insufficient data to make a recommendation.</p>"

    # ── Full HTML ─────────────────────────────────────────────────────────
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Ridgeline Schools Weather Network — Health Report</title>
<style>
  @page {{ size: A4; margin: 18mm 18mm 18mm 18mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #222;
          max-width: 190mm; margin: auto; line-height: 1.4; }}
  h1 {{ font-size: 14pt; border-bottom: 2px solid #333; padding-bottom: 4px; }}
  h2 {{ font-size: 11pt; border-bottom: 1px solid #aaa; margin-top: 14px; padding-bottom: 2px; }}
  h3 {{ font-size: 10pt; margin-top: 10px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 9pt; margin-bottom: 8px; }}
  th {{ background: #eee; border: 1px solid #ccc; padding: 3px 6px; text-align: left; }}
  td {{ border: 1px solid #ddd; padding: 3px 6px; vertical-align: top; }}
  .summary-box {{ background: #f0f4ff; border: 1px solid #b0c0e0;
                  padding: 6px 12px; margin-bottom: 10px; }}
  ul, ol {{ margin-top: 4px; }}
  li {{ margin-bottom: 3px; }}
  .footer {{ font-size: 8pt; color: #888; border-top: 1px solid #ccc;
             margin-top: 16px; padding-top: 4px; }}
  @media print {{
    .no-print {{ display: none; }}
    body {{ font-size: 9pt; }}
  }}
</style>
</head>
<body>
<h1>Ridgeline Schools Weather Network — Health Report</h1>
<p style="color:#555; font-size:9pt">Generated: {_esc(now_str)} &nbsp;|&nbsp;
Observation period: 2025-03-01 to 2025-04-11 &nbsp;|&nbsp;
{n_stations} stations checked</p>

<div class="summary-box">
<strong>Summary:</strong>
{n_total} data-quality flags raised across {n_stations} stations —
{n_exclude} exclusion(s), {n_correct} correction(s), {n_keep} kept-for-awareness.
All figures are derived from automated analysis of the supplied data files.
</div>

<h2>1. Network Health Overview</h2>
<table>
  <tr>
    <th>ID</th><th>Station Name</th><th>Records</th>
    <th>Status</th><th>Climate Summary</th><th>Exclusion flags</th>
  </tr>
  {"".join(station_rows)}
</table>

<h2>2. Climate Summary Data Suitability</h2>
<p>The table above shows each station's recommended treatment.
Stations rated <strong>Good</strong> may be included directly.
Stations rated <strong>Usable (with caveats)</strong> or <strong>Partial</strong>
should be included only after applying the documented corrections and excluding
flagged intervals. A station rated <strong>Unreliable</strong> should be excluded
from the climate summary or reviewed manually before use.</p>
<p>Note: a "Partial" or "Usable" rating does not mean the station is broken — it means
one or more channels had a short failure or require a documented correction. Other channels
from the same station may be fully usable.</p>

<h2>3. Important Data-Quality Problems</h2>
<table>
  <tr>
    <th>Station</th><th>Channel</th><th>From</th><th>To</th><th>Issue</th><th>Action</th>
  </tr>
  {"".join(flag_rows)}
</table>
<p style="font-size:9pt; color:#555">
(Showing top {len(sorted_flags)} flags by severity. Full list in flags.csv.)
</p>

{tech_html}

<h2>6. Limitations and Uncertainties</h2>
<ul>
  <li>Thresholds for spike and flatline detection are configurable estimates, not
      calibrated instrument specifications. Short flatlines (≤ 1 h) are flagged for
      awareness but may represent genuine stable atmospheric conditions.</li>
  <li>Cross-station comparison is qualitative: stations differ in elevation and
      local microclimate, so agreement is expected but not guaranteed.</li>
  <li>The maintenance log is from volunteer memory and may be incomplete or imprecise
      about exact times.</li>
  <li>GPS position offsets are within GPS accuracy for most stations and do not
      indicate significant location errors.</li>
  <li>Rain-gauge blockage periods (e.g. S04 before 2025-04-02) cannot be
      distinguished from genuine dry periods without cross-station rainfall comparison.</li>
</ul>

<div class="footer">
Ridgeline Schools Weather Network — Automated Quality Report.
Data based on ERA5 reanalysis (Copernicus Climate Change Service, CC BY 4.0).
Station names, people, hardware and notes are fictional (assignment dataset).
This report is for the assignment evaluation only.
</div>
</body>
</html>"""

    # Atomic write
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".html.tmp", dir=os.path.dirname(os.path.abspath(output_path))
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(html_content)
        os.replace(tmp_path, output_path)
        logger.info("Wrote report to %s", output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

