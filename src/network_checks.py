"""Bounded peer comparisons corroborate faults; they never supply calibration factors.

References exclude local faults, require simultaneous observations, and use a small
set of nearby sites with similar elevation. Fixed site offsets are removed through
before/after residuals. Maintenance notes corroborate, rather than prove, faults.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from .config import CheckerConfig
from .evidence import interval_mask
from .intervals import find_runs
from .maintenance import MaintenanceEvent
from .schema import TS_FMT
from .solar import interval_sun_masks

logger = logging.getLogger(__name__)


@dataclass
class PeerEvidence:
    values: pd.Series
    count: pd.Series
    stations: tuple[str, ...]
    members: pd.DataFrame


@dataclass
class NetworkCheckResult:
    flags: dict[str, list[dict]]
    failures: dict[str, str]


class PeerIndex:
    """Index geography once; align at most peer_count series per channel and target."""

    def __init__(self, data: dict[str, pd.DataFrame], metadata: dict[str, dict],
                 flags: dict[str, list[dict]], configs: dict[str, CheckerConfig]):
        self.data, self.flags, self.configs = data, flags, configs
        self.ids, xyz, self.elevations = [], [], {}
        for sid in data:
            meta = metadata.get(sid, {})
            values = [meta.get(key, float("nan")) for key in ("lat", "lon", "elevation_m")]
            if not data[sid].empty and all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
                lat, lon, elevation = values
                if abs(lat) > 90 or abs(lon) > 180:
                    continue
                lat, lon = np.deg2rad([lat, lon])
                self.ids.append(sid)
                xyz.append([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])
                self.elevations[sid] = elevation
        self.xyz = np.asarray(xyz)
        self.tree = cKDTree(self.xyz) if self.ids else None
        self.positions = {sid: index for index, sid in enumerate(self.ids)}
        self._neighbors: dict[str, tuple[str, ...]] = {}

    def neighbors(self, sid: str) -> tuple[str, ...]:
        if sid in self._neighbors:
            return self._neighbors[sid]
        if sid not in self.positions:
            return ()
        cfg = self.configs[sid]
        # Cap geographical candidates too, so dense networks do not create all-pairs
        # observation comparisons. Too few eligible peers means no network diagnosis.
        distances, indices = self.tree.query(self.xyz[self.positions[sid]], k=min(len(self.ids), 8 * cfg.peer_count + 1))
        limit = 2 * np.sin(cfg.peer_radius_km / (2 * 6371))
        selected = []
        for distance, index in zip(np.atleast_1d(distances), np.atleast_1d(indices)):
            peer = self.ids[index]
            if peer == sid or distance > limit:
                continue
            if abs(self.elevations[peer] - self.elevations[sid]) > cfg.peer_max_elevation_difference_m:
                continue
            if self.configs[peer].sampling_interval_min != cfg.sampling_interval_min:
                continue
            selected.append(peer)
            if len(selected) == cfg.peer_count:
                break
        self._neighbors[sid] = tuple(selected)
        return self._neighbors[sid]

    def reference(self, sid: str, channel: str) -> PeerEvidence:
        times = pd.DatetimeIndex(self.data[sid].timestamp_utc)
        neighbors = self.neighbors(sid)
        series = {}
        for peer in neighbors:
            frame = self.data[peer]
            exclusions = self.flags[peer]
            if channel == "pressure_hpa":
                # Provisional baseline steps must remain available to establish a
                # network-wide weather change; isolated spikes stay masked.
                exclusions = [flag for flag in exclusions if not (
                    flag.get("check") == "pressure" and flag["start_utc"] != flag["end_utc"]
                )]
            valid = frame[channel].where(~interval_mask(frame.timestamp_utc, exclusions, channel))
            series[peer] = pd.Series(valid.to_numpy(), index=pd.DatetimeIndex(frame.timestamp_utc)).reindex(times)
        matrix = pd.DataFrame(series, index=times)
        count = matrix.count(axis=1)
        values = matrix.median(axis=1).where(count >= self.configs[sid].peer_min_count)
        return PeerEvidence(values, count, neighbors, matrix)


def _flag(channel: str, start, end, issue: str, action: str, note: str, replaces: tuple[str, ...] = ()) -> dict:
    return {"channel": channel, "start_utc": start, "end_utc": end, "issue": issue,
            "action": action, "note": note, "replaces": replaces}


def _pressure(frame: pd.DataFrame, local_flags: list[dict], reference: PeerEvidence,
              cfg: CheckerConfig) -> list[dict]:
    residual = pd.Series(frame.pressure_hpa.to_numpy(), index=reference.values.index) - reference.values
    hourly = residual.resample("1h").median()
    results = []
    for flag in local_flags:
        if flag.get("check") != "pressure" or flag["start_utc"] == flag["end_utc"]:
            continue
        time = flag["start_utc"]
        before = hourly.loc[time - pd.Timedelta(days=cfg.change_window_days):time - pd.Timedelta(hours=1)].dropna()
        after = hourly.loc[time:time + pd.Timedelta(days=cfg.change_window_days)].dropna()
        if min(len(before), len(after)) < cfg.minimum_peer_hours:
            continue
        change = after.median() - before.median()
        index = frame.timestamp_utc.searchsorted(time)
        observed_step = frame.pressure_hpa.iloc[index] - frame.pressure_hpa.iloc[index - 1]
        peer_step = reference.values.iloc[index] - reference.values.iloc[index - 1]
        if abs(change) < cfg.pressure_max_step_hpa / 2 and abs(peer_step) >= cfg.pressure_max_step_hpa:
            results.append(_flag("pressure_hpa", time, time, "Pressure change supported by peers", "keep",
                f"Station and peer pressure change together ({observed_step:+.1f} and {peer_step:+.1f} hPa) without a sustained residual shift; retain the weather change.", ("pressure",)))
        elif abs(change) >= cfg.pressure_max_step_hpa:
            results.append(_flag("pressure_hpa", time, flag["end_utc"], "Pressure baseline shift", "exclude",
                f"Station-minus-peer pressure shifts by {change:+.1f} hPa across comparison windows with at least {cfg.minimum_peer_hours} valid hours each, using {','.join(reference.stations)}; withhold the new regime because peer agreement does not establish an exact calibration.", ("pressure",)))
    return results


def _fit_step(series: pd.Series, event: MaintenanceEvent,
              cfg: CheckerConfig) -> tuple[pd.Timestamp, float, float] | None:
    """Robust hourly change point near a documented intervention, with bounded work."""
    anchor = pd.Timestamp(event.date)
    window = series.loc[anchor - pd.Timedelta(days=cfg.change_window_days):anchor + pd.Timedelta(days=cfg.change_window_days)].dropna()
    candidates = window.index[(window.index >= anchor - pd.Timedelta(days=cfg.event_search_days)) &
                              (window.index < anchor + pd.Timedelta(days=cfg.event_search_days + 1))]
    best = None
    for time in candidates:
        left, right = window.loc[window.index < time], window.loc[window.index >= time]
        if min(len(left), len(right)) < cfg.minimum_peer_hours:
            continue
        before, after = left.median(), right.median()
        loss = (left - before).abs().sum() + (right - after).abs().sum()
        if best is None or loss < best[0]:
            best = (loss, time, before, after)
    return best[1:] if best else None


def _wind(frame: pd.DataFrame, events: list[MaintenanceEvent], reference: PeerEvidence,
          cfg: CheckerConfig) -> list[dict]:
    speed = pd.Series(frame.wind_kmh.to_numpy(), index=reference.values.index)
    ratio = (speed.where(speed > 0) / reference.values.where(reference.values >= cfg.wind_min_reference_kmh))
    hourly = np.log(ratio).resample("1h").median()
    results = []
    for event in events:
        if not event.is_sensor_change or not event.mentions_channel("wind_kmh"):
            continue
        fit = _fit_step(hourly, event, cfg)
        if fit is None:
            continue
        time, before, after = fit
        factor = math.exp(after - before)
        if cfg.wind_scale_change_ratio < factor < 1 / cfg.wind_scale_change_ratio:
            continue
        later = hourly.loc[time:].resample("D").median().dropna()
        shifted = later.lt((before + after) / 2) if factor < 1 else later.gt((before + after) / 2)
        if len(later) < 3 or not shifted.tail(3).all() or shifted.mean() < 0.8:
            continue
        start = max(frame.timestamp_utc.min(), time - pd.Timedelta(hours=1))
        results.append(_flag("wind_kmh", start, frame.timestamp_utc.max(), "Wind scale changed after replacement", "exclude",
            f"Wind relative to {','.join(reference.stations)} changes by a factor of {factor:.2f} near {time.strftime(TS_FMT)} at hourly resolution after the {event.date} intervention; withhold from one hour earlier through the pack end until sensor pulse scaling is verified, without assuming a unit conversion."))
    return results


def _humidity(frame: pd.DataFrame, reference: PeerEvidence, cfg: CheckerConfig) -> list[dict]:
    results = []
    saturated = frame.rh_pct.ge(cfg.rh_saturation_threshold)
    for start, end, count in find_runs(frame, saturated, cfg.sampling_interval_min):
        duration = count * cfg.sampling_interval_min / 60
        peers = reference.values.iloc[start:end + 1]
        if peers.notna().mean() < 0.8:
            continue
        block = frame.iloc[start:end + 1]
        votes = reference.members.iloc[start:end + 1]
        dry = votes.lt(cfg.dry_peer_rh_pct).sum(axis=1).ge(cfg.peer_min_count)
        bright_dry_hours = (dry.to_numpy() & block.ghi_wm2.gt(cfg.sunny_ghi_wm2).to_numpy()).sum() * cfg.sampling_interval_min / 60
        if duration >= cfg.rh_saturation_min_hours and dry.mean() >= cfg.rh_dry_fraction and bright_dry_hours >= cfg.rh_bright_min_hours:
            results.append(_flag("rh_pct", block.timestamp_utc.iloc[0], block.timestamp_utc.iloc[-1], "Humidity saturation contradicts peer weather", "exclude",
                f"RH stays at {block.rh_pct.min():g}-{block.rh_pct.max():g}% for {duration:.1f} h including night, while at least {cfg.peer_min_count} peers are below {cfg.dry_peer_rh_pct:g}% for {100 * dry.mean():.0f}% of the run and sunlight exceeds {cfg.sunny_ghi_wm2:g} W/m2 during {bright_dry_hours:.1f} dry-peer hours; withhold the whole episode, without capping it to 100%.", ("humidity", "flatline")))
        elif duration >= cfg.rh_keep_min_hours and votes.ge(cfg.humid_peer_rh_pct).sum(axis=1).ge(cfg.peer_min_count).mean() >= 0.5 and bright_dry_hours == 0 and block.rh_pct.max() <= cfg.physical_bounds["rh_pct"]["max"]:
            results.append(_flag("rh_pct", block.timestamp_utc.iloc[0], block.timestamp_utc.iloc[-1], "Saturation supported by humid peers", "keep",
                f"Near-saturation lasts {duration:.1f} h and at least {cfg.peer_min_count} peers are also at or above {cfg.humid_peer_rh_pct:g}% for most of the episode, with no bright/dry contradiction; retain plausible humid weather."))
    return results


def _thermal(frame: pd.DataFrame, events: list[MaintenanceEvent], reference: PeerEvidence,
             meta: dict, cfg: CheckerConfig, local_flags: list[dict]) -> list[dict]:
    times = pd.DatetimeIndex(frame.timestamp_utc)
    valid_temp = frame.temp_c.where(~interval_mask(frame.timestamp_utc, local_flags, "temp_c"))
    residual = pd.Series(valid_temp.to_numpy(), index=times) - reference.values
    night, daylight = interval_sun_masks(frame.timestamp_utc, meta.get("lat"), meta.get("lon"), cfg.sampling_interval_min,
                                         cfg.solar_night_elevation_deg, cfg.solar_day_elevation_deg)
    sunny = frame.ghi_wm2.gt(cfg.sunny_ghi_wm2) & ~interval_mask(frame.timestamp_utc, local_flags, "ghi_wm2")
    day_residual, night_residual = residual.where(sunny.to_numpy()), residual.where(night.to_numpy())
    day = day_residual.resample("D").median().where(day_residual.resample("D").count() >= math.ceil(60 / cfg.sampling_interval_min))
    dark = night_residual.resample("D").median().where(night_residual.resample("D").count() >= math.ceil(120 / cfg.sampling_interval_min))
    contrast = day - dark
    results = []
    for event in events:
        if not event.is_sensor_change or not event.mentions_channel("temp_c"):
            continue
        anchor = pd.Timestamp(event.date)
        before = contrast.loc[anchor - pd.Timedelta(days=cfg.thermal_baseline_days):anchor - pd.Timedelta(days=1)].dropna()
        if len(before) < 3:
            continue
        baseline = before.median()
        excess = contrast - baseline
        elevated = excess.ge(cfg.thermal_delta_c) & (contrast.index >= anchor)
        supported = [run for run in find_runs(contrast.index.to_series(), pd.Series(elevated, index=contrast.index), 1440)
                     if run[2] >= cfg.thermal_min_days]
        if not supported:
            continue
        first, last, count = max(supported, key=lambda run: run[2])
        # A late threshold crossing establishes a problem, not a known fault onset.
        # Protect the climate summary from smaller earlier biases after the same work.
        period = frame.timestamp_utc.ge(anchor) & daylight & frame.ghi_wm2.gt(0)
        for start, end, _ in find_runs(frame, period, cfg.sampling_interval_min):
            results.append(_flag("temp_c", frame.timestamp_utc.iloc[start], frame.timestamp_utc.iloc[end], "Persistent sunlight-linked temperature bias", "exclude",
                f"Day-minus-night temperature residual against {','.join(reference.stations)} exceeds its baseline by {excess.iloc[first:last+1].min():.1f}-{excess.iloc[first:last+1].max():.1f} C on {count} consecutive days; onset after the {event.date} shield/site work is unknown, so conservatively withhold daytime readings above {cfg.solar_day_elevation_deg:g} degrees solar elevation from the work date, without subtracting a guessed offset."))
    return results


def _rain(frame: pd.DataFrame, events: list[MaintenanceEvent], reference: PeerEvidence,
          cfg: CheckerConfig) -> list[dict]:
    times = pd.DatetimeIndex(frame.timestamp_utc)
    rain = pd.Series(frame.rain_mm.to_numpy(), index=times)
    daily = rain.resample("D").sum(min_count=1)
    complete = rain.resample("D").count() >= 0.8 * 1440 / cfg.sampling_interval_min
    peer_daily = reference.members.resample("D").sum(min_count=1)
    peer_daily = peer_daily.where(reference.members.resample("D").count() >= 0.8 * 1440 / cfg.sampling_interval_min)
    wet_votes = peer_daily.ge(cfg.rain_peer_wet_mm).sum(axis=1).ge(cfg.peer_min_count)
    results = []
    for event in events:
        if not event.rain_obstruction:
            continue
        anchor = pd.Timestamp(event.date)
        around_cleaning = (times >= anchor - pd.Timedelta(days=cfg.event_search_days)) & (times < anchor + pd.Timedelta(days=cfg.event_search_days + 1))
        burst = rain.ge(cfg.rain_cleaning_burst_mm) & rain.gt(cfg.rain_burst_peer_ratio * (reference.values + 0.2)) & around_cleaning
        missed = daily.eq(0) & wet_votes & complete & (daily.index < anchor + pd.Timedelta(days=1))
        # Restrict to the ongoing zero run immediately before the cleaning.
        cutoff = rain.index[burst][0] - pd.Timedelta(minutes=cfg.sampling_interval_min) if burst.any() else anchor
        preceding = rain.loc[:cutoff].loc[lambda values: values > 0]
        lower = preceding.index[-1] + pd.Timedelta(minutes=cfg.sampling_interval_min) if len(preceding) else times[0]
        wet_days = daily.index[missed & (daily.index >= lower.normalize())]
        if len(wet_days) < cfg.rain_wet_days:
            continue
        end = rain.index[burst][-1] if burst.any() else min(times[-1], anchor + pd.Timedelta(days=2) - pd.Timedelta(minutes=cfg.sampling_interval_min))
        if rain.loc[wet_days[0]:end].drop(rain.index[burst], errors="ignore").gt(0).any():
            continue
        burst_mm = rain.loc[burst].sum()
        beginning = "After the last positive tip" if len(preceding) else "From the pack start, with no earlier positive tip available"
        if burst.any():
            issue = "Blocked gauge and delayed drainage"
            ending = f"then records {burst_mm:g} mm near the documented {event.date} blockage cleaning"
        else:
            issue = "Blocked gauge; cleaning time uncertain"
            ending = f"with blockage documented on {event.date} but no observed drainage burst, so the uncertain cleaning boundary is extended through the following UTC day"
        results.append(_flag("rain_mm", lower, end, issue, "exclude",
            f"{beginning}, the gauge stays dry on {len(wet_days)} adequately observed days when at least {cfg.peer_min_count} peers imply wet weather, {ending}; onset is uncertain before {wet_days[0].strftime('%Y-%m-%d')}, so withhold this conservative interval and do not redistribute drainage as rainfall."))
    return results


def _station_checks(sid: str, index: PeerIndex, meta: dict,
                    notes: list[MaintenanceEvent]) -> list[dict]:
    frame, cfg, local = index.data[sid], index.configs[sid], index.flags[sid]
    if frame.empty or len(index.neighbors(sid)) < cfg.peer_min_count:
        return local
    additions, checks = [], []
    if frame.rh_pct.ge(cfg.rh_saturation_threshold).any():
        checks.append(("rh_pct", lambda reference: _humidity(frame, reference, cfg)))
    if any(flag.get("check") == "pressure" for flag in local):
        checks.append(("pressure_hpa", lambda reference: _pressure(frame, local, reference, cfg)))
    if any(event.is_sensor_change and event.mentions_channel("wind_kmh") for event in notes):
        checks.append(("wind_kmh", lambda reference: _wind(frame, notes, reference, cfg)))
    if any(event.is_sensor_change and event.mentions_channel("temp_c") for event in notes):
        checks.append(("temp_c", lambda reference: _thermal(frame, notes, reference, meta, cfg, local)))
    if any(event.rain_obstruction for event in notes):
        checks.append(("rain_mm", lambda reference: _rain(frame, notes, reference, cfg)))
    for channel, check in checks:
        additions.extend(check(index.reference(sid, channel)))
    retained = []
    for old in local:
        replaced = any(old.get("check") in new.get("replaces", ()) and old["channel"] == new["channel"]
                       and new["start_utc"] <= old["start_utc"] and
                       (old["end_utc"] <= new["end_utc"] or
                        (new["action"] == "keep" and old["start_utc"] == new["start_utc"])) for new in additions)
        if not replaced:
            retained.append(old)
    return retained + additions


def run_network_checks(data: dict[str, pd.DataFrame], metadata: dict[str, dict],
                       flags: dict[str, list[dict]], configs: dict[str, CheckerConfig],
                       events: dict[str, list[MaintenanceEvent]]) -> NetworkCheckResult:
    """Isolate a station's failed diagnosis and return explicit failures to the CLI."""
    index = PeerIndex(data, metadata, flags, configs)
    results, failures = {}, {}
    for sid in data:
        try:
            results[sid] = _station_checks(sid, index, metadata.get(sid, {}), events.get(sid, []))
        except Exception as exc:
            logger.error("%s: network evidence failed: %s", sid, exc, exc_info=True)
            results[sid], failures[sid] = flags[sid], str(exc)
    return NetworkCheckResult(results, failures)
