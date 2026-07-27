"""National meteorological agency CAP feeds.

Six of the eight IGAD member states publish Common Alerting Protocol feeds on
an **identical URL path** (`/api/cap/rss.xml`), so one parser handles Kenya,
Ethiopia, Somalia, Sudan, South Sudan and Djibouti. Uganda publishes on an S3
path and its feed is stale; Eritrea has no WMO-registered alerting authority.

Why these outrank everything else we ingest
-------------------------------------------
A CAP alert is an **official government warning**, issued by the national
meteorological service under its WMO Register of Alerting Authorities OID. It
carries institutional weight nothing we compute can match, and it is the legal
instrument in that country.

So a live CAP alert does not contribute to our score — it **short-circuits**
it. We are not in the business of second-guessing a national met agency, and a
system that quietly disagreed with one would be worse than useless: it would
put a household in the position of choosing between two warnings.

Consuming CAP is also what makes emitting CAP honest (see `api/cap.py`). We
speak the same language in both directions rather than asking for
interoperability we do not offer.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from xml.etree import ElementTree

from ..config import CAP_FEEDS, WMO_ALERTING_OIDS
from ..models import CAPAlert
from .base import Fetcher, IngestResult, SourceHealth

log = logging.getLogger("hatua.cap")

CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}

# Feeds we know are stale. Kept so coverage is visible rather than silently
# absent, but flagged so nothing downstream treats them as current.
KNOWN_STALE: frozenset[str] = frozenset({"UGA"})

# Beyond this an "active" alert is almost certainly a feed nobody is updating.
MAX_ALERT_AGE_DAYS = 30


def _text(node: ElementTree.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path, CAP_NS)
    return found.text.strip() if found is not None and found.text else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_cap_document(xml: str, country_iso3: str) -> list[CAPAlert]:
    """Parse a CAP 1.2 document into alerts.

    Handles both a bare <alert> and an RSS feed whose items link to or embed
    alerts, because the six national feeds are not consistent about which they
    serve.
    """
    alerts: list[CAPAlert] = []
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        log.warning("%s: CAP feed is not valid XML: %s", country_iso3, exc)
        return alerts

    # A feed may be <alert>, or RSS containing <alert> elements.
    nodes = (
        [root]
        if root.tag.endswith("alert")
        else root.findall(".//cap:alert", CAP_NS)
    )

    for node in nodes:
        identifier = _text(node, "cap:identifier") or ""
        sender = _text(node, "cap:sender") or ""
        sent = _parse_time(_text(node, "cap:sent"))
        info = node.find("cap:info", CAP_NS)
        if info is None:
            continue

        areas = [
            area.text.strip()
            for area in info.findall("cap:area/cap:areaDesc", CAP_NS)
            if area.text
        ]

        alerts.append(
            CAPAlert(
                identifier=identifier,
                sender=sender,
                sender_oid=WMO_ALERTING_OIDS.get(country_iso3),
                country_iso3=country_iso3,
                sent=sent or datetime.now(timezone.utc),
                event=_text(info, "cap:event") or "Unknown",
                severity_raw=_text(info, "cap:severity"),
                urgency=_text(info, "cap:urgency"),
                certainty=_text(info, "cap:certainty"),
                headline=_text(info, "cap:headline"),
                description=_text(info, "cap:description"),
                areas=areas,
                effective=_parse_time(_text(info, "cap:effective")),
                expires=_parse_time(_text(info, "cap:expires")),
                link=_text(info, "cap:web"),
            )
        )
    return alerts


def _rss_item_links(xml: str) -> list[str]:
    """Extract item links from an RSS feed, for feeds that link out to CAP."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []
    links = []
    for item in root.findall(".//item"):
        link = item.find("link")
        if link is not None and link.text and link.text.strip().endswith(
            (".xml", ".cap")
        ):
            links.append(link.text.strip())
    return links


async def fetch_alerts(
    fetcher: Fetcher,
    countries: list[str] | None = None,
    *,
    max_per_country: int = 5,
) -> tuple[list[CAPAlert], IngestResult]:
    """Fetch current official alerts for the IGAD member states.

    Never raises. A national met agency's feed being down must not take the
    pipeline with it — that is precisely when the rest of the system matters
    most.
    """
    result = IngestResult()
    today = datetime.now(timezone.utc)
    targets = [
        (iso3, url)
        for iso3, url in CAP_FEEDS.items()
        if not countries or iso3 in countries
    ]

    async def one(iso3: str, url: str) -> tuple[list[CAPAlert], SourceHealth]:
        xml, health = await fetcher.get_text(url, cache_s=1800)
        health.source = f"national_cap_{iso3}"
        if not xml:
            return [], health

        found = parse_cap_document(xml, iso3)

        # Some feeds are RSS wrappers linking out to CAP documents. Chase a
        # couple in parallel — never serially, and never many. Six national
        # feeds fetched one link at a time is how a 3-second job became a
        # 45-second timeout.
        if not found:
            links = _rss_item_links(xml)[:2]
            if links:
                docs = await asyncio.gather(
                    *(fetcher.get_text(l, cache_s=3600) for l in links),
                    return_exceptions=True,
                )
                for doc in docs:
                    if isinstance(doc, tuple) and doc[0]:
                        found.extend(parse_cap_document(doc[0], iso3))

        fresh = [
            a
            for a in found
            if not (a.expires and a.expires < today)
            and (today - a.sent.replace(tzinfo=timezone.utc)).days
            <= MAX_ALERT_AGE_DAYS
        ]
        if iso3 in KNOWN_STALE and fresh:
            log.info(
                "%s CAP feed is registered but historically stale — "
                "treating %d alert(s) as informational only",
                iso3,
                len(fresh),
            )
        health.records = len(fresh)
        log.info(
            "%s: %d CAP alert(s) parsed, %d current",
            iso3,
            len(found),
            len(fresh),
        )
        return fresh[:max_per_country], health

    outcomes = await asyncio.gather(
        *(one(iso3, url) for iso3, url in targets), return_exceptions=True
    )

    alerts: list[CAPAlert] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            # A national met agency's feed failing must never take the pipeline
            # with it. That is precisely when the rest of the system matters.
            log.warning("CAP feed failed: %s", outcome)
            continue
        found, health = outcome
        alerts.extend(found)
        result.health.append(health)

    return alerts, result


def alerts_for_country(
    alerts: list[CAPAlert], country_iso3: str
) -> list[CAPAlert]:
    return [a for a in alerts if a.country_iso3 == country_iso3]


def coverage() -> dict[str, str]:
    """Which countries have an official alerting feed, and which do not.

    Surfaced rather than hidden. Eritrea having no registered alerting
    authority is a fact about the region's early warning capacity, not a gap
    in our implementation to be papered over.
    """
    out = {}
    for iso3 in ("KEN", "ETH", "SOM", "SDN", "SSD", "DJI", "UGA", "ERI"):
        if iso3 not in CAP_FEEDS:
            out[iso3] = "no WMO-registered alerting authority"
        elif iso3 in KNOWN_STALE:
            out[iso3] = f"registered but stale — {CAP_FEEDS[iso3]}"
        else:
            out[iso3] = CAP_FEEDS[iso3]
    return out
