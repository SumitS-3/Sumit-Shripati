# Implementation checkpoints

Work stops after each phase so the changes can be reviewed and committed separately.
The original data pack stays local and must not be committed.

## Phase 1 - accurate flag intervals (complete)

- Exclude the entire `ridgeline_data_pack*/` folder from Git, including its README.
- Keep separate missing-value and duplicate incidents separate; count distinct
  duplicate timestamps rather than assuming there are always two rows per time.
- Report only missing slots in gap flags, leaving the two observed endpoints usable;
  detect a single missing sample with the default ten-minute cadence.
- Break anomaly runs at missing time or repeated timestamps. Flatline duration
  cannot span an outage or combine adjacent runs with different values.
- Merge only matching channel, issue and action, across at most one sample interval;
  preserve overlapping interval bounds and supporting notes.
- Add regression cases and a CLI test using an independently generated pack,
  a different station ID, and an output directory separate from the pack.

Validation: `python -m pytest -q` passes **57 tests**. The original baseline had
39 passing tests and one failing aggregation test. That test now uses the same
issue description for adjacent flags; separate tests require distinct descriptions
and actions to remain separate.

The supplied pack runs successfully across eight stations. Diagnostic outputs are
in ignored `scratch/phase1/`; the root `flags.csv` and `report.html` are still the
earlier outputs and are **not final deliverables**. More flags after interval
splitting do not mean that more observations are faulty.

Suggested commit: `fix: preserve precise problem intervals and ignore the data pack`

## Phase 2a - GPS, clock and replay integrity (complete)

Phase 2 is split into two reviewable commits: timing integrity first, then the
weather-specific decisions. Final flags and the coordinator report depend on both.

- Shared schemas and a vectorized interval helper replace repeated channel lists,
  timestamp formats and run detection. Pytest path configuration and fixture builders
  are centralized; production code has no station IDs or anomaly dates embedded in it.
- Ingestion preserves source sequence and CSV row numbers. Invalid timestamps fail
  visibly, and observation and metadata reads no longer silently stop at a row cap.
- GPS is parsed once per station. Clock evidence uses both RMC date and UTC time;
  bad checksums, invalid fix statuses, unsupported modes and invalid fields are not
  trusted. GGA time is never assigned an invented date. Position estimates use only
  trusted fixes and a median rather than an outlier-sensitive mean.
- A correction requires at least three independent stable RMC fixes, bounded GPS
  gaps, and a unique observation break matching each GPS offset transition. Ambiguous
  evidence withholds the station's timed observations instead of guessing. Default
  thresholds are configurable and validated.
- The working timeline is corrected before deduplication or weather checks. Exact
  replays retain the first identical record; conflicting duplicates are withheld.
  Correction notes identify source CSV rows and the signed timestamp adjustment.
- Individual invalid GPS packets receive bounded metadata flags, not one long
  exclusion spanning valid packets. GPS metadata exclusions do not count as weather
  loss or dominate visit priority; fuller report/priority changes remain in Phase 3.
- Missing station files, malformed station data, failed sensor/GPS checks and report
  failures produce nonzero exit codes; station failures also appear in `flags.csv`.
  Each station's metadata cadence is passed through the shared checking pipeline.

Validation: `python -m pytest -q` passes **114 tests**, including full CLI fixtures
using different dates/station IDs, forward/backward clock changes, midnight,
multiple transitions, insufficient evidence, replay, conflicts, failure paths,
source protection and an input with more than 100,000 rows.

On the supplied pack, the shared rules identify S06 source rows 3411-6049 for a
**+360 minute** timestamp correction, supported by 435 valid RMC fixes. All 6,048
observations are retained; the earlier spurious pressure and sunlight flags disappear.
For S08, 144 identical replay rows are removed; the 214 genuinely missing ten-minute
slots remain flagged. All 19 source files were verified unchanged by SHA-256 hashes.

Synthetic timing-stage measurement on this machine (6,048 observations and 1,008
GPS samples per station, processed sequentially):

| Stations | Elapsed | Peak incremental traced memory |
| --- | --- | --- |
| 8 | 0.07 s | 1.65 MiB |
| 100 | 0.81 s | 1.82 MiB |
| 500 | 3.69 s | 1.84 MiB |

These figures cover only temporal normalization, exclude allocating the shared test
input, and are not full-checker RAM or runtime claims. The CLI still retains station
frames for the existing report; reducing that retention belongs with Phase 3.

Diagnostic outputs are under ignored `scratch/phase2a/`. The root `flags.csv` and
`report.html` remain the previous, preliminary deliverables until the remaining
weather decisions are validated.

Suggested commit: `feat: validate GPS clocks and resolve observation replays`

## Phase 2b - weather evidence and defensible treatments (complete)

- Night/day screening now uses UTC, station coordinates and NOAA solar geometry,
  with generous elevation margins over the whole logging interval. There are no
  Colorado-specific UTC night hours; unknown location skips geometric diagnosis.
- Broad screening bounds are configurable and described as screening limits, not
  proof of universal physical impossibility. Small negative irradiance offsets are
  allowed; RH above tolerance is withheld rather than capped as a fake calibration.
- Peer comparisons use a geographic index and at most five nearby, similar-elevation
  sites at the same cadence. Local exclusions are masked; a minimum of three valid
  peers is required. Provisional pressure regimes remain available to recognize a
  common weather change. Fixed site/elevation offsets are removed through residuals.
- Shared interval masks count overlapping exclusions once. Weather checks use the
  same schema, geometry and interval helpers; no observations are copied from peers.
- Humidity failure requires sustained saturation, actual dry-peer votes and bright
  conditions, while humid peer-supported saturation is kept. Sunshine or four hours
  at 100% alone no longer establishes sensor blockage.
- Isolated pressure spikes exclude only the spike; sustained baseline changes are
  withheld pending calibration. A common pressure jump supported by peers is kept.
- Wind replacements are checked for persistent changes in relative scale, with
  hourly onset uncertainty. No specific unit conversion or calibration is assumed.
- Rain-gauge obstruction requires the maintenance record and adequately observed
  missed wet days; cleaning drainage is quarantined rather than assigned to invented
  rainfall times. The uncertain onset and conservative interval are stated explicitly.
- A sustained increase in daylight-versus-night temperature residual after shield/site
  work triggers a conservative daytime quarantine from the work date. The later
  statistical threshold crossing is not presented as the physical fault onset.
- All weather configuration overrides are validated; partial bounds merge safely
  with defaults. Unimplemented spike settings and an unused dependency were removed.
- Network failures are isolated per station, recorded in flags and reflected in the
  exit status. Maintenance provenance uses relevant record dates instead of chopped
  quotations that can omit the important evidence.

Validation: `python -m pytest -q` passes **149 tests**. New independent cases cover
shared weather versus isolated faults, pressure exposure offsets, humid fog, dry-peer
vote counts, replacements without scale changes, solar bias versus day-and-night
warming, uncertain fault onset, blockage/drainage, polar seasons, sunset intervals,
configuration round-tripping, benchmark isolation and per-station network failures.

Results on the supplied pack (dates and values below describe this pack, not rules
embedded in the implementation):

| Station/channel | Treatment and evidence |
| --- | --- |
| S02 pressure | Withhold 21 March 17:20 onward; its station-minus-peer baseline changes by about +6.4 hPa, without a reference calibration. |
| S03 temperature | Withhold the two 85 C readings and the 481-record exact flatline; these earlier detections remain. |
| S04 rain | Withhold 23 March 06:00 through 2 April 16:30 as a conservative interval after the last positive tip; the storm is missed and 33.4 mm appears near cleaning. Actual blockage onset is unknown. |
| S04 temperature | The clearest day/night residual change is 3.4-5.3 C over six days; withhold daytime above 10 degrees solar elevation after the 15 March site work, because earlier smaller bias cannot be ruled out. Nights remain outside this flag. |
| S05 humidity | Withhold the full 505-record near-saturation run, 7 March 12:00 through 11 March 00:00, including night; do not cap values. Retain the later peer-supported humid episode. |
| S05 location | Retain weather, use the trusted receiver position for working geometry, and flag the installation-sheet disagreement for site verification; the source metadata is not silently edited. |
| S06 time / S08 replay | Preserve the Phase 2a clock correction and replay removal; the genuine S08 gap remains excluded. |
| S07 wind | Withhold from a conservative 26 March 14:00 onset boundary; relative scale changes by roughly 0.23 after replacement, which does not establish a 3.6 or other exact conversion. |

The root `flags.csv` has been regenerated. The original 19 data-pack files remain
unchanged. The root `report.html` still needs the Phase 3 redesign and should not
be used as the final recommendation. Diagnostic output remains in ignored scratch/.

Reproducible validation benchmark: `python checker.py --benchmark` (no pack needed).
Generation, normalization, local checks, network checks and retained frames are
included; CSV/NMEA parsing and HTML are excluded. Memory is traced allocation,
not total process RSS. These measurements are specific to this machine:

| Stations, each with 6,048 records | Elapsed | Peak traced memory |
| --- | --- | --- |
| 8 | 0.50 s | 5.56 MiB |
| 100 | 5.55 s | 50.74 MiB |
| 500 | 29.15 s | 247.04 MiB |

Known limits: neighbour agreement cannot detect a fault common to the whole network;
unrecognized maintenance wording may need review; insufficient peers mean a diagnosis
is skipped, not that the data is certified. The solar heating and rain onset boundaries
are conservative judgements. Neither peer differences nor this checker replace a
calibration reference. See the formula reference in `src/solar.py`.

Suggested commit: `feat: validate weather faults with bounded peer evidence`

## Phase 3 - coordinator report and visit priority (complete)

- Coverage uses each station's cadence and logging phase within the common network
  period, cropped by its recorded installation date. Missing timestamps remain in
  the denominator; overlapping exclusions count once. Missing slots, excluded
  observations and pending corrections are disjoint. All seven sensor channels,
  including wind direction, are included.
- Keep-flags do not reduce eligibility or count as uncertainty. Clock/replay fixes
  count as applied only when the temporal pipeline records the treatment. Unknown
  or unapplied corrections remain pending. Failure rows remain visible as Hold,
  even if the station has no readable observations.
- Per-station treatments replace the old percentage-based "Include" shortcuts:
  a small excluded period cannot silently turn into an unqualified green light.
  The report explains that percentages assume listed time/replay treatments and
  are not a calibration score; original files remain unchanged.
- The visit policy ranks unresolved, on-site-checkable problems at the pack end.
  Historical gaps, recovered humidity/flatline episodes and discarded GPS packets
  cannot win by generating more flags. Recurring daylight problems remain live
  when the last observation is at night. Fragmenting an interval cannot raise rank.
- The supplied pack recommends **S04, Foothills Community Garden**, for the ongoing
  temperature exposure problem, with **S07, Riverbend Farm**, as runner-up. The
  report gives a timed one-hour shield/reference-temperature inspection, a check-first
  instruction, equipment, the reason for this order and a remote sensor-label/settings
  check for S07. This is an explicit triage policy, not a claim of mathematical optimality.
- A static, self-contained A4 template separates presentation from coverage and
  diagnosis. Dynamic text is HTML-escaped. Dates, names, fractions, treatments and
  visit steps come from the run. Larger networks show at most 12 priority rows and
  disclose omissions; qualifications are bounded rather than listing hundreds of IDs.
- CSV and HTML share an atomic writer. A failed replacement preserves the prior
  deliverable; output file symlinks are not followed to overwrite their targets.
  Once coverage is computed, the CLI releases station frames before rendering HTML.

Validation: `python -m pytest -q` passes **173 tests**; one file-symlink test is
skipped because Windows does not permit creating the link in this environment.
New cases cover overlapping intervals, absent edge/internal timestamps, missing
values, logging phases/cadences, installation dates, applied versus pending fixes,
failed stations, wind direction, visit ranking invariant to fragmentation, dynamic
report periods, HTML escaping, bounded large reports and atomic output failure.

The root `report.html` was rendered in headless Chrome and visually inspected as
**one A4 portrait page**. The preview PDF and image remain under ignored
`scratch/phase3/`; a PDF/browser dependency is not required to run the checker.

Key eligible shares after treatment in this pack:

| Station/channel | Share retained |
| --- | --- |
| S02 pressure | 49.3% |
| S03 temperature | 92.0% |
| S04 temperature / rain | 71.1% / 75.1% |
| S05 humidity | 91.7% |
| S06 all channels | 100%, after the +6 h treatment of 2,639 records |
| S07 wind speed | 60.9% |
| S08 all channels | 96.5%, after removing 144 replay rows; 214 slots remain missing |

The existing `flags.csv` decisions are unchanged. The original 19 data-pack files
remain unchanged. Final submission preparation, including the candidate-authored
decisions and recording, remains in Phase 4.

Suggested commit: `feat: add actionable network report and evidence-based visit triage`

## Phase 4 - submission preparation

- Replace the root data-pack README with run/install instructions, checks,
  assumptions and limitations; start with name, email, video link and `Run:`.
- The candidate writes `DECISIONS.md` in their own words, within 1.5 pages. The
  brief explicitly requires the S04 solar-noon working to be done without code or
  AI; that answer must come from the candidate.
- Add complete, unedited AI conversation exports under `ai_logs/`; do not substitute
  a generated summary for the required exports.
- The candidate records the three-minute evidence walkthrough and supplies its link.
  Check repository naming and the submission checklist before submission.

No commits or publication are performed automatically at these checkpoints.
