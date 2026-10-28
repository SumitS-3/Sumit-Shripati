# Ridgeline Schools Weather Network: 6-week data pack

A small volunteer-run network of 8 automatic weather stations at schools, farms and community
buildings across a mountain front. Each station has a data logger that records every 10 minutes and
sends data over a mobile link. Each logger also has a GPS receiver that is meant to keep the clock
right; the logger writes the GPS sentences it receives to a separate file.

The stations are maintained by volunteers. Nobody has checked this data carefully.

## Files

| Path | What it is |
|---|---|
| `stations.csv` | One row per station: id, name, lat/lon (decimal degrees, from the install sheet), elevation (m), install date, logging interval |
| `data/Sxx.csv` | Logger records. One row per 10-minute interval, timestamped by the logger clock (meant to be UTC) |
| `gps/Sxx.nmea` | `logger timestamp,NMEA sentence`, about one per hour. Standard NMEA 0183 `$GPRMC` and `$GPGGA` |
| `maintenance_log.md` | Volunteers' notes, copied from a shared notebook |

## Columns in `data/Sxx.csv`

| Column | Unit | Notes |
|---|---|---|
| `timestamp_utc` | `YYYY-MM-DD HH:MM` | end of the 10-minute interval, logger clock |
| `temp_c` | °C | air temperature, in a passive radiation shield |
| `rh_pct` | % | relative humidity |
| `pressure_hpa` | hPa | station (not sea-level) pressure |
| `wind_kmh` | km/h | 10-minute mean wind speed |
| `wind_dir_deg` | degrees | direction the wind blows from |
| `rain_mm` | mm | rain in the interval, 0.2 mm tipping bucket |
| `ghi_wm2` | W/m² | global horizontal irradiance |

## Licence and source

The weather values are based on ERA5 reanalysis (Copernicus Climate Change Service, via the
Open-Meteo archive API, CC BY 4.0). Station names, people, hardware and notes are fictional.
Use this pack only for this assignment.
