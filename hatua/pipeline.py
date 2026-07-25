"""End-to-end pipeline: live data in, verified advisories out.

    ingest -> fuse -> trigger -> analyse -> plan -> localise -> verify

Nothing leaves this module unverified. ``run_district`` returns every advisory
it produced, passed and blocked alike, because the blocked ones are the
interesting ones — they are the evidence that the guardrail does something.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .agents.core import (
    ActionPlanner,
    ImpactAnalyst,
    Localizer,
    default_languages,
    render_evidence,
)
from .agents.llm import LLM
from .agents.verifier import Verifier
from .delivery.encoding import (
    USSD_SCREEN_CHARS,
    max_characters,
    measure,
    normalise_for_gsm7,
)
from .fusion.engine import assess
from .ingest import gdacs, openmeteo
from .ingest.base import Fetcher, IngestResult
from .models import (
    AdminUnit,
    Advisory,
    CAPAlert,
    Channel,
    Exposure,
    Language,
    RiskAssessment,
    SignalReading,
    TriggerEvent,
    Vulnerability,
)

log = logging.getLogger("hatua.pipeline")


@dataclass
class DistrictResult:
    unit: AdminUnit
    assessment: RiskAssessment
    advisories: list[Advisory] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def dispatchable(self) -> list[Advisory]:
        return [a for a in self.advisories if a.dispatchable]

    @property
    def blocked(self) -> list[Advisory]:
        return [a for a in self.advisories if not a.dispatchable]


def channel_char_limit(channel: Channel, language: Language) -> int:
    """How many characters this channel and language actually allow."""
    if channel is Channel.USSD:
        return USSD_SCREEN_CHARS
    if channel is Channel.SMS:
        return max_characters(language.value, segments=2)
    if channel is Channel.VOICE:
        return 600  # roughly 45 seconds of speech
    return 900      # Telegram, WhatsApp, dashboard


async def gather_signals(
    fetcher: Fetcher, units: dict[str, AdminUnit]
) -> tuple[dict[str, list[SignalReading]], IngestResult]:
    """Pull every source in parallel and index readings by district."""
    points = {
        pcode: (u.centroid_lat, u.centroid_lon)
        for pcode, u in units.items()
        if u.centroid_lat is not None and u.centroid_lon is not None
    }

    forecast, flood, events = await asyncio.gather(
        openmeteo.fetch_forecast(fetcher, points, days=16),
        openmeteo.fetch_flood(fetcher, points, days=90),
        gdacs.fetch_events(fetcher, lookback_days=150),
    )

    combined = IngestResult()
    for part in (forecast, flood, events):
        combined.extend(part)

    by_district: dict[str, list[SignalReading]] = {p: [] for p in units}
    country_level: dict[str, list[SignalReading]] = {}

    for reading in combined.readings:
        if reading.pcode in by_district:
            by_district[reading.pcode].append(reading)
        elif reading.pcode:
            # GDACS reports at country level; fan out to that country's units.
            country_level.setdefault(reading.pcode, []).append(reading)

    for pcode, unit in units.items():
        for reading in country_level.get(unit.country_iso3, []):
            by_district[pcode].append(
                reading.model_copy(update={"pcode": pcode})
            )

    return by_district, combined


async def run_district(
    unit: AdminUnit,
    readings: list[SignalReading],
    *,
    vulnerability: Vulnerability | None = None,
    exposure: Exposure | None = None,
    cap_alerts: list[CAPAlert] | None = None,
    languages: list[Language] | None = None,
    channels: list[Channel] | None = None,
    llm: LLM | None = None,
    max_triggers: int = 1,
) -> DistrictResult:
    """Full pipeline for one district."""
    assessment = assess(
        unit,
        readings,
        vulnerability=vulnerability,
        exposure=exposure,
        cap_alerts=cap_alerts,
    )
    result = DistrictResult(unit=unit, assessment=assessment)

    if not assessment.triggers:
        return result

    llm = llm or LLM()
    analyst = ImpactAnalyst(llm)
    planner = ActionPlanner(llm)
    localizer = Localizer(llm)
    verifier = Verifier(llm)

    languages = languages or default_languages(unit.country_iso3)
    channels = channels or [Channel.SMS, Channel.TELEGRAM]

    for trigger in assessment.triggers[:max_triggers]:
        evidence = render_evidence(assessment, trigger)
        try:
            hypothesis = await analyst.analyse(assessment, trigger)
            plan = await planner.plan(assessment, trigger, hypothesis)
        except Exception as exc:  # noqa: BLE001 - must not take down the run
            msg = f"{trigger.threshold_name}: {type(exc).__name__}: {exc}"
            log.error("%s reasoning failed — %s", unit.pcode, msg)
            result.errors.append(msg)
            continue

        for language in languages:
            for channel in channels:
                try:
                    body = await localizer.render(
                        plan,
                        hypothesis,
                        assessment,
                        language=language,
                        channel=channel,
                        char_limit=channel_char_limit(channel, language),
                    )
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(
                        f"{trigger.threshold_name}/{language.value}/"
                        f"{channel.value}: {type(exc).__name__}: {exc}"
                    )
                    continue

                # Normalise and measure before verification, so the encoding
                # metadata is carried on the advisory the caller receives
                # rather than only inside the verifier's local copy.
                if channel.has_hard_length_limit and language.is_latin_script:
                    body = normalise_for_gsm7(body)
                sms = measure(body)

                seed = (
                    f"{unit.pcode}:{trigger.trigger_id}:"
                    f"{language.value}:{channel.value}"
                )
                advisory = Advisory(
                    advisory_id=hashlib.sha256(seed.encode()).hexdigest()[:16],
                    pcode=unit.pcode,
                    admin_name=unit.name,
                    country_iso3=unit.country_iso3,
                    hazard=plan.hazard,
                    severity=plan.severity,
                    language=language,
                    channel=channel,
                    body=body,
                    created_at=datetime.now(timezone.utc),
                    valid_until=datetime.now(timezone.utc)
                    + timedelta(days=trigger.lead_time_days),
                    trigger_ids=[trigger.trigger_id],
                    cited_signals=[s.citation for s in trigger.signals[:12]],
                    action_ids=[a.action_id for a in plan.actions],
                    confidence_score=trigger.confidence_score,
                    encoding=sms.encoding,
                    segment_count=sms.segments,
                    septet_count=sms.units,
                )

                verification = await verifier.verify(
                    advisory,
                    trigger=trigger,
                    assessment=assessment,
                    hypothesis=hypothesis,
                    plan=plan,
                    evidence=evidence,
                )
                result.advisories.append(
                    advisory.model_copy(update={"verification": verification})
                )

    return result


async def run(
    units: dict[str, AdminUnit],
    *,
    context: dict[str, Vulnerability] | None = None,
    exposures: dict[str, Exposure] | None = None,
    languages: list[Language] | None = None,
    channels: list[Channel] | None = None,
    top_n: int | None = None,
    concurrency: int = 3,
) -> list[DistrictResult]:
    """Run the full pipeline across a set of districts.

    ``top_n`` restricts LLM work to the highest-risk districts, which is what
    the scheduler uses in production: scoring all ~900 admin-2 units is cheap
    and deterministic, but reasoning over all of them is neither.
    """
    context = context or {}
    exposures = exposures or {}

    async with Fetcher() as fetcher:
        signals, _health = await gather_signals(fetcher, units)

    # Score everything deterministically first, then reason only where it matters.
    ranked = sorted(
        units.items(),
        key=lambda kv: assess(
            kv[1],
            signals.get(kv[0], []),
            vulnerability=context.get(kv[0]),
            exposure=exposures.get(kv[0]),
        ).compound_risk_score,
        reverse=True,
    )
    if top_n is not None:
        ranked = ranked[:top_n]

    llm = LLM()
    semaphore = asyncio.Semaphore(concurrency)

    async def one(pcode: str, unit: AdminUnit) -> DistrictResult:
        async with semaphore:
            return await run_district(
                unit,
                signals.get(pcode, []),
                vulnerability=context.get(pcode),
                exposure=exposures.get(pcode),
                languages=languages,
                channels=channels,
                llm=llm,
            )

    results = await asyncio.gather(
        *(one(pcode, unit) for pcode, unit in ranked), return_exceptions=True
    )

    out: list[DistrictResult] = []
    for item in results:
        if isinstance(item, BaseException):
            log.error("district failed: %s", item)
            continue
        out.append(item)
    return out
