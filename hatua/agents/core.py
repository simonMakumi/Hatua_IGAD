"""
The HATUA reasoning core: Impact Analyst, Action Planner, Localizer.

Sequence
--------
    RiskAssessment + TriggerEvent   (deterministic, from fusion/)
        -> ImpactAnalyst   -> ImpactHypothesis   what actually breaks, for whom
        -> ActionPlanner   -> ActionPlan         what to do, by actor, by when
        -> Localizer       -> Advisory           per language, per channel
        -> Verifier        -> PASS / BLOCK       (see verifier.py)

Each stage has a typed Pydantic contract, so a malformed stage fails loudly at
its own boundary rather than producing a fluent, wrong message three stages
later.

Two constraints shape every prompt here:

**The model never sees raw data it could misread as authority.** It receives
the fused numbers and the exact provenance strings, and is told that any figure
it states must come from that list. This is what makes verification possible at
all — an advisory citing an uncitable number is mechanically detectable.

**The model never invents an action.** It selects ``action_id`` values from a
list it is shown, and that list has already been filtered by severity and by
whether the situation warrants committing (irreversible) actions. The
constraint is applied before generation rather than policed after it.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from pydantic import BaseModel, Field

from ..models import (
    ActionPlan,
    ActorType,
    AnticipatoryAction,
    Channel,
    Confidence,
    HazardType,
    ImpactHypothesis,
    Language,
    RiskAssessment,
    Severity,
    TriggerEvent,
)
from . import actions as action_library
from .llm import LLM

log = logging.getLogger("hatua.agents")


# ---------------------------------------------------------------------------
# Shared context rendering
# ---------------------------------------------------------------------------


def render_evidence(assessment: RiskAssessment, trigger: TriggerEvent) -> str:
    """The complete factual basis the model is allowed to draw on.

    Everything the model may assert must be derivable from this block. The
    Verifier is given the same block and checks the output against it.
    """
    lines = [
        f"LOCATION: {assessment.admin_name} ({assessment.pcode}), "
        f"{assessment.country_iso3}",
        f"TRIGGER: {trigger.threshold_name} — observed "
        f"{trigger.observed_value:.2f} against threshold "
        f"{trigger.threshold_value:.2f}",
        f"WINDOW: {trigger.window_start} to {trigger.window_end} "
        f"({trigger.lead_time_days} days lead time)",
        f"CONFIDENCE: {trigger.confidence_score:.2f} "
        f"({trigger.confidence.value}) — maximum severity permitted is "
        f"{trigger.max_permitted_severity.value.upper()}",
        f"DATA SUFFICIENCY: {assessment.data_sufficiency:.2f} "
        f"({int(assessment.data_sufficiency * 5)} of 5 source families reporting)",
        "",
        "HAZARD SCORES (0-1, from deterministic fusion):",
    ]
    for hazard, score in sorted(
        assessment.hazard_scores.items(), key=lambda kv: kv[1], reverse=True
    ):
        lines.append(f"  {hazard.value}: {score:.2f}")

    v = assessment.vulnerability
    lines += ["", "VULNERABILITY CONTEXT:"]
    lines.append(
        f"  IPC food security phase: {v.ipc_phase if v.ipc_phase else 'no data'}"
        + (f" (ref {v.ipc_reference_period})" if v.ipc_reference_period else "")
    )
    lines.append(f"  Conflict events, last 90 days: {v.conflict_events_90d or 0}")
    lines.append(f"  Fatalities, last 90 days: {v.fatalities_90d or 0}")
    lines.append(f"  Internally displaced people: {v.idps or 'no data'}")
    lines.append(f"  Refugees: {v.refugees or 'no data'}")
    if v.data_gaps:
        lines.append(f"  KNOWN DATA GAPS: {', '.join(v.data_gaps)}")

    e = assessment.exposure
    lines += ["", "EXPOSURE:"]
    lines.append(
        f"  Population: {e.population:,}" if e.population else "  Population: no data"
    )
    if e.population_year:
        lines.append(f"  Population reference year: {e.population_year}")

    lines += ["", "CITABLE SIGNALS — you may only state figures that appear here:"]
    for s in trigger.signals[:20]:
        lines.append(f"  [{s.citation}]" + (f"  {s.note}" if s.note else ""))

    if trigger.cap_alerts:
        lines += ["", "OFFICIAL GOVERNMENT ALERTS IN FORCE:"]
        for a in trigger.cap_alerts:
            lines.append(
                f"  {a.sender}: {a.event} — severity {a.severity_raw}, "
                f"urgency {a.urgency}, certainty {a.certainty}"
            )
            if a.headline:
                lines.append(f'    "{a.headline}"')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. Impact Analyst
# ---------------------------------------------------------------------------

ANALYST_SYSTEM = """\
You are a humanitarian impact analyst for ICPAC, the WMO Regional Climate \
Centre for the Greater Horn of Africa. You work on anticipatory action: \
turning forecasts into an understanding of what will actually happen to people.

Your task is NOT to restate the meteorology. It is to state the consequence.

Bad:  "220mm of rainfall is forecast over five days."
Good: "220mm over five days onto soils already saturated by the March-May \
rains, upstream of a district hosting 34,000 displaced people in informal \
shelter on the floodplain."

Rules you must follow exactly:

1. Every numeric figure you state must appear in the CITABLE SIGNALS block or \
the vulnerability/exposure context. If a number is not there, do not state it. \
Say "an unknown number of" instead. Inventing a casualty or population figure \
in an early warning product is a serious harm, not a stylistic flaw.

2. Do not restate a population or displacement figure as an estimate of people \
affected unless the evidence supports that specific inference. If 64,000 people \
are displaced in a district, that is not the same as 64,000 people being at \
risk from a drought.

3. Reason about compounding. In this region climate hazards land on populations \
already under conflict, displacement and food insecurity stress. The same \
rainfall deficit means something different at IPC Phase 4 than at Phase 1.

4. Be explicit about what you do not know. The uncertainties field is not \
decoration; it is read by the officer deciding whether to act.

5. Write for a district disaster officer, not a climate scientist.

Return only JSON."""


class ImpactAnalyst:
    def __init__(self, llm: LLM | None = None) -> None:
        self.llm = llm or LLM()

    async def analyse(
        self, assessment: RiskAssessment, trigger: TriggerEvent
    ) -> ImpactHypothesis:
        evidence = render_evidence(assessment, trigger)
        user = (
            f"{evidence}\n\n"
            f"Produce an ImpactHypothesis for the {trigger.hazard.value} "
            f"trigger in {assessment.admin_name}.\n"
            f"Set pcode to exactly '{assessment.pcode}' and hazard to exactly "
            f"'{trigger.hazard.value}'.\n"
            f"Set onset_window_start to {trigger.window_start.isoformat()} and "
            f"onset_window_end to {trigger.window_end.isoformat()}.\n"
            f"Set confidence_score to {trigger.confidence_score:.2f}.\n"
            f"In cited_signals, copy the exact bracketed citation strings you "
            f"relied on."
        )
        hypothesis = await self.llm.structured(
            system=ANALYST_SYSTEM,
            user=user,
            response_model=ImpactHypothesis,
            max_tokens=2000,
        )
        # Pin the fields the model must not be trusted to set. These are facts
        # from the deterministic layer, not judgements.
        return hypothesis.model_copy(
            update={
                "pcode": assessment.pcode,
                "hazard": trigger.hazard,
                "onset_window_start": trigger.window_start,
                "onset_window_end": trigger.window_end,
                "confidence_score": trigger.confidence_score,
            }
        )


# ---------------------------------------------------------------------------
# 2. Action Planner
# ---------------------------------------------------------------------------


class _SelectedAction(BaseModel):
    action_id: str
    actor: ActorType
    contextual_note: str = Field(
        max_length=600,
        description="One sentence tying the standard action to this specific "
                    "district and window. Must not contradict the action.",
    )


class _PlannerOutput(BaseModel):
    severity: Severity
    selected: list[_SelectedAction] = Field(min_length=1, max_length=6)
    reasoning: str = Field(max_length=2000)


PLANNER_SYSTEM = """\
You are an anticipatory action planner for the Greater Horn of Africa.

You do NOT write advice. You SELECT actions from the approved library you are \
shown, and add one sentence of local context to each.

Rules:

1. You may only use action_id values from the ELIGIBLE ACTIONS list. Any other \
value will be rejected and the advisory will not be sent.

2. Severity must not exceed the MAXIMUM PERMITTED SEVERITY stated in the \
evidence. That ceiling comes from forecast confidence. Telling people to \
evacuate on a signal we are 45% sure of destroys trust in every future warning, \
and trust is the only thing that makes an early warning system work at all.

3. Select actions for the actors who are actually present. A district that is \
predominantly pastoralist does not need farming advice.

4. Order by urgency: shortest deadline first.

5. Prefer fewer, clearer actions. A person receiving this on a feature phone \
can act on two things, not six.

Return only JSON."""


class ActionPlanner:
    def __init__(self, llm: LLM | None = None) -> None:
        self.llm = llm or LLM()

    async def plan(
        self,
        assessment: RiskAssessment,
        trigger: TriggerEvent,
        hypothesis: ImpactHypothesis,
    ) -> ActionPlan:
        ceiling = trigger.max_permitted_severity

        # The safety gate: below HIGH confidence, only no-regret actions are
        # even shown to the model, so it cannot recommend an irreversible
        # livelihood decision on a signal we do not trust.
        no_regret_only = trigger.confidence in (
            Confidence.LOW,
            Confidence.MODERATE,
        )

        catalogue = action_library.catalogue_for_prompt(
            trigger.hazard, ceiling, no_regret_only=no_regret_only
        )

        user = (
            f"{render_evidence(assessment, trigger)}\n\n"
            f"IMPACT ASSESSMENT:\n{hypothesis.summary}\n"
            f"Mechanism: {hypothesis.physical_mechanism}\n"
            f"Exposed groups: {', '.join(hypothesis.exposed_groups)}\n"
            f"Compounding: {', '.join(hypothesis.compounding_factors) or 'none'}\n\n"
            f"MAXIMUM PERMITTED SEVERITY: {ceiling.value.upper()}\n"
            + (
                "CONFIDENCE IS BELOW 'HIGH', so only no-regret actions are "
                "available. Irreversible actions have been withheld.\n"
                if no_regret_only
                else ""
            )
            + f"\nELIGIBLE ACTIONS:\n{catalogue}\n\n"
            f"Select the actions that fit this district and window."
        )

        out = await self.llm.structured(
            system=PLANNER_SYSTEM,
            user=user,
            response_model=_PlannerOutput,
            max_tokens=1500,
        )

        # Enforce the ceiling in code. The prompt asks; this guarantees.
        severity = out.severity
        if severity.rank > ceiling.rank:
            log.warning(
                "%s: planner proposed %s above ceiling %s — clamped",
                assessment.pcode,
                severity.value,
                ceiling.value,
            )
            severity = ceiling

        chosen: list[AnticipatoryAction] = []
        for sel in out.selected:
            if not action_library.exists(sel.action_id):
                log.warning(
                    "%s: planner invented action_id %r — discarded",
                    assessment.pcode,
                    sel.action_id,
                )
                continue
            base = action_library.ACTIONS[sel.action_id]
            if no_regret_only and not base.no_regret:
                log.warning(
                    "%s: planner selected committing action %r at %s "
                    "confidence — discarded",
                    assessment.pcode,
                    sel.action_id,
                    trigger.confidence.value,
                )
                continue
            chosen.append(
                base.model_copy(
                    update={
                        "rationale": f"{base.rationale} {sel.contextual_note}".strip()
                    }
                )
            )

        chosen.sort(key=lambda a: a.deadline_days)

        if not chosen:
            # Rather than send nothing, fall back to the library's own
            # no-regret actions for this hazard. If even that is empty, the
            # ActionPlan will be empty and the Verifier will block it.
            chosen = action_library.candidates(
                trigger.hazard, severity, no_regret_only=True
            )[:2]
            log.warning(
                "%s: no valid model selections, fell back to %d library "
                "no-regret actions",
                assessment.pcode,
                len(chosen),
            )

        return ActionPlan(
            pcode=assessment.pcode,
            hazard=trigger.hazard,
            severity=severity,
            actions=chosen[:4],
            lead_time_days=trigger.lead_time_days,
            plan_confidence=trigger.confidence_score,
        )


# ---------------------------------------------------------------------------
# 3. Localizer
# ---------------------------------------------------------------------------

# Verb ladder. Severity controls how directive the language is allowed to be,
# and severity is itself capped by confidence. This is the chain that stops a
# probabilistic forecast from producing an imperative order.
SEVERITY_VERBS: dict[Severity, str] = {
    Severity.ADVISORY: "informational — 'be aware', 'check', 'consider'. "
                       "Do not use imperatives.",
    Severity.WATCH: "preparatory — 'prepare to', 'get ready to', 'start'. "
                    "Do not order immediate irreversible action.",
    Severity.WARNING: "directive — 'move', 'harvest now', 'do not cross'. "
                      "Clear instructions are appropriate.",
    Severity.EMERGENCY: "imperative and immediate — 'leave now'. "
                        "Use only for imminent threat to life.",
}

LANGUAGE_NAMES: dict[Language, str] = {
    Language.ENGLISH: "English",
    Language.SWAHILI: "Kiswahili",
    Language.SOMALI: "Somali (Af-Soomaali)",
    Language.AMHARIC: "Amharic (አማርኛ)",
    Language.OROMO: "Afaan Oromoo",
    Language.TIGRINYA: "Tigrinya (ትግርኛ)",
    Language.ARABIC: "Arabic (العربية)",
}


class _LocalizedMessage(BaseModel):
    body: str
    contains_numbers: list[str] = Field(
        default_factory=list,
        description="Every numeric figure appearing in the body, as written.",
    )


LOCALIZER_SYSTEM = """\
You write early warning messages that reach people on basic mobile phones in \
the Greater Horn of Africa.

Your reader may have limited literacy, is not a technical person, and is \
paying attention for about five seconds. Many will hear this read aloud rather \
than read it.

Rules:

1. Lead with the place and the hazard. The reader must know in the first four \
words whether this concerns them.

2. State the action, not the science. No probabilities, no indices, no \
technical terms. Never write "SPI", "anomaly", "percentile" or "IPC Phase".

3. Use only the numbers given to you. Do not add, round differently, or infer.

4. Match the severity register you are given, exactly. Do not escalate.

5. Short sentences. No jargon, no marketing tone, no emoji.

6. HARD CHARACTER LIMIT. Exceeding it means the message is truncated or costs \
more to send than the programme can afford. Ge'ez and Arabic script cost more \
than double per character, so the limit for those languages is much shorter. \
Count carefully.

7. Write naturally in the target language. Do not translate word for word from \
English — write as someone from the region would say it.

Return only JSON."""


class Localizer:
    def __init__(self, llm: LLM | None = None) -> None:
        self.llm = llm or LLM()

    async def render(
        self,
        plan: ActionPlan,
        hypothesis: ImpactHypothesis,
        assessment: RiskAssessment,
        *,
        language: Language,
        channel: Channel,
        char_limit: int,
    ) -> str:
        action_text = "\n".join(
            f"  - [{a.actor.value}] {a.instruction} (within {a.deadline_days} days)"
            for a in plan.actions
        )
        script_note = (
            ""
            if language.is_latin_script
            else (
                f"\nNOTE: {LANGUAGE_NAMES[language]} uses a non-Latin script, "
                f"which costs more than twice as much per character to send. "
                f"The {char_limit}-character limit is therefore strict and "
                f"short. Be ruthless."
            )
        )

        # Distinguish a hazard that is forecast from one already underway.
        # Without this the model writes "get ready for food insecurity" to a
        # district already at IPC Phase 4, which is both wrong and insulting —
        # and the Verifier correctly blocks it as an unsupported claim about
        # the future. The severity register controls urgency; this controls
        # tense.
        v = assessment.vulnerability
        already_present = bool(
            (plan.hazard is HazardType.FOOD_INSECURITY and v.ipc_phase and v.ipc_phase >= 3)
            or (plan.hazard is HazardType.DISPLACEMENT and v.idps and v.idps > 10_000)
            or (plan.hazard is HazardType.CONFLICT and v.conflict_events_90d
                and v.conflict_events_90d > 25)
        )
        tense = (
            "THIS CONDITION IS ALREADY PRESENT, not forecast. Do NOT write "
            "'prepare for' or 'get ready for' — it is already happening. "
            "Write about worsening, or about acting now.\n"
            if already_present
            else "This condition is FORECAST, not yet observed. Do not state "
                 "it as already happening.\n"
        )

        user = (
            f"PLACE: {assessment.admin_name}, {assessment.country_iso3}\n"
            f"HAZARD: {plan.hazard.value}\n"
            f"{tense}"
            f"SEVERITY: {plan.severity.value} — tone must be "
            f"{SEVERITY_VERBS[plan.severity]}\n"
            f"WINDOW: {hypothesis.onset_window_start} to "
            f"{hypothesis.onset_window_end}\n"
            f"IMPACT: {hypothesis.summary}\n\n"
            f"ACTIONS TO CONVEY:\n{action_text}\n\n"
            f"LANGUAGE: {LANGUAGE_NAMES[language]}\n"
            f"CHANNEL: {channel.value}\n"
            f"HARD LIMIT: {char_limit} characters{script_note}\n\n"
            f"Write the message body in {LANGUAGE_NAMES[language]}."
        )

        out = await self.llm.structured(
            system=LOCALIZER_SYSTEM,
            user=user,
            response_model=_LocalizedMessage,
            max_tokens=800,
        )
        return out.body.strip()


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def default_languages(country_iso3: str) -> list[Language]:
    """Which languages an advisory should be issued in for a given country.

    Deliberately includes English everywhere, because county and national
    officers work in it, and it is the fallback when a translation cannot be
    verified.
    """
    mapping: dict[str, list[Language]] = {
        "KEN": [Language.SWAHILI, Language.ENGLISH, Language.SOMALI],
        "UGA": [Language.SWAHILI, Language.ENGLISH],
        "SOM": [Language.SOMALI, Language.ENGLISH],
        "ETH": [
            Language.AMHARIC,
            Language.OROMO,
            Language.SOMALI,
            Language.ENGLISH,
        ],
        "ERI": [Language.TIGRINYA, Language.ARABIC, Language.ENGLISH],
        "DJI": [Language.SOMALI, Language.ARABIC, Language.ENGLISH],
        "SDN": [Language.ARABIC, Language.ENGLISH],
        "SSD": [Language.ENGLISH, Language.ARABIC],
    }
    return mapping.get(country_iso3, [Language.ENGLISH])
