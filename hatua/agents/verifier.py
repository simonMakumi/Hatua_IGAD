"""
The Verifier: the blocking guardrail on every outgoing advisory.

The argument for this module
----------------------------
HATUA sends messages that ask people to make irreversible decisions. Sell the
breeding stock. Move the family. Harvest early. Leave the house tonight.

If a model hallucinates a rainfall figure, a person acts on a fiction with
their livelihood. That is not a quality defect to be tuned away with a better
prompt — it is a harm, and it needs a mechanism, not an intention.

So nothing is dispatched that has not passed here. An ``Advisory`` whose
``verification`` is missing or ``BLOCKED`` cannot be sent; ``Advisory.
dispatchable`` is False and the delivery layer refuses it.

Design
------
Six checks. Five are **deterministic** and run first, because a check that
depends on a model cannot be trusted to police a model. Only if all five pass
does an LLM adjudicate the sixth — semantic faithfulness — and its vote can
only ever *block*, never rescue something the deterministic checks rejected.

    1. Numeric fidelity      every figure traces to a SignalReading
    2. Action legitimacy     every action_id exists in the approved library
    3. Severity gate         severity within the confidence ceiling
    4. Encoding budget       fits its channel and language segment cap
    5. Geographic sanity     the named place matches the triggered district
    6. Semantic faithfulness (LLM) no unsupported causal or temporal claims

Failing open is not an option anywhere in this file. If the verifier itself
errors, the advisory is BLOCKED.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..delivery.encoding import MAX_SEGMENTS, measure, normalise_for_gsm7
from ..models import (
    ActionPlan,
    Advisory,
    Channel,
    ImpactHypothesis,
    RiskAssessment,
    TriggerEvent,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)
from . import actions as action_library
from .llm import LLM, LLMError

log = logging.getLogger("hatua.verifier")

# Numbers that carry no factual claim and need no provenance: small counts
# ("2 days"), percentages of nothing, ordinals, and years within a plausible
# window. Anything else must be traceable.
_BENIGN_MAX = 31

_NUMBER_RE = re.compile(r"\d[\d,.٫٬]*")

# Arabic-Indic and Extended Arabic-Indic digits, used in Arabic-script
# messages. A naive [0-9] scan misses these entirely, which would let an
# Arabic advisory carry unverified figures straight through.
_DIGIT_TRANSLATION = {
    ord(c): str(i % 10)
    for i, c in enumerate("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹")
}


def _normalise_digits(text: str) -> str:
    """Fold Arabic-Indic and Ethiopic numerals to ASCII before scanning."""
    text = text.translate(_DIGIT_TRANSLATION)
    out = []
    for ch in text:
        if unicodedata.category(ch) == "Nd" and not ch.isascii():
            try:
                out.append(str(unicodedata.decimal(ch)))
                continue
            except (ValueError, TypeError):
                pass
        out.append(ch)
    return "".join(out)


def _extract_numbers(text: str) -> list[float]:
    values: list[float] = []
    for match in _NUMBER_RE.finditer(_normalise_digits(text)):
        raw = match.group().replace(",", "").replace("٬", "")
        raw = raw.replace("٫", ".").rstrip(".")
        if not raw:
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def _supported_values(
    trigger: TriggerEvent,
    assessment: RiskAssessment,
    hypothesis: ImpactHypothesis,
) -> set[float]:
    """Every number the advisory is permitted to contain."""
    allowed: set[float] = set()

    for s in trigger.signals:
        if s.value is not None:
            allowed.add(round(float(s.value), 2))
            allowed.add(float(round(s.value)))

    v = assessment.vulnerability
    e = assessment.exposure
    for candidate in (
        v.ipc_phase,
        v.conflict_events_90d,
        v.fatalities_90d,
        v.idps,
        v.refugees,
        v.ipc_population_in_phase3plus,
        e.population,
        hypothesis.estimated_people_affected,
        trigger.lead_time_days,
    ):
        if candidate is not None:
            allowed.add(float(candidate))

    # Dates in the advisory window, expressed as day and year numbers.
    for d in (trigger.window_start, trigger.window_end):
        allowed.add(float(d.day))
        allowed.add(float(d.year))

    # Rounded forms of large figures, since a message will sensibly say
    # "64,000" for 64,321 or "112 thousand" for 112,400.
    for value in list(allowed):
        if value >= 1000:
            for unit in (100, 1000, 10_000):
                allowed.add(float(round(value / unit) * unit))
            allowed.add(float(round(value / 1000)))
        if value >= 1_000_000:
            allowed.add(float(round(value / 1_000_000)))
    return allowed


# ---------------------------------------------------------------------------
# Semantic check (LLM)
# ---------------------------------------------------------------------------


class _SemanticVerdict(BaseModel):
    faithful: bool = Field(
        description="True only if every claim in the message is supported by "
                    "the evidence."
    )
    unsupported_claims: list[str] = Field(default_factory=list)
    severity_appropriate: bool = Field(
        description="True if the message's urgency matches the stated severity "
                    "and does not escalate beyond it."
    )
    reasoning: str = Field(max_length=600)


VERIFIER_SYSTEM = """\
You are a verification checkpoint for a humanitarian early warning system in \
the Greater Horn of Africa. Your job is adversarial: find reasons this message \
must NOT be sent.

The message will be delivered to people who may act on it irreversibly — \
selling livestock, moving their family, harvesting a crop early. A false or \
overstated warning causes real harm and destroys trust in every future warning.

Mark faithful = false if the message does ANY of the following:

- States a fact, figure or causal claim not supported by the evidence block
- Asserts something WILL happen when the evidence only supports that it MIGHT
- Attributes the warning to an authority that did not issue it
- Names a place, river, road or facility not present in the evidence
- Promises assistance, relief or compensation that the evidence does not confirm
- Gives a deadline that does not match an approved action's deadline, or a \
timing outside the stated window
- Instructs the reader to do something that is NOT among the approved actions
- Misuses a figure — for example, presenting a displacement count as a count of \
people at risk from a different hazard

Do NOT flag the following, which are legitimate:
- An action deadline that matches the approved deadline for that action
- Natural rephrasing of an approved instruction into the target language
- Naming the district the advisory is scoped to

Mark severity_appropriate = false if the message is more urgent or more \
directive than the stated severity level permits.

Being conservative is correct. Blocking a sound message costs one cycle. \
Sending an unsound one costs trust that takes years to rebuild.

Return only JSON."""


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class Verifier:
    def __init__(self, llm: LLM | None = None, *, use_llm: bool = True) -> None:
        self._llm = llm
        self.use_llm = use_llm

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM()
        return self._llm

    async def verify(
        self,
        advisory: Advisory,
        *,
        trigger: TriggerEvent,
        assessment: RiskAssessment,
        hypothesis: ImpactHypothesis,
        plan: ActionPlan,
        evidence: str,
    ) -> VerificationResult:
        checks: list[VerificationCheck] = []
        blocked: list[str] = []

        def record(name: str, passed: bool, detail: str) -> None:
            checks.append(
                VerificationCheck(name=name, passed=passed, detail=detail)
            )
            if not passed:
                blocked.append(f"{name}: {detail}")

        # -- 1. Numeric fidelity -------------------------------------------
        allowed = _supported_values(trigger, assessment, hypothesis)
        found = _extract_numbers(advisory.body)
        unsupported = [
            n
            for n in found
            if n > _BENIGN_MAX
            and not any(abs(n - a) < max(1.0, abs(a) * 0.02) for a in allowed)
        ]
        record(
            "numeric_fidelity",
            not unsupported,
            f"{len(found)} figures found, all traceable"
            if not unsupported
            else f"untraceable figures: {unsupported}",
        )

        # -- 2. Action legitimacy ------------------------------------------
        unknown = [a for a in advisory.action_ids if not action_library.exists(a)]
        record(
            "action_legitimacy",
            not unknown and bool(plan.actions),
            f"{len(advisory.action_ids)} actions, all in the approved library"
            if not unknown and plan.actions
            else (
                f"actions not in library: {unknown}"
                if unknown
                else "advisory carries no actions"
            ),
        )

        # -- 3. Severity gate ----------------------------------------------
        ceiling = trigger.max_permitted_severity
        within = advisory.severity.rank <= ceiling.rank
        record(
            "severity_gate",
            within,
            f"{advisory.severity.value} within {ceiling.value} ceiling "
            f"(confidence {trigger.confidence_score:.2f})"
            if within
            else f"{advisory.severity.value} exceeds the {ceiling.value} "
            f"ceiling permitted at confidence {trigger.confidence_score:.2f}",
        )

        # -- 4. Encoding and budget ----------------------------------------
        if advisory.channel.has_hard_length_limit:
            body = advisory.body
            m = measure(body)
            limit = MAX_SEGMENTS.get(advisory.language.value, 2)

            # If Latin-script text was pushed into UCS-2 by substitutable
            # punctuation, that is an authoring bug we can fix rather than a
            # reason to block.
            if (
                m.encoding == "UCS-2"
                and advisory.language.is_latin_script
                and measure(normalise_for_gsm7(body)).encoding == "GSM-7"
            ):
                body = normalise_for_gsm7(body)
                m = measure(body)
                advisory = advisory.model_copy(update={"body": body})

            ok = m.segments <= limit
            advisory = advisory.model_copy(
                update={
                    "encoding": m.encoding,
                    "segment_count": m.segments,
                    "septet_count": m.units,
                }
            )
            record(
                "encoding_budget",
                ok,
                m.describe()
                if ok
                else f"{m.describe()} exceeds the {limit}-segment cap for "
                f"{advisory.language.value}",
            )
        else:
            record(
                "encoding_budget", True, f"{advisory.channel.value}: no hard cap"
            )

        # -- 5. Geographic sanity ------------------------------------------
        place_ok = (
            advisory.pcode == trigger.pcode
            and assessment.admin_name.split()[0].lower()
            in advisory.body.lower() + advisory.admin_name.lower()
        )
        record(
            "geographic_sanity",
            place_ok,
            f"advisory scoped to {advisory.admin_name} ({advisory.pcode})"
            if place_ok
            else f"place mismatch: advisory {advisory.pcode}/"
            f"{advisory.admin_name} vs trigger {trigger.pcode}/"
            f"{trigger.admin_name}",
        )

        # -- 6. Semantic faithfulness (LLM) --------------------------------
        # Only runs if the deterministic checks passed. A model cannot be
        # trusted to overrule arithmetic, so its vote can block but never
        # rescue.
        if not self.use_llm:
            record("semantic_faithfulness", True, "LLM check disabled")
        elif blocked:
            record(
                "semantic_faithfulness",
                True,
                "skipped — deterministic checks already blocked this advisory",
            )
        else:
            try:
                verdict = await self.llm.structured(
                    system=VERIFIER_SYSTEM,
                    user=(
                        f"EVIDENCE:\n{evidence}\n\n"
                        f"APPROVED ACTIONS CONVEYED — the message may state "
                        f"these instructions and these deadlines:\n"
                        + "\n".join(
                            f"  - [{a.actor.value}] {a.instruction} "
                            f"(approved deadline: within {a.deadline_days} days)"
                            for a in plan.actions
                        )
                        + f"\n\nSTATED SEVERITY: {advisory.severity.value}\n"
                        f"LANGUAGE: {advisory.language.value}\n\n"
                        f"MESSAGE AS IT WOULD BE SENT:\n{advisory.body}\n\n"
                        f"Verify it."
                    ),
                    response_model=_SemanticVerdict,
                    max_tokens=1000,
                )
                passed = verdict.faithful and verdict.severity_appropriate
                detail = verdict.reasoning
                if verdict.unsupported_claims:
                    detail += " | unsupported: " + "; ".join(
                        verdict.unsupported_claims[:4]
                    )
                record("semantic_faithfulness", passed, detail[:500])
            except LLMError as exc:
                # Fail closed. An unavailable verifier is not permission to send.
                record(
                    "semantic_faithfulness",
                    False,
                    f"verification unavailable, blocking by default: {exc}"[:300],
                )

        status = (
            VerificationStatus.PASSED if not blocked else VerificationStatus.BLOCKED
        )
        if blocked:
            log.warning(
                "BLOCKED %s/%s (%s): %s",
                advisory.pcode,
                advisory.language.value,
                advisory.channel.value,
                "; ".join(blocked),
            )

        return VerificationResult(
            status=status,
            checks=checks,
            blocked_reasons=blocked,
            verified_at=datetime.now(timezone.utc),
        )


def summarise(result: VerificationResult) -> str:
    """One-line rendering for the dashboard and logs."""
    mark = "PASS" if result.passed else "BLOCK"
    passed = sum(1 for c in result.checks if c.passed)
    return f"[{mark}] {passed}/{len(result.checks)} checks" + (
        "" if result.passed else f" — {result.blocked_reasons[0]}"
    )
