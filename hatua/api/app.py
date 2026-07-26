"""HATUA FastAPI service.

Serves three consumers with different needs:

* **The officials' dashboard** — risk map, fired triggers, the advisory queue
  and, critically, the *blocked* advisories. A verification layer nobody can
  inspect is just a claim.
* **The USSD gateway** — must answer in well under three seconds, so it reads
  only from the in-memory advisory cache and never triggers work.
* **Any downstream system** — a CAP 1.2 feed, so HATUA is interoperable with
  HUSIKA and with the Communications Authority of Kenya's incoming cell
  broadcast, rather than a parallel silo.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..agents.llm import available_providers
from ..config import get_settings
from ..delivery.ussd import USSDMenu, USSDRequest
from ..fusion.engine import explain
from ..models import (
    AdminUnit,
    Advisory,
    CommunityFeedback,
    FeedbackKind,
    Language,
    RiskAssessment,
)
from ..pipeline import DistrictResult, run
from .districts import DEMO_CONTEXT, DEMO_EXPOSURE, DEMO_UNITS
from .cap import cap_feed

log = logging.getLogger("hatua.api")


class State:
    """In-process store.

    Postgres/PostGIS is the right home for this in production, but for a
    six-day build an in-process store keeps the deployment to a single free-tier
    service and removes a whole class of failure from the demo. The interface
    is narrow enough to swap.
    """

    def __init__(self) -> None:
        self.results: list[DistrictResult] = []
        self.advisories: dict[tuple[str, str], Advisory] = {}
        self.feedback: list[CommunityFeedback] = []
        self.last_run: datetime | None = None
        self.running: bool = False
        self.last_error: str | None = None

    def index(self, results: list[DistrictResult]) -> None:
        self.results = results
        self.advisories = {}
        for result in results:
            for advisory in result.advisories:
                # USSD reads this dict. Only dispatchable advisories are
                # indexed, so an unverified message cannot reach a caller even
                # by accident.
                if advisory.dispatchable:
                    self.advisories[
                        (advisory.pcode, advisory.language.value)
                    ] = advisory
        self.last_run = datetime.now(timezone.utc)

    def lookup(self, pcode: str, language: Language) -> Advisory | None:
        return self.advisories.get((pcode, language.value)) or self.advisories.get(
            (pcode, Language.ENGLISH.value)
        )

    def record_feedback(
        self, pcode: str, phone: str, kind: FeedbackKind
    ) -> None:
        salt = get_settings().contact_salt
        self.feedback.append(
            CommunityFeedback(
                feedback_id=hashlib.sha256(
                    f"{phone}{datetime.now(timezone.utc)}".encode()
                ).hexdigest()[:16],
                pcode=pcode,
                channel="ussd",
                kind=kind,
                received_at=datetime.now(timezone.utc),
                # Never store a raw MSISDN. A phone number in an early warning
                # database is a person's location history in a region with
                # active conflict.
                contact_hash=hashlib.sha256(
                    f"{salt}{phone}".encode()
                ).hexdigest()[:24],
            )
        )

    @property
    def all_advisories(self) -> list[Advisory]:
        return [a for r in self.results for a in r.advisories]

    @property
    def blocked(self) -> list[Advisory]:
        return [a for a in self.all_advisories if not a.dispatchable]

    # -- persistence -------------------------------------------------------
    # Free-tier hosting sleeps and cold-starts. Re-running the whole pipeline
    # on every wake would take two minutes and burn model quota for results
    # that have not changed. So we snapshot to disk: the service comes up
    # instantly serving the last known state, then refreshes in the
    # background. This is also what makes the USSD path viable — it is
    # reading a snapshot, never computing.

    def save(self, path: Path) -> None:
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "assessments": [
                r.assessment.model_dump(mode="json") for r in self.results
            ],
            "units": [r.unit.model_dump(mode="json") for r in self.results],
            "advisories": [
                a.model_dump(mode="json") for a in self.all_advisories
            ],
            "feedback": [f.model_dump(mode="json") for f in self.feedback],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, default=str), encoding="utf-8")
        log.info("snapshot written to %s", path)

    def load(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            advisories = [Advisory.model_validate(a) for a in payload["advisories"]]
            by_pcode: dict[str, list[Advisory]] = {}
            for advisory in advisories:
                by_pcode.setdefault(advisory.pcode, []).append(advisory)

            self.results = [
                DistrictResult(
                    unit=AdminUnit.model_validate(unit),
                    assessment=RiskAssessment.model_validate(assessment),
                    advisories=by_pcode.get(assessment["pcode"], []),
                )
                for unit, assessment in zip(payload["units"], payload["assessments"])
            ]
            self.feedback = [
                CommunityFeedback.model_validate(f)
                for f in payload.get("feedback", [])
            ]
            self.advisories = {
                (a.pcode, a.language.value): a for a in advisories if a.dispatchable
            }
            self.last_run = (
                datetime.fromisoformat(payload["last_run"])
                if payload.get("last_run")
                else None
            )
            log.info(
                "snapshot loaded: %d districts, %d advisories",
                len(self.results),
                len(advisories),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load snapshot: %s", exc)
            return False


STATE = State()
SNAPSHOT = Path(__file__).resolve().parent.parent.parent / "data" / "snapshot.json"


async def refresh(top_n: int = 4) -> None:
    """Recompute assessments and advisories from live sources."""
    if STATE.running:
        log.info("refresh already in progress, skipping")
        return
    STATE.running = True
    STATE.last_error = None
    try:
        log.info("refresh starting")
        results = await run(
            DEMO_UNITS,
            context=DEMO_CONTEXT,
            exposures=DEMO_EXPOSURE,
            top_n=top_n,
            concurrency=1,
        )
        STATE.index(results)
        STATE.save(SNAPSHOT)
        log.info(
            "refresh complete: %d districts, %d advisories (%d blocked)",
            len(results),
            len(STATE.all_advisories),
            len(STATE.blocked),
        )
    except Exception as exc:  # noqa: BLE001
        STATE.last_error = f"{type(exc).__name__}: {exc}"
        log.exception("refresh failed")
    finally:
        STATE.running = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Serve the last known state immediately, then refresh in the background.
    # A warning system that is unavailable while it thinks is not much of a
    # warning system.
    STATE.load(SNAPSHOT)
    asyncio.create_task(refresh())
    yield


app = FastAPI(
    title="HATUA",
    description="From warning to action — the last mile of early warning "
                "for the Greater Horn of Africa.",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health and status
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "last_run": STATE.last_run.isoformat() if STATE.last_run else None,
        "running": STATE.running,
        "last_error": STATE.last_error,
        "districts_assessed": len(STATE.results),
        "advisories_total": len(STATE.all_advisories),
        # Count advisories, not cache entries. STATE.advisories is keyed by
        # (pcode, language) for the USSD lookup, so it collapses the SMS and
        # Telegram variants of the same message into one — reporting its length
        # as "dispatchable" made the numbers fail to add up on the dashboard.
        "advisories_dispatchable": sum(
            1 for a in STATE.all_advisories if a.dispatchable
        ),
        "advisories_blocked": len(STATE.blocked),
        "ussd_cache_entries": len(STATE.advisories),
        "feedback_received": len(STATE.feedback),
        "llm_providers_configured": {
            k: v for k, v in available_providers().items() if v
        },
        "sms_provider": settings.sms_provider,
        "dry_run": settings.dry_run,
    }


@app.post("/refresh")
async def trigger_refresh(top_n: int = 4) -> dict[str, str]:
    if STATE.running:
        return {"status": "already running"}
    asyncio.create_task(refresh(top_n))
    return {"status": "started"}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@app.get("/api/districts")
async def districts() -> list[dict[str, Any]]:
    """Ranked risk, for the map and the priority table."""
    out = []
    for result in STATE.results:
        a: RiskAssessment = result.assessment
        out.append(
            {
                "pcode": a.pcode,
                "name": a.admin_name,
                "country": a.country_iso3,
                "lat": result.unit.centroid_lat,
                "lon": result.unit.centroid_lon,
                "compound_risk": a.compound_risk_score,
                "confidence": a.confidence_score,
                "data_sufficiency": a.data_sufficiency,
                "dominant_hazard": a.dominant_hazard.value
                if a.dominant_hazard
                else None,
                "hazard_scores": {h.value: v for h, v in a.hazard_scores.items()},
                "vulnerability": {
                    "ipc_phase": a.vulnerability.ipc_phase,
                    "conflict_events_90d": a.vulnerability.conflict_events_90d,
                    "idps": a.vulnerability.idps,
                    "multiplier": a.vulnerability.multiplier,
                },
                "population": a.exposure.population,
                "triggers": [
                    {
                        "id": t.trigger_id,
                        "name": t.threshold_name,
                        "hazard": t.hazard.value,
                        "observed": t.observed_value,
                        "threshold": t.threshold_value,
                        "lead_time_days": t.lead_time_days,
                        "max_severity": t.max_permitted_severity.value,
                    }
                    for t in a.triggers
                ],
                "advisories": len(result.advisories),
                "blocked": len(result.blocked),
            }
        )
    return sorted(out, key=lambda d: -d["compound_risk"])


@app.get("/api/districts/{pcode}/explain", response_class=PlainTextResponse)
async def explain_district(pcode: str) -> str:
    """The full derivation of a district's score.

    Exposed deliberately. If an ICPAC analyst disputes a warning, this is the
    artefact they argue with — every weight, every multiplier, every trigger.
    """
    for result in STATE.results:
        if result.assessment.pcode == pcode:
            return explain(result.assessment)
    raise HTTPException(404, f"no assessment for {pcode}")


@app.get("/api/advisories")
async def advisories(include_blocked: bool = True) -> list[dict[str, Any]]:
    out = []
    for advisory in STATE.all_advisories:
        if not include_blocked and not advisory.dispatchable:
            continue
        v = advisory.verification
        out.append(
            {
                "id": advisory.advisory_id,
                "pcode": advisory.pcode,
                "district": advisory.admin_name,
                "country": advisory.country_iso3,
                "hazard": advisory.hazard.value,
                "severity": advisory.severity.value,
                "language": advisory.language.value,
                "channel": advisory.channel.value,
                "body": advisory.body,
                "characters": len(advisory.body),
                "encoding": advisory.encoding,
                "segments": advisory.segment_count,
                "confidence": advisory.confidence_score,
                "dispatchable": advisory.dispatchable,
                "action_ids": advisory.action_ids,
                "cited_signals": advisory.cited_signals,
                "verification": {
                    "status": v.status.value if v else "pending",
                    "checks": [
                        {"name": c.name, "passed": c.passed, "detail": c.detail}
                        for c in (v.checks if v else [])
                    ],
                    "blocked_reasons": v.blocked_reasons if v else [],
                },
            }
        )
    return out


@app.get("/api/feedback")
async def feedback() -> dict[str, Any]:
    """The warning-to-action funnel.

    This is the measurable-impact number the whole field is missing: not how
    many messages were sent, but how many were acted on.
    """
    by_district: dict[str, dict[str, int]] = {}
    for item in STATE.feedback:
        bucket = by_district.setdefault(item.pcode, {})
        bucket[item.kind.value] = bucket.get(item.kind.value, 0) + 1

    acted = sum(
        1
        for f in STATE.feedback
        if f.kind
        in (
            FeedbackKind.LIVESTOCK_MOVED,
            FeedbackKind.ACTION_TAKEN,
        )
    )
    return {
        "total": len(STATE.feedback),
        "by_district": by_district,
        "reached": len(STATE.advisories),
        "responded": len(STATE.feedback),
        "acted": acted,
    }


@app.get("/api/cap.xml")
async def cap(response: Response) -> Response:
    """CAP 1.2 feed of every verified advisory.

    Emitting CAP costs almost nothing and buys interoperability with HUSIKA
    (which is CAP-compliant) and a clean ingestion path into Kenya's incoming
    cell broadcast system. We consume the national met agencies' CAP feeds, so
    we should speak the same language back.
    """
    xml = cap_feed(
        [a for a in STATE.all_advisories if a.dispatchable],
        base_url=get_settings().public_base_url,
    )
    return Response(content=xml, media_type="application/cap+xml")


# ---------------------------------------------------------------------------
# USSD
# ---------------------------------------------------------------------------


def _menu() -> USSDMenu:
    districts = [
        (r.assessment.pcode, r.assessment.admin_name)
        for r in sorted(
            STATE.results, key=lambda r: -r.assessment.compound_risk_score
        )
    ] or [(p, u.name) for p, u in DEMO_UNITS.items()]
    return USSDMenu(districts, STATE.lookup, STATE.record_feedback)


@app.post("/ussd", response_class=PlainTextResponse)
async def ussd(
    sessionId: str = Form(default=""),
    serviceCode: str = Form(default=""),
    phoneNumber: str = Form(default=""),
    text: str = Form(default=""),
) -> str:
    """Africa's Talking USSD callback.

    Must return in well under three seconds. Everything here is an in-memory
    read — no network, no model, no database.
    """
    return _menu().handle(
        USSDRequest(
            session_id=sessionId,
            service_code=serviceCode,
            phone_number=phoneNumber,
            text=text,
        )
    )


@app.get("/ussd/simulate", response_class=PlainTextResponse)
async def ussd_simulate(text: str = "") -> str:
    """Browser-testable USSD, so the demo does not depend on the simulator."""
    return _menu().handle(
        USSDRequest("sim", "*384*7899#", "+254700000000", text)
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    from .dashboard import render_dashboard

    return render_dashboard()
