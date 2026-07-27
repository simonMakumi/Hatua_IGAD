"""CAP 1.2 output.

We consume the national met agencies' CAP feeds — Kenya, Ethiopia, Somalia,
Sudan, South Sudan and Djibouti all publish on the same URL path — so it would
be strange not to speak the same language back.

Emitting valid CAP costs almost nothing on static hosting and buys three
things:

* **Interoperability with HUSIKA**, ICPAC's own CAP-compliant dissemination
  platform. HATUA becomes an input to it rather than a competitor.
* **A clean ingestion path into cell broadcast.** The Communications Authority
  of Kenya is rolling out CAP-based cell broadcast; that is the correct
  technology for imminent mass alerting (no opt-in, no MSISDN list, no
  congestion) and CAP is how you feed it.
* **Auditability.** A CAP document carries its own identifier, sender, urgency,
  severity and certainty, which is a far better record of what was said and
  when than a row in our database.

Reference: OASIS Common Alerting Protocol Version 1.2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from xml.sax.saxutils import escape

from ..config import WMO_ALERTING_OIDS
from ..models import Advisory, HazardType, Severity

CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"

# CAP severity is a fixed vocabulary; ours maps onto it cleanly because both
# ladders are ordered by expected impact.
CAP_SEVERITY: dict[Severity, str] = {
    Severity.NONE: "Unknown",
    Severity.ADVISORY: "Minor",
    Severity.WATCH: "Moderate",
    Severity.WARNING: "Severe",
    Severity.EMERGENCY: "Extreme",
}

# CAP urgency describes when action should be taken.
CAP_URGENCY: dict[Severity, str] = {
    Severity.NONE: "Unknown",
    Severity.ADVISORY: "Future",
    Severity.WATCH: "Expected",
    Severity.WARNING: "Expected",
    Severity.EMERGENCY: "Immediate",
}

CAP_CATEGORY: dict[HazardType, str] = {
    HazardType.FLOOD: "Met",
    HazardType.HEAVY_RAIN: "Met",
    HazardType.DROUGHT: "Met",
    HazardType.HEAT_STRESS: "Met",
    HazardType.WILDFIRE: "Fire",
    HazardType.EARTHQUAKE: "Geo",
    HazardType.DISEASE_OUTBREAK: "Health",
    HazardType.FOOD_INSECURITY: "Health",
    HazardType.LOCUST: "Env",
    HazardType.CONFLICT: "Security",
    HazardType.DISPLACEMENT: "Rescue",
}

CAP_EVENT: dict[HazardType, str] = {
    HazardType.FLOOD: "Flood",
    HazardType.HEAVY_RAIN: "Heavy Rainfall",
    HazardType.DROUGHT: "Drought",
    HazardType.HEAT_STRESS: "Extreme Heat",
    HazardType.FOOD_INSECURITY: "Acute Food Insecurity",
    HazardType.DISEASE_OUTBREAK: "Disease Outbreak",
    HazardType.LOCUST: "Desert Locust",
    HazardType.WILDFIRE: "Wildfire",
    HazardType.EARTHQUAKE: "Earthquake",
    HazardType.CONFLICT: "Armed Conflict",
    HazardType.DISPLACEMENT: "Population Displacement",
}


def _ts(value: datetime) -> str:
    """CAP requires a timezone offset; 'Z' is not permitted."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":00"


def _certainty(confidence: float) -> str:
    """Map our confidence score onto CAP's certainty vocabulary.

    Note the ceiling: we never emit "Observed", because HATUA forecasts and
    fuses — it does not observe. Claiming observation for a modelled signal
    would be a misrepresentation to any downstream system that trusts CAP.
    """
    if confidence >= 0.65:
        return "Likely"
    if confidence >= 0.40:
        return "Possible"
    return "Unlikely"


def cap_alert(advisory: Advisory, *, base_url: str = "") -> str:
    """Render one advisory as a CAP 1.2 alert document."""
    oid = WMO_ALERTING_OIDS.get(advisory.country_iso3, "")
    identifier = f"HATUA.{advisory.country_iso3}.{advisory.advisory_id}"

    parameters = [
        ("HATUA-confidence", f"{advisory.confidence_score:.2f}"),
        ("HATUA-actions", ",".join(advisory.action_ids)),
        (
            "HATUA-verification",
            advisory.verification.status.value if advisory.verification else "none",
        ),
        ("HATUA-encoding", advisory.encoding or "n/a"),
    ]
    if oid:
        # Reference the national alerting authority we align to, without
        # claiming to be it.
        parameters.append(("WMO-alerting-authority-reference", oid))

    param_xml = "\n".join(
        f"      <parameter>\n"
        f"        <valueName>{escape(name)}</valueName>\n"
        f"        <value>{escape(value)}</value>\n"
        f"      </parameter>"
        for name, value in parameters
        if value
    )

    citations = "\n".join(
        f"      <parameter>\n"
        f"        <valueName>source-signal</valueName>\n"
        f"        <value>{escape(c)}</value>\n"
        f"      </parameter>"
        for c in advisory.cited_signals[:8]
    )

    expires = (
        f"    <expires>{_ts(advisory.valid_until)}</expires>\n"
        if advisory.valid_until
        else ""
    )

    return f"""  <alert xmlns="{CAP_NS}">
    <identifier>{escape(identifier)}</identifier>
    <sender>hatua@icpac-hackathon</sender>
    <sent>{_ts(advisory.created_at)}</sent>
    <status>Exercise</status>
    <msgType>Alert</msgType>
    <scope>Public</scope>
    <note>Generated by HATUA for the IGAD Hackathon 2026. Status is Exercise: \
this is a demonstration system and is not an official alerting authority. \
Official alerts for this area are issued by the national meteorological \
service.</note>
    <info>
      <language>{advisory.language.value}</language>
      <category>{CAP_CATEGORY.get(advisory.hazard, 'Other')}</category>
      <event>{escape(CAP_EVENT.get(advisory.hazard, advisory.hazard.value))}</event>
      <urgency>{CAP_URGENCY[advisory.severity]}</urgency>
      <severity>{CAP_SEVERITY[advisory.severity]}</severity>
      <certainty>{_certainty(advisory.confidence_score)}</certainty>
{expires}      <headline>{escape(CAP_EVENT.get(advisory.hazard, 'Alert'))} \
- {escape(advisory.admin_name)}</headline>
      <description>{escape(advisory.body)}</description>
      <web>{escape(base_url)}/api/districts/{escape(advisory.pcode)}/explain</web>
{param_xml}
{citations}
      <area>
        <areaDesc>{escape(advisory.admin_name)}, \
{escape(advisory.country_iso3)}</areaDesc>
        <geocode>
          <valueName>PCODE</valueName>
          <value>{escape(advisory.pcode)}</value>
        </geocode>
      </area>
    </info>
  </alert>"""


def cap_feed(advisories: list[Advisory], *, base_url: str = "") -> str:
    """A feed of every verified advisory.

    Only dispatchable advisories appear. A blocked advisory has, by definition,
    failed verification and must not enter an interoperability channel where a
    downstream system would treat it as authoritative.
    """
    now = datetime.now(timezone.utc)
    alerts = "\n".join(
        cap_alert(a, base_url=base_url) for a in advisories if a.dispatchable
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- HATUA CAP 1.2 feed. Generated {_ts(now)}.
     Status is Exercise throughout: HATUA is a demonstration system built for
     the IGAD Hackathon 2026 and is not a registered alerting authority.
     Official alerts are issued by national meteorological services under
     their WMO Register of Alerting Authorities OIDs. -->
<alerts>
{alerts}
</alerts>"""
