"""GDACS connector — Global Disaster Alert and Coordination System.

GDACS is the closest thing to an authoritative "is something already happening"
feed, and it ships alert levels (Green/Orange/Red) that have already been
through a severity model. We treat an active Orange or Red GDACS event as a
strong prior rather than recomputing it.

**The gotcha that will cost you an afternoon:** ``eventlist=EQ;TC;FL;DR``
silently drops everything after the first type — the server 302-redirects to
``?eventlist=EQ``. You must issue one request per hazard type. Also
``EVENTS4APP`` is ~95% wildfires and returned zero Horn of Africa events on
test, so use ``SEARCH``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..config import GDACS_API, GDACS_GEOMETRY
from ..models import HazardType, IGAD_COUNTRIES, SignalReading
from .base import Fetcher, IngestResult, utcnow

# GDACS event type codes -> our taxonomy.
EVENT_TYPES: dict[str, HazardType] = {
    "DR": HazardType.DROUGHT,
    "FL": HazardType.FLOOD,
    "TC": HazardType.FLOOD,      # tropical cyclone; coastal flooding is the impact
    "EQ": HazardType.EARTHQUAKE,
    "WF": HazardType.WILDFIRE,
}

ALERT_INTENSITY: dict[str, float] = {
    "green": 0.25,
    "orange": 0.65,
    "red": 0.95,
}

_COUNTRY_ALIASES = {name.lower(): iso3 for iso3, name in IGAD_COUNTRIES.items()}
_COUNTRY_ALIASES.update(
    {
        "united republic of tanzania": None,  # explicitly not IGAD
        "south sudan": "SSD",
        "sudan": "SDN",
    }
)


def _match_countries(text: str) -> list[str]:
    """GDACS reports affected countries as free text, so we match by name."""
    found: list[str] = []
    lowered = (text or "").lower()
    for name, iso3 in _COUNTRY_ALIASES.items():
        if iso3 and name in lowered and iso3 not in found:
            found.append(iso3)
    return found


async def fetch_events(
    fetcher: Fetcher,
    *,
    lookback_days: int = 120,
    event_types: list[str] | None = None,
) -> IngestResult:
    """Fetch active GDACS events touching IGAD member states."""
    result = IngestResult()
    types = event_types or ["DR", "FL", "TC", "EQ"]
    today = date.today()
    since = today - timedelta(days=lookback_days)
    now = utcnow()

    for code in types:
        # One request per type — see module docstring.
        params = {
            "eventlist": code,
            "fromdate": since.isoformat(),
            "todate": today.isoformat(),
        }
        payload, health = await fetcher.get_json(
            GDACS_API, params=params, cache_s=3600
        )
        health.source = f"gdacs_{code}"
        result.health.append(health)
        if not payload:
            continue

        features = payload.get("features") if isinstance(payload, dict) else None
        if not features:
            continue

        count = 0
        for feat in features:
            props = feat.get("properties") or {}
            affected = props.get("affectedcountries") or []
            names = " ".join(
                c.get("countryname", "") for c in affected if isinstance(c, dict)
            )
            names = f"{names} {props.get('country', '')} {props.get('htmldescription', '')}"
            iso3s = _match_countries(names)
            if not iso3s:
                continue

            alert = str(props.get("alertlevel", "green")).lower()
            intensity = ALERT_INTENSITY.get(alert, 0.25)
            hazard = EVENT_TYPES.get(code, HazardType.FLOOD)

            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or [None, None]
            lon, lat = (coords + [None, None])[:2] if isinstance(coords, list) else (None, None)

            def _parse(v: str | None) -> date | None:
                if not v:
                    return None
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
                except ValueError:
                    return None

            for iso3 in iso3s:
                result.readings.append(
                    SignalReading(
                        source="gdacs",
                        source_url=f"{GDACS_API}?eventlist={code}"
                        f"&fromdate={since}&todate={today}",
                        dataset=f"gdacs_{code}_alert",
                        hazard=hazard,
                        pcode=iso3,  # country-level; fusion fans out to admin2
                        lat=lat if isinstance(lat, (int, float)) else None,
                        lon=lon if isinstance(lon, (int, float)) else None,
                        value=float(props.get("alertscore") or 0.0),
                        unit="alertscore",
                        normalised=intensity,
                        valid_from=_parse(props.get("fromdate")),
                        valid_to=_parse(props.get("todate")),
                        retrieved_at=now,
                        raw={
                            "eventid": props.get("eventid"),
                            "eventname": props.get("eventname"),
                            "glide": props.get("glide"),
                            "alertlevel": props.get("alertlevel"),
                        },
                        note=f"GDACS {alert.upper()} alert — "
                        f"{props.get('eventname') or props.get('name') or code} "
                        f"(event {props.get('eventid')})",
                    )
                )
                count += 1
        result.health[-1].records = count

    return result


async def fetch_event_geometry(
    fetcher: Fetcher, event_type: str, event_id: int, episode_id: int | None = None
) -> dict | None:
    """Fetch the affected-area polygon for a specific event, used to decide
    which admin-2 districts actually fall inside an alert."""
    params: dict[str, object] = {"eventtype": event_type, "eventid": event_id}
    if episode_id is not None:
        params["episodeid"] = episode_id
    payload, _ = await fetcher.get_json(
        GDACS_GEOMETRY, params=params, cache_s=24 * 3600
    )
    return payload
