# Decisions

## 1. The visit

I would send the technician to S04, Foothills Community Garden. First, inspect the radiation shield, airflow and contact with the painted support. Spend 10 minutes inspecting, 30 minutes comparing readings with a shaded reference thermometer and correcting any obvious obstruction, then 20 minutes documenting the intervention and checking readings.
S04’s temperature bias remains relevant to the climate summary. S07 is second: its wind-sensor model and logger settings could first be checked remotely using photographs.

## 2. My least certain call

The uncertain decision is excluding S04 rain from 23 March 06:00. Missed wet days, the blockage note and the later 33.4 mm drainage burst support exclusion. However, earlier zeroes could represent genuine dry weather, and occasional tips do not prove the funnel was completely clear.
A trusted, co-located rainfall record covering this period would distinguish genuine zeroes from missed rainfall and allow a better exclusion boundary.

## 3. Where the AI was wrong

An exploratory AI script used:
fix.raw_line.split(',', 1)[1].split(',')
This incorrectly removed the NMEA message identifier: raw_line already contained only the sentence. It produced no clock offsets despite valid RMC records. Comparing the parser’s output with the raw sentence exposed the shifted field positions. Describe your own review of this example rather than claiming you originally caught it.

## 4. Next year: 500 stations

I would add versioned sensor metadata, monitored ingestion and station-level incidents that combine related flags. Alerts should prioritise ongoing failures and require acknowledgement.
I would retain isolated GPS checksum failures in the audit trail but stop sending each one for human review. I would refuse automatic calibration from neighbour agreement alone, rainfall backdating and ambiguous clock corrections without stronger evidence.

## 5. By hand

S04 is approximately 105° west. Earth turns about 15° per hour, giving a seven-hour longitude offset: 
12:00 + 105 ÷ 15 hours ≈ 19:00 UTC
Around 20 March, the equation-of-time correction adds roughly eight minutes, giving about 19:08 UTC. This follows the NOAA solar-noon relationship.
It helps check whether sunlight timing is plausible and identify possible logger-clock errors. A cloudy-day irradiance maximum need not occur exactly at solar noon.