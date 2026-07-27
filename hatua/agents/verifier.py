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

# Prefix marking a check that did not run, so it is never counted as a pass.
_SKIPPED = "SKIPPED: "

# Numbers small enough to be structural rather than factual: "under 5 years",
# "within 14 days", "1. do this". Anything larger must be traceable.
#
# This was 31, which exempted rainfall in mm, SPI values, borehole counts, IPC
# phase numbers and — the reason it was lowered — casualty figures. An audit
# body reading "30 people died. 25 boreholes are dry. 28 mm fell." passed every
# figure as benign. 20 still admits ages and short deadlines while catching the
# range a fabricated impact claim actually lives in.
_BENIGN_MAX = 20

# Words that make a nearby number a factual claim regardless of size. "5 people
# died" is not the same kind of 5 as "children under 5".
_CLAIM_CONTEXT = re.compile(
    r"(died|dead|death|killed|casualt|injur|destroy|lost|damage|"
    r"displac|affect|mm|millimet|percent|%|phase)",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(r"\d[\d,.٫٬]*")

# Arabic-Indic and Extended Arabic-Indic digits, used in Arabic-script
# messages. A naive [0-9] scan misses these entirely, which would let an
# Arabic advisory carry unverified figures straight through.
_DIGIT_TRANSLATION = {
    ord(c): str(i % 10)
    for i, c in enumerate("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹")
}

# Ethiopic numerals (፩ ፪ ፫ … ፻ ፼) used in Amharic and Tigrinya.
#
# These are Unicode category **No**, not Nd, and `\d` does not match them — so
# the generic decimal-folding below silently skipped every one. An Amharic
# advisory carrying ፻፳፭ was invisible to numeric verification, in exactly the
# languages this project makes a virtue of supporting. Caught in audit.
_ETHIOPIC_DIGITS: dict[int, int] = {
    0x1369: 1, 0x136A: 2, 0x136B: 3, 0x136C: 4, 0x136D: 5,
    0x136E: 6, 0x136F: 7, 0x1370: 8, 0x1371: 9,
    0x1372: 10, 0x1373: 20, 0x1374: 30, 0x1375: 40, 0x1376: 50,
    0x1377: 60, 0x1378: 70, 0x1379: 80, 0x137A: 90,
    0x137B: 100, 0x137C: 10_000,
}


def _fold_ethiopic(text: str) -> str:
    """Replace runs of Ethiopic numerals with their ASCII value.

    Ethiopic numerals are additive-multiplicative: ፻፳፭ is 100 + 20 + 5 = 125.
    This is a deliberately simple evaluator — it will not perfectly render
    every historical form, but it must not *miss* a numeral, because a missed
    numeral is an unverified claim reaching a reader.
    """
    out: list[str] = []
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        total = 0
        group = 0
        for value in run:
            if value >= 100:
                group = (group or 1) * value
                total += group
                group = 0
            else:
                group += value
        out.append(str(total + group))
        run.clear()

    for ch in text:
        value = _ETHIOPIC_DIGITS.get(ord(ch))
        if value is not None:
            run.append(value)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out)


def _normalise_digits(text: str) -> str:
    """Fold Arabic-Indic and Ethiopic numerals to ASCII before scanning."""
    text = _fold_ethiopic(text.translate(_DIGIT_TRANSLATION))
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
    reasoning: str = Field(max_length=2000)


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
        claim_context = bool(_CLAIM_CONTEXT.search(advisory.body))

        def traceable(n: float) -> bool:
            # 0.5% or half a unit, whichever is larger. The previous 2% band
            # meant a stated 6,600,000 matched a true 6,524,000 — a 76,000
            # person exaggeration passing as "traceable".
            return any(
                abs(n - a) <= max(0.5, abs(a) * 0.005) for a in allowed
            )

        unsupported = [
            n
            for n in found
            # A small number is only exempt when nothing in the message frames
            # it as a claim. "5 people died" is checked; "under 5 years" is not.
            if (n > _BENIGN_MAX or claim_context) and not traceable(n)
        ]
        record(
            "numeric_fidelity",
            not unsupported,
            f"{len(found)} figures checked, all traceable to a source reading"
            if not unsupported
            else f"figures with no source reading: {unsupported}",
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
        # An earlier version of this check searched the concatenation
        # `body + admin_name` for the district name — which always contains it,
        # so the check could never fail. It emitted a reassuring string naming
        # a district that appeared nowhere in the message. Caught in audit by
        # feeding it a body reading "Nairobi County: flooding tonight" against
        # a Somali Region trigger: PASSED.
        #
        # The message body alone must name the place. A warning that does not
        # tell you where it applies is worse than no warning: it is acted on
        # by people it does not concern, and ignored by people it does.
        body = advisory.body.lower()
        tokens = [
            t.lower()
            for t in re.split(r"[\s,/()-]+", assessment.admin_name)
            if len(t) > 2
        ]
        named = any(t in body for t in tokens) if tokens else False
        scoped = advisory.pcode == trigger.pcode

        if not tokens:
            # No usable place token (single short name). Fall back to the
            # pcode check rather than blocking, but say so.
            record(
                "geographic_sanity",
                scoped,
                f"scoped to {advisory.pcode}; district name too short to "
                f"verify in body",
            )
        else:
            record(
                "geographic_sanity",
                scoped and named,
                f"body names '{assessment.admin_name}' and is scoped to "
                f"{advisory.pcode}"
                if scoped and named
                else (
                    f"body does not name the district "
                    f"'{assessment.admin_name}' (looked for: "
                    f"{', '.join(tokens)})"
                    if scoped
                    else f"pcode mismatch: advisory {advisory.pcode} vs "
                    f"trigger {trigger.pcode}"
                ),
            )

        # -- 6. Semantic faithfulness (LLM) --------------------------------
        # Only runs if the deterministic checks passed. A model cannot be
        # trusted to overrule arithmetic, so its vote can block but never
        # rescue.
        if advisory.source == "template":
            # Assembled from pre-reviewed sentences with only verified numerals
            # and place names substituted. There is no generated prose here for
            # a semantic check to examine, and the models we have available
            # cannot read these languages well enough to judge them — in
            # testing they rejected correct Afaan Oromo templates. The five
            # deterministic checks above still ran in full.
            record(
                "semantic_faithfulness",
                True,
                f"{_SKIPPED}pre-reviewed template — no generated prose to "
                f"verify; the five deterministic checks applied in full",
            )
        elif not self.use_llm:
            record(
                "semantic_faithfulness", True, f"{_SKIPPED}LLM check disabled"
            )
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
            except Exception as exc:  # noqa: BLE001
                # Fail closed on ANYTHING, not just LLMError.
                #
                # This previously caught only LLMError, so any other exception
                # escaped verify(), unwound run_district, and was swallowed by
                # asyncio.gather(return_exceptions=True) — silently dropping the
                # entire district, including advisories that had already passed.
                # Nothing unsafe was sent, but no BLOCKED record was written and
                # nothing appeared in the dashboard's blocked tab. A failure the
                # operator cannot see is not a safe failure.
                log.exception("verifier error for %s", advisory.advisory_id)
                record(
                    "semantic_faithfulness",
                    False,
                    f"verification failed ({type(exc).__name__}), blocking by "
                    f"default: {exc}"[:300],
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
    """One-line rendering for the dashboard and logs.

    Counts only checks that actually ran. A skipped semantic check was
    previously recorded as a pass, so a template advisory reported "6/6" when
    five checks had run — overstating the guarantee by exactly the check that
    did not happen.
    """
    mark = "PASS" if result.passed else "BLOCK"
    ran = [c for c in result.checks if not c.detail.startswith(_SKIPPED)]
    passed = sum(1 for c in ran if c.passed)
    skipped = len(result.checks) - len(ran)
    tail = f" ({skipped} skipped)" if skipped else ""
    return f"[{mark}] {passed}/{len(ran)} checks{tail}" + (
        "" if result.passed else f" — {result.blocked_reasons[0]}"
    )
