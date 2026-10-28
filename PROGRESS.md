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

## Repository review and Phase 2 - evidence and checker reliability (next)

Reviewed the CLI, all source modules, tests, requirements, ignore rules, README,
existing outputs, pack metadata and notes, and record/time summaries for all eight
observation and GPS files. The following findings remain open:

- **Clock evidence is missed.** Valid, checksum-passing S06 RMC sentences show logger
  timestamps approximately 360 minutes behind GPS time after the reboot. Preserve
  source row order and compare full GPS date/time with logger timestamps before
  interpreting sensor steps. The sorted S06 data has 72 rows at duplicate times.
- **Replay and missing time need distinct treatments.** S08 has 288 rows sharing
  144 timestamps on 5 April, plus 214 absent ten-minute slots on 16-18 March.
  Compare duplicate row contents; do not select a supposedly plausible row without
  evidence or treat a clock rollback as an ordinary replay.
- **Corrections are too speculative.** Pressure changes are labelled correctable
  without a measured offset or its applicable period; RH is capped without
  establishing a calibration error. Establish a reproducible correction or withhold
  the affected values with an explicit reason.
- **GPS validity is mishandled.** Position calculations include invalid fixes and
  bad checksums, sentence counts overlap, and most GPS flags have no time bounds.
  S05 has an apparent position disagreement requiring validation with trusted fixes.
- **Weather checks need context.** Night hours assume Colorado, long saturation
  alone is treated as sensor failure, and station comparisons are absent. Inspect
  evidence around S04's shield-post painting and rain-gauge cleaning, S07's
  replacement anemometer, and S05's humidity note. These are investigation leads,
  not established fault intervals or automatic correction instructions.
- **Input and failure handling need attention.** Ingestion sorts away clock order,
  silently caps row counts, and can discard invalid timestamps with only a warning;
  a missing station file can disappear from the result. Per-check and report failures
  can be logged while the command still returns success. Validate configuration
  and use station cadence consistently.
- **Tests leave gaps.** The existing source-overwrite test is a placeholder;
  meaningful GPS clock, replay, station-summary and failure-path tests are needed.
  Benchmark data uses a different sunlight timing assumption from the checker,
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
