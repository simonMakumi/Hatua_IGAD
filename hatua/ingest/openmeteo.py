"""Open-Meteo connector: weather forecast and GloFAS river discharge.

This is the numeric backbone. Two properties make it unusually good for this
project:

* **Multi-coordinate requests.** Passing comma-separated latitudes and
  longitudes returns an array, so all eight IGAD capitals — or a whole batch of
  admin-2 centroids — cost one HTTP call. That matters when you are scoring
  ~900 districts against a free-tier rate limit.

* **The Flood API is GloFAS without Copernicus.** ``flood-api.open-meteo.com``
  serves GloFAS v4 river discharge, keyless, with a 210-day horizon. Getting
  the same data from the Copernicus CDS means an async job queue and GRIB
  parsing. This endpoint is a straight JSON GET.

Verified limits (25 Jul 2026): ``forecast_days`` max is 16 — asking for 17
returns an error whose message is itself buggy ("Given 16"). Roughly 10,000
calls/day on the free tier.
"""

from __future__ import annotations

from datetime import date, datetime

from ..models import HazardType, SignalReading
from ..config import OPEN_METEO_FLOOD, OPEN_METEO_FORECAST
from .base import Fetcher, IngestResult, normalise_linear, utcnow

MAX_FORECAST_DAYS = 16

# Thresholds. Rainfall figures are tuned for the Greater Horn, where the
# seasonal rains (MAM and OND) mean a wet week is normal and it is the extreme
# tail that signals flooding.
RAIN_7D_ADVISORY_MM = 50.0
RAIN_7D_SEVERE_MM = 200.0
RAIN_DAILY_HEAVY_MM = 30.0
RAIN_DAILY_EXTREME_MM = 100.0
HEAT_INDEX_LOW_C = 35.0
HEAT_INDEX_HIGH_C = 45.0


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def fetch_forecast(
    fetcher: Fetcher,
    points: dict[str, tuple[float, float]],
    *,
    days: int = MAX_FORECAST_DAYS,
    batch_size: int = 50,
) -> IngestResult:
    """Fetch rainfall and temperature forecasts for a set of keyed points.

    ``points`` maps an identifier (P-code or ISO3) to (lat, lon).
    """
    result = IngestResult()
    days = min(days, MAX_FORECAST_DAYS)
    keys = list(points)

    for batch in _chunk(keys, batch_size):
        lats = ",".join(str(points[k][0]) for k in batch)
        lons = ",".join(str(points[k][1]) for k in batch)

        params = {
            "latitude": lats,
            "longitude": lons,
            "daily": ",".join(
                [
                    "precipitation_sum",
                    "precipitation_probability_max",
                    "temperature_2m_max",
                    "temperature_2m_min",
                ]
            ),
            "forecast_days": days,
            "timezone": "auto",
        }
        payload, health = await fetcher.get_json(
            OPEN_METEO_FORECAST, params=params, cache_s=3600
        )
        health.source = "open_meteo_forecast"
        result.health.append(health)
        if payload is None:
            continue

        # A single coordinate returns an object; multiple return a list.
        entries = payload if isinstance(payload, list) else [payload]
        for key, entry in zip(batch, entries):
            result.readings.extend(
                _readings_from_forecast(key, points[key], entry, days)
            )
        result.health[-1].records = len(result.readings)

    return result


def _readings_from_forecast(
    key: str, point: tuple[float, float], entry: dict, days: int
) -> list[SignalReading]:
    daily = entry.get("daily") or {}
    times = daily.get("time") or []
    precip = daily.get("precipitation_sum") or []
    tmax = daily.get("temperature_2m_max") or []
    prob = daily.get("precipitation_probability_max") or []
    if not times:
        return []

    lat, lon = point
    now = utcnow()
    url = f"{OPEN_METEO_FORECAST}?latitude={lat}&longitude={lon}"
    out: list[SignalReading] = []

    def d(i: int) -> date | None:
        try:
            return date.fromisoformat(times[i])
        except (IndexError, ValueError):
            return None

    # --- 7-day rainfall accumulation: the flood-relevant aggregate ---
    window = [p for p in precip[:7] if p is not None]
    if window:
        total = sum(window)
        out.append(
            SignalReading(
                source="open_meteo_forecast",
                source_url=url,
                dataset="precipitation_sum_7d",
                hazard=HazardType.HEAVY_RAIN,
                pcode=key,
                lat=lat,
                lon=lon,
                value=round(total, 1),
                unit="mm",
                normalised=normalise_linear(
                    total, RAIN_7D_ADVISORY_MM, RAIN_7D_SEVERE_MM
                ),
                valid_from=d(0),
                valid_to=d(min(6, len(times) - 1)),
                retrieved_at=now,
            )
        )

    # --- Peak single-day rainfall: the flash-flood signal ---
    daily_pairs = [(i, p) for i, p in enumerate(precip[:days]) if p is not None]
    if daily_pairs:
        peak_i, peak = max(daily_pairs, key=lambda t: t[1])
        confidence_note = None
        if peak_i < len(prob) and prob[peak_i] is not None:
            confidence_note = f"precipitation probability {prob[peak_i]}%"
        out.append(
            SignalReading(
                source="open_meteo_forecast",
                source_url=url,
                dataset="precipitation_daily_peak",
                hazard=HazardType.FLOOD,
                pcode=key,
                lat=lat,
                lon=lon,
                value=round(peak, 1),
                unit="mm/day",
                normalised=normalise_linear(
                    peak, RAIN_DAILY_HEAVY_MM, RAIN_DAILY_EXTREME_MM
                ),
                valid_from=d(peak_i),
                valid_to=d(peak_i),
                retrieved_at=now,
                note=confidence_note,
            )
        )

    # --- Heat stress ---
    temps = [t for t in tmax[:days] if t is not None]
    if temps:
        peak_t = max(temps)
        out.append(
            SignalReading(
                source="open_meteo_forecast",
                source_url=url,
                dataset="temperature_2m_max",
                hazard=HazardType.HEAT_STRESS,
                pcode=key,
                lat=lat,
                lon=lon,
                value=round(peak_t, 1),
                unit="degC",
                normalised=normalise_linear(
                    peak_t, HEAT_INDEX_LOW_C, HEAT_INDEX_HIGH_C
                ),
                valid_from=d(0),
                valid_to=d(min(days - 1, len(times) - 1)),
                retrieved_at=now,
            )
        )

    # --- Dry spell: consecutive days with negligible rain, drought precursor ---
    dry_run = longest = 0
    for p in precip[:days]:
        if p is not None and p < 1.0:
            dry_run += 1
            longest = max(longest, dry_run)
        else:
            dry_run = 0
    if longest >= 7:
        out.append(
            SignalReading(
                source="open_meteo_forecast",
                source_url=url,
                dataset="dry_spell_days",
                hazard=HazardType.DROUGHT,
                pcode=key,
                lat=lat,
                lon=lon,
                value=float(longest),
                unit="days",
                normalised=normalise_linear(longest, 7, 16),
                valid_from=d(0),
                valid_to=d(min(days - 1, len(times) - 1)),
                retrieved_at=now,
                note="consecutive forecast days with <1mm rainfall",
            )
        )

    return out


async def fetch_flood(
    fetcher: Fetcher,
    points: dict[str, tuple[float, float]],
    *,
    days: int = 90,
    batch_size: int = 50,
) -> IngestResult:
    """GloFAS river discharge. Flags anomalies against the model's own
    climatological mean, which is more meaningful than an absolute threshold
    because a discharge of 500 m3/s means very different things on the Tana and
    on the Blue Nile."""
    result = IngestResult()
    keys = list(points)

    for batch in _chunk(keys, batch_size):
        params = {
            "latitude": ",".join(str(points[k][0]) for k in batch),
            "longitude": ",".join(str(points[k][1]) for k in batch),
            "daily": "river_discharge,river_discharge_mean",
            "forecast_days": days,
        }
        payload, health = await fetcher.get_json(
            OPEN_METEO_FLOOD, params=params, cache_s=6 * 3600
        )
        health.source = "open_meteo_flood"
        result.health.append(health)
        if payload is None:
            continue

        entries = payload if isinstance(payload, list) else [payload]
        now = utcnow()
        for key, entry in zip(batch, entries):
            daily = entry.get("daily") or {}
            times = daily.get("time") or []
            disc = [d for d in (daily.get("river_discharge") or []) if d is not None]
            mean = [
                d for d in (daily.get("river_discharge_mean") or []) if d is not None
            ]
            if not disc or not mean:
                continue

            peak = max(disc)
            baseline = sum(mean) / len(mean)
            if baseline <= 0:
                continue
            ratio = peak / baseline

            peak_idx = disc.index(peak)
            try:
                peak_date = date.fromisoformat(times[peak_idx])
            except (IndexError, ValueError):
                peak_date = None

            result.readings.append(
                SignalReading(
                    source="open_meteo_flood",
                    source_url=f"{OPEN_METEO_FLOOD}?latitude={points[key][0]}"
                    f"&longitude={points[key][1]}",
                    dataset="glofas_river_discharge",
                    hazard=HazardType.FLOOD,
                    pcode=key,
                    lat=points[key][0],
                    lon=points[key][1],
                    value=round(ratio, 2),
                    unit="x climatological mean",
                    # 1.5x mean is notable; 3x is a major flood signal.
                    normalised=normalise_linear(ratio, 1.5, 3.0),
                    valid_from=date.fromisoformat(times[0]) if times else None,
                    valid_to=peak_date,
                    retrieved_at=now,
                    note=f"peak {peak:.1f} m3/s vs mean {baseline:.1f} m3/s "
                    f"(GloFAS v4)",
                )
            )
        result.health[-1].records = len(result.readings)

    return result
