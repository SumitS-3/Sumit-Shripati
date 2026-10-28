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

## Repository review and Phase 2b - weather evidence (next)

Reviewed the CLI, all source modules, tests, requirements, ignore rules, README,
existing outputs, pack metadata and notes, and record/time summaries for all eight
observation and GPS files. Timing findings are resolved in Phase 2a; the following
weather findings remain open:

- **Corrections are too speculative.** Pressure changes are labelled correctable
  without a measured offset or its applicable period; RH is capped without
  establishing a calibration error. Establish a reproducible correction or withhold
  the affected values with an explicit reason.
- **Location disagreement remains a judgement call.** S05's trusted GPS fixes and
  its installation latitude disagree; establish what this means for station metadata
  and spatial comparisons before making a correction.
- **Weather checks need context.** Night hours assume Colorado, long saturation
  alone is treated as sensor failure, and station comparisons are absent. Inspect
  evidence around S04's shield-post painting and rain-gauge cleaning, S07's
  replacement anemometer, and S05's humidity note. These are investigation leads,
  not established fault intervals or automatic correction instructions.
- **Remaining validation and benchmarking.** Validate weather threshold overrides
  as well as timing settings. The older full-checker benchmark in `src/benchmark.py`
  uses a different sunlight timing assumption from the checker,
  and its memory measurement excludes creation of the retained datasets.

Implement and test these checks using evidence from the pack and independent
fixtures, without rules tied to particular station IDs or anomaly dates. Regenerate
root `flags.csv` once the checker decisions are defensible.

## Phase 3 - coordinator report and visit priority

- Count the union of affected slots so overlapping flags cannot double-count loss;
  include absent slots in completeness and distinguish corrections still required.
- Replace the hardcoded observation dates and station-level "Include" shortcuts
  with actual periods and channel-specific instructions.
- Rank visits by current, fixable problems and likely benefit, rather than the
  number of flags. Provide an evidence-based one-hour task and runner-up comparison.
- Replace the generic inspection list and unsupported limitations with the findings
  actually established in Phase 2. Verify an A4 print layout of at most two pages.

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
