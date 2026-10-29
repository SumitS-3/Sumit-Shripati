Sumit Shripati
sumitshripati5391@gmail.com
Video:  
Run: python checker.py "<path-to-data-pack>"

# Ridgeline weather data checker

Checks weather-station observations, GPS sentences and maintenance notes to
identify usable data and prioritise one technician visit. The program generates
`flags.csv` and a self-contained, A4-printable `report.html`.

## Install and run

From the repository root, with Python 3.12:

```sh
python -m pip install -r requirements.txt
python checker.py "<path-to-data-pack>"
```

Replace `<path-to-data-pack>` with the actual folder containing:

```text
stations.csv
maintenance_log.md
data/Sxx.csv
gps/Sxx.nmea
```

The command accepts any pack in this format. Quote paths containing spaces.
Both outputs are written to the **current working directory**. The source data
is never rewritten, and the private data pack must not be committed.

To preserve existing outputs, choose an existing destination folder:

```sh
python checker.py "<path-to-data-pack>" --output-dir "<output-directory>"
```

Exit codes: `0` completed, including any quality exclusions; `1` fatal failure;
`2` partial station/report failure. Failed station analyses appear in the flags.

## What is checked

| Check | Treatment |
| --- | --- |
| Completeness and input | Validate timestamps and values; identify absent intervals while preserving source order. |
| GPS and clock | Verify checksums, fix status, position and full RMC date/time; require repeated evidence and a matching observation break for a clock correction. |
| Replays and conflicts | Remove identical replay rows; withhold conflicting duplicates. |
| Bounds and flatlines | Screen all seven channels, allowing calm/dry intervals and small nighttime irradiance offsets. |
| Sunlight | Use coordinates and whole-interval solar geometry instead of fixed UTC night hours. |
| Pressure and wind | Compare changes with bounded nearby peer sets; distinguish shared weather from suspect baseline or scale changes. |
| Humidity | Combine persistent saturation, dry-peer votes and sunlight; retain supported humid weather without automatic RH capping. |
| Temperature exposure | Compare daylight/night residuals around site work; conservatively withhold supported exposure bias. |
| Rain obstruction | Corroborate maintenance with missed wet days; withhold uncertain blockage/drainage periods without backdating rainfall. |

`flags.csv` uses the required columns:

```text
station_id,channel,start_utc,end_utc,issue,action,note
```

Times are UTC (`YYYY-MM-DD HH:MM`). `exclude` withholds the channel/window,
`correct` specifies a treatment, and `keep` retains unusual but supported weather.
GPS `metadata` exclusions do not automatically discard weather observations.

The checker assesses a corrected, deduplicated working timeline; it does not
export a cleaned data pack. Apply the documented treatments when constructing
the climate summary. Coverage includes missing slots and counts overlap once.

## Review and verification

Open [report.html](report.html) in a browser and print on A4 with browser
headers/footers disabled. The supplied pack's report was verified as one page.
[flags.csv](flags.csv) contains exact windows and evidence; [PROGRESS.md](PROGRESS.md)
records the implementation phases and measured results.

```sh
python -m pytest -q
python checker.py --benchmark
```

Tested on Python 3.12.8, pandas 3.0.0, NumPy 2.4.1 and SciPy 1.17.0.
The synthetic benchmark includes generation and validation, but excludes file
parsing and HTML rendering; traced allocations are not total process RAM.

Thresholds are in [src/config.py](src/config.py). Override them using
`--config settings.json` or the pack's `config.json`; invalid settings are rejected.
Use `--verbose` for diagnostic logging.

## Assumptions and remaining limitations

Peer comparisons need matching cadence, nearby locations and similar elevation.
Agreement is evidence, not calibration; insufficient peers skip a diagnosis
rather than certify the data. Maintenance wording may require human review.

Automatic recalibration, reconstructed rainfall, independent field validation
and detection of faults shared by the whole network are not provided. S04's
fault-onset boundaries remain uncertain, so its exclusions are conservative.

