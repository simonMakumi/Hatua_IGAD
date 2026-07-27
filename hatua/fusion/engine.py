"""
Compound risk fusion and trigger evaluation.

This layer is deliberately **pure Python with no model in it**. That is a
design decision, not a shortcut.

The numeric basis of a life-safety warning has to be reproducible and
inspectable. If a district is told to move livestock, an ICPAC analyst must be
able to open this file, read the arithmetic, and disagree with a specific
weight. You cannot do that with an embedding. So the LLM layer above never
computes risk — it only *explains* risk that was computed here, and the
Verifier checks its explanation back against these numbers.

The core claim
--------------
Existing regional systems are single-hazard. Drought is monitored separately
from conflict, which is monitored separately from displacement and food
insecurity. But in the Horn of Africa these are one crisis. A drought in a
district already at IPC Phase 4 with active conflict and 30,000 IDPs is not the
same event as the same drought in a stable, food-secure district, and should
not generate the same warning.

    CRS = hazard_composite
          x exposure_multiplier      (who and what is there,     max 1.5)
          x vulnerability_multiplier (can they absorb it,        max 2.6)
          / (1.5 * 2.6)              (normalise to the theoretical maximum)

Confidence is deliberately NOT a term here. It gates whether a trigger fires
and how severe an advisory may be, but it does not lower the risk score: a
hazard is no less dangerous for our being unsure about it. Folding uncertainty
into the ranking would quietly deprioritise a real threat we happen to have
thin evidence for.

Every term is bounded, every weight is named, and every output carries the
SignalReadings that produced it.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import date, timedelta

from ..models import (
    AdminUnit,
    CAPAlert,
    Confidence,
    Exposure,
    HazardType,
    RiskAssessment,
    SignalReading,
    TriggerEvent,
    Vulnerability,
)
from ..ingest.base import clamp01, utcnow

log = logging.getLogger("hatua.fusion")

# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

# Relative contribution of each hazard to the composite score. Rapid-onset
# hazards outrank slow-onset ones because the value of a warning is a function
# of how little time the recipient otherwise has.
HAZARD_WEIGHTS: dict[HazardType, float] = {
    HazardType.FLOOD: 1.00,
    HazardType.HEAVY_RAIN: 0.75,
    HazardType.DROUGHT: 0.90,
    HazardType.HEAT_STRESS: 0.55,
    HazardType.FOOD_INSECURITY: 0.85,
    HazardType.CONFLICT: 0.80,
    HazardType.DISPLACEMENT: 0.70,
    HazardType.DISEASE_OUTBREAK: 0.75,
    HazardType.LOCUST: 0.60,
    HazardType.WILDFIRE: 0.45,
    HazardType.EARTHQUAKE: 0.85,
}

# How much we trust each source when several disagree about the same hazard.
# ICPAC outranks global models inside its own region because it is the WMO
# Regional Climate Centre for the Greater Horn and downscales to it.
SOURCE_TRUST: dict[str, float] = {
    "national_cap": 1.00,          # official government alert
    "icpac_hazards_watch": 0.95,
    "icpac_drought_watch": 0.95,
    "icpac_triggers": 0.95,
    "gdacs": 0.90,                 # already severity-modelled, multi-agency
    "climateserv_chirps": 0.85,
    "open_meteo_flood": 0.80,      # GloFAS v4
    "open_meteo_forecast": 0.75,   # global model, ~11km
    "hdx_hapi": 0.90,              # IPC / ACLED / DTM derived
    "fews_net": 0.90,
    "who_don": 0.85,
    "usgs": 0.95,
    "firms": 0.80,
}
DEFAULT_TRUST = 0.60

# IPC phase -> vulnerability multiplier. Phase 3 (Crisis) is the conventional
# threshold for humanitarian action, so the curve steepens there.
IPC_MULTIPLIER: dict[int, float] = {
    1: 1.00,   # Minimal
    2: 1.15,   # Stressed
    3: 1.45,   # Crisis
    4: 1.75,   # Emergency
    5: 2.00,   # Catastrophe / Famine
}

MAX_VULNERABILITY_MULTIPLIER = 2.6
MAX_EXPOSURE_MULTIPLIER = 1.5

# Expected source families. Data sufficiency is measured against this, so a
# district scored from one source alone is visibly under-evidenced.
EXPECTED_SOURCE_FAMILIES = {
    "forecast",     # open_meteo_forecast
    "flood",        # open_meteo_flood / GloFAS
    "regional",     # ICPAC
    "event",        # GDACS / CAP
    "humanitarian", # HAPI / FEWS NET
}

_SOURCE_FAMILY: dict[str, str] = {
    "open_meteo_forecast": "forecast",
    "open_meteo_flood": "flood",
    "icpac_hazards_watch": "regional",
    "icpac_drought_watch": "regional",
    "icpac_triggers": "regional",
    "climateserv_chirps": "regional",
    "gdacs": "event",
    "national_cap": "event",
    "hdx_hapi": "humanitarian",
    "fews_net": "humanitarian",
    "who_don": "humanitarian",
}


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


class Threshold:
    """A named threshold with a lead time.

    Semantics deliberately mirror ICPAC's own thresholds-and-triggers framework
    (eatriggersthresholds.icpac.net) so our triggers are comparable to theirs
    rather than a parallel invention.
    """

    def __init__(
        self,
        name: str,
        hazard: HazardType,
        value: float,
        lead_time_days: int,
        *,
        min_confidence: float = 0.0,
        description: str = "",
    ) -> None:
        self.name = name
        self.hazard = hazard
        self.value = value
        self.lead_time_days = lead_time_days
        self.min_confidence = min_confidence
        self.description = description


THRESHOLDS: list[Threshold] = [
    Threshold(
        "flood_imminent", HazardType.FLOOD, 0.70, 3,
        min_confidence=0.55,
        description="River discharge or extreme rainfall indicating flooding "
                    "within 72 hours",
    ),
    Threshold(
        "flood_watch", HazardType.FLOOD, 0.45, 10,
        min_confidence=0.40,
        description="Elevated flood signal within the 10-day forecast window",
    ),
    Threshold(
        "heavy_rain_warning", HazardType.HEAVY_RAIN, 0.60, 7,
        min_confidence=0.45,
        description="Rainfall accumulation likely to disrupt movement, "
                    "shelter or crops",
    ),
    Threshold(
        "drought_emerging", HazardType.DROUGHT, 0.50, 60,
        min_confidence=0.40,
        description="Rainfall deficit or forage decline consistent with an "
                    "emerging drought",
    ),
    Threshold(
        "drought_severe", HazardType.DROUGHT, 0.75, 90,
        min_confidence=0.55,
        description="Sustained deficit with failed or failing season",
    ),
    Threshold(
        "heat_stress", HazardType.HEAT_STRESS, 0.65, 5,
        min_confidence=0.50,
        description="Heat levels dangerous to people and livestock",
    ),
    Threshold(
        "food_insecurity_crisis", HazardType.FOOD_INSECURITY, 0.60, 90,
        min_confidence=0.50,
        description="IPC Phase 3+ with deteriorating hazard outlook",
    ),
    Threshold(
        "compound_crisis", HazardType.FOOD_INSECURITY, 0.70, 30,
        min_confidence=0.50,
        description="Climate hazard converging with conflict, displacement "
                    "or acute food insecurity",
    ),
]


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def _trust(source: str) -> float:
    return SOURCE_TRUST.get(source, DEFAULT_TRUST)


def combine_hazard(readings: list[SignalReading]) -> tuple[float, float]:
    """Combine several readings of one hazard into (intensity, agreement).

    Intensity is a trust-weighted mean rather than a max: one twitchy global
    model should not by itself drive a district to emergency. But we take the
    higher of that mean and 85% of the single most trusted reading, so a
    high-confidence official alert is never diluted by quieter sources.

    Agreement is 1 - normalised spread, and feeds the confidence score. Sources
    disagreeing is itself information, and should lower the severity we are
    permitted to assert.
    """
    if not readings:
        return 0.0, 0.0

    weights = [_trust(r.source) for r in readings]
    values = [r.normalised for r in readings]
    total_w = sum(weights) or 1.0

    weighted_mean = sum(v * w for v, w in zip(values, weights)) / total_w

    best = max(readings, key=lambda r: _trust(r.source) * r.normalised)
    floor = 0.85 * best.normalised * _trust(best.source)

    intensity = clamp01(max(weighted_mean, floor))

    if len(values) == 1:
        # A single source cannot corroborate itself. Treat as moderate
        # agreement so a lone reading cannot reach maximum confidence.
        agreement = 0.55
    else:
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        agreement = clamp01(1.0 - (variance**0.5) * 2.0)

    return intensity, agreement


def exposure_multiplier(exposure: Exposure) -> float:
    """More people and more productive land means a given hazard matters more.

    Deliberately gentle (1.0-1.5). Sparsely populated pastoral districts are
    exactly where early warning has historically failed, and an aggressive
    population weighting would systematically deprioritise them. Turkana must
    not lose to Nairobi because fewer people live there.
    """
    m = 1.0
    if exposure.population:
        if exposure.population > 1_000_000:
            m += 0.30
        elif exposure.population > 250_000:
            m += 0.20
        elif exposure.population > 50_000:
            m += 0.10
    if exposure.cropland_fraction:
        m += 0.15 * clamp01(exposure.cropland_fraction)
    if exposure.rangeland_fraction:
        m += 0.15 * clamp01(exposure.rangeland_fraction)
    return min(m, MAX_EXPOSURE_MULTIPLIER)


def vulnerability_multiplier(v: Vulnerability) -> float:
    """The compound-risk term. This is where HATUA differs from a weather app."""
    m = 1.0

    if v.ipc_phase:
        m *= IPC_MULTIPLIER.get(v.ipc_phase, 1.0)

    # Conflict does not just add risk, it removes coping capacity: it blocks
    # the roads people would use to move livestock and the markets where they
    # would sell them.
    if v.conflict_events_90d:
        if v.conflict_events_90d > 100:
            m *= 1.30
        elif v.conflict_events_90d > 25:
            m *= 1.18
        elif v.conflict_events_90d > 5:
            m *= 1.08
    if v.fatalities_90d and v.fatalities_90d > 50:
        m *= 1.10

    # Displaced people are in temporary shelter on marginal land, often
    # precisely the floodplain.
    if v.idps:
        if v.idps > 100_000:
            m *= 1.25
        elif v.idps > 20_000:
            m *= 1.15
        elif v.idps > 5_000:
            m *= 1.07

    return min(m, MAX_VULNERABILITY_MULTIPLIER)


def confidence_score(
    agreements: dict[HazardType, float],
    dominant: HazardType | None,
    sufficiency: float,
    freshness: float,
) -> float:
    """How much we actually know, on 0-1.

    Gates the severity ladder in models.MAX_SEVERITY_BY_CONFIDENCE, so this
    number decides whether we are permitted to say "evacuate" at all.
    """
    agreement = agreements.get(dominant, 0.5) if dominant else 0.5
    # Weighted toward sufficiency: agreement between two sources means little
    # if three others returned nothing.
    return clamp01(0.45 * agreement + 0.35 * sufficiency + 0.20 * freshness)


def assess(
    unit: AdminUnit,
    readings: list[SignalReading],
    *,
    vulnerability: Vulnerability | None = None,
    exposure: Exposure | None = None,
    cap_alerts: list[CAPAlert] | None = None,
    freshness: float = 0.8,
) -> RiskAssessment:
    """Fuse all signals for one admin-2 unit into a single assessment."""
    by_hazard: dict[HazardType, list[SignalReading]] = defaultdict(list)
    for r in readings:
        # Currency metadata (normalised == 0 with a freshness note) is not a
        # hazard observation and must not dilute the mean.
        if r.normalised > 0 or r.dataset.endswith(("_anomaly", "_cdi")):
            by_hazard[r.hazard].append(r)

    intensities: dict[HazardType, float] = {}
    agreements: dict[HazardType, float] = {}
    for hazard, group in by_hazard.items():
        intensity, agreement = combine_hazard(group)
        if intensity > 0:
            intensities[hazard] = intensity
            agreements[hazard] = agreement

    exposure = exposure or Exposure(
        population=unit.population, population_year=unit.population_year
    )
    vulnerability = vulnerability or Vulnerability()

    # An official government CAP alert overrides our own modelling. We are not
    # in the business of second-guessing a national met agency.
    for alert in cap_alerts or []:
        sev = (alert.severity_raw or "").lower()
        if sev in ("extreme", "severe"):
            hazard = _hazard_from_cap(alert.event)
            intensities[hazard] = max(intensities.get(hazard, 0.0), 0.85)
            agreements[hazard] = max(agreements.get(hazard, 0.0), 0.90)

    families = {
        _SOURCE_FAMILY.get(r.source, "other")
        for r in readings
        if _SOURCE_FAMILY.get(r.source)
    }
    sufficiency = len(families & EXPECTED_SOURCE_FAMILIES) / len(
        EXPECTED_SOURCE_FAMILIES
    )

    # Composite: weighted hazards, with secondary hazards contributing at a
    # discount so a genuinely compound crisis outranks a single severe hazard
    # without any single hazard being able to saturate the score alone.
    weighted = sorted(
        ((i * HAZARD_WEIGHTS.get(h, 0.5), h) for h, i in intensities.items()),
        reverse=True,
    )
    composite = 0.0
    for rank, (score, _hazard) in enumerate(weighted):
        composite += score * (1.0 if rank == 0 else 0.35 / rank)
    composite = clamp01(composite)

    vuln_m = vulnerability_multiplier(vulnerability)
    exp_m = exposure_multiplier(exposure)
    vulnerability.multiplier = round(vuln_m, 3)

    dominant = max(intensities, key=intensities.get) if intensities else None
    conf = confidence_score(agreements, dominant, sufficiency, freshness)

    # Normalise against the theoretical maximum of the multipliers rather than
    # an arbitrary constant. An earlier version divided by a smaller figure and
    # three of five test districts pinned at exactly 1.000 — which is worse
    # than useless, because the entire operational purpose of this score is to
    # *rank* districts so a county officer knows where to go first. A score
    # that saturates cannot rank. Values now land in a 0.2-0.8 band in practice,
    # which is honest: 1.0 should mean "maximum hazard, maximum exposure,
    # maximum vulnerability simultaneously" and should essentially never occur.
    crs = clamp01(
        composite * exp_m * vuln_m
        / (MAX_EXPOSURE_MULTIPLIER * MAX_VULNERABILITY_MULTIPLIER)
    )

    assessment = RiskAssessment(
        pcode=unit.pcode,
        admin_name=unit.name,
        country_iso3=unit.country_iso3,
        assessed_at=utcnow(),
        compound_risk_score=round(crs, 4),
        hazard_scores={h: round(v, 4) for h, v in intensities.items()},
        exposure=exposure,
        vulnerability=vulnerability,
        confidence_score=round(conf, 4),
        signals=readings,
        data_sufficiency=round(sufficiency, 3),
    )
    assessment.triggers = evaluate_triggers(assessment, cap_alerts or [])
    return assessment


def _hazard_from_cap(event: str) -> HazardType:
    e = (event or "").lower()
    if "flood" in e:
        return HazardType.FLOOD
    if "drought" in e:
        return HazardType.DROUGHT
    if "rain" in e or "storm" in e:
        return HazardType.HEAVY_RAIN
    if "heat" in e:
        return HazardType.HEAT_STRESS
    if "locust" in e:
        return HazardType.LOCUST
    return HazardType.HEAVY_RAIN


def evaluate_triggers(
    assessment: RiskAssessment, cap_alerts: list[CAPAlert]
) -> list[TriggerEvent]:
    """Fire thresholds, each with full provenance.

    A trigger requires **both** that the hazard crosses its threshold **and**
    that confidence clears the threshold's own gate. A severe-looking signal we
    do not trust produces no trigger, which is the correct behaviour: silence
    is better than a warning we cannot stand behind.
    """
    fired: list[TriggerEvent] = []
    today = date.today()

    for threshold in THRESHOLDS:
        observed = assessment.hazard_scores.get(threshold.hazard, 0.0)

        # The compound trigger is special: it fires on convergence, not on any
        # single hazard. This is the case existing single-hazard systems miss.
        if threshold.name == "compound_crisis":
            climate = max(
                (
                    assessment.hazard_scores.get(h, 0.0)
                    for h in (
                        HazardType.DROUGHT,
                        HazardType.FLOOD,
                        HazardType.HEAVY_RAIN,
                    )
                ),
                default=0.0,
            )
            v = assessment.vulnerability
            stressed = bool(
                (v.ipc_phase and v.ipc_phase >= 3)
                or (v.conflict_events_90d and v.conflict_events_90d > 25)
                or (v.idps and v.idps > 20_000)
            )
            if not (climate >= 0.45 and stressed):
                continue
            observed = clamp01(climate * v.multiplier / 1.8)

        if observed < threshold.value:
            continue
        if assessment.confidence_score < threshold.min_confidence:
            log.info(
                "%s: %s reached %.2f but confidence %.2f < %.2f — not fired",
                assessment.pcode,
                threshold.name,
                observed,
                assessment.confidence_score,
                threshold.min_confidence,
            )
            continue

        supporting = [
            s
            for s in assessment.signals
            if s.hazard == threshold.hazard and s.normalised > 0
        ] or [s for s in assessment.signals if s.normalised > 0]

        seed = f"{assessment.pcode}:{threshold.name}:{today.isoformat()}"
        fired.append(
            TriggerEvent(
                trigger_id=hashlib.sha256(seed.encode()).hexdigest()[:16],
                pcode=assessment.pcode,
                admin_name=assessment.admin_name,
                country_iso3=assessment.country_iso3,
                hazard=threshold.hazard,
                threshold_name=threshold.name,
                threshold_value=threshold.value,
                observed_value=round(observed, 4),
                lead_time_days=threshold.lead_time_days,
                fired_at=utcnow(),
                window_start=today,
                window_end=today + timedelta(days=threshold.lead_time_days),
                confidence_score=assessment.confidence_score,
                signals=supporting,
                cap_alerts=cap_alerts,
            )
        )

    # Most severe first, so the reasoning core addresses the worst problem.
    fired.sort(key=lambda t: t.observed_value, reverse=True)
    return fired


def explain(assessment: RiskAssessment) -> str:
    """Human-readable derivation of the score.

    Rendered in the dashboard and passed to the Verifier. If an ICPAC analyst
    disputes a warning, this is the artefact they argue with.
    """
    lines = [
        f"{assessment.admin_name} ({assessment.pcode}, "
        f"{assessment.country_iso3})",
        f"  compound risk   {assessment.compound_risk_score:.3f}",
        f"  confidence      {assessment.confidence_score:.3f} "
        f"({Confidence.from_score(assessment.confidence_score).value}) "
        f"-> max severity {assessment.triggers[0].max_permitted_severity.value if assessment.triggers else 'n/a'}",
        f"  data sufficiency {assessment.data_sufficiency:.2f} "
        f"({int(assessment.data_sufficiency * 5)}/5 source families)",
        "  hazards:",
    ]
    for hazard, score in sorted(
        assessment.hazard_scores.items(), key=lambda kv: kv[1], reverse=True
    ):
        lines.append(
            f"    {hazard.value:18} {score:.3f} "
            f"(weight {HAZARD_WEIGHTS.get(hazard, 0.5):.2f})"
        )
    v = assessment.vulnerability
    lines.append(
        f"  vulnerability x{v.multiplier:.2f} "
        f"(IPC {v.ipc_phase or '-'}, conflict {v.conflict_events_90d or 0}, "
        f"IDPs {v.idps or 0})"
    )
    lines.append(f"  exposure      x{exposure_multiplier(assessment.exposure):.2f}")
    if assessment.triggers:
        lines.append("  triggers fired:")
        for t in assessment.triggers:
            lines.append(
                f"    {t.threshold_name:24} {t.observed_value:.3f} "
                f">= {t.threshold_value:.2f}  lead {t.lead_time_days}d"
            )
    else:
        lines.append("  triggers fired: none")
    return "\n".join(lines)
