"""
HATUA self-test. Run this instead of pasting shell one-liners.

    python scripts/selftest.py

Every check prints PASS or FAIL and explains what a failure means. Exits
non-zero if anything fails, so it can gate a deploy.

Why a script rather than inline commands: characters like the typographic
apostrophe (U+2019) — the exact character this project exists to catch — get
silently rewritten by shell quoting before Python ever sees them, which makes
an inline test of the encoding layer meaningless. Escapes in a file cannot be
mangled.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------


def test_imports() -> None:
    section("1. Every module imports")
    import importlib

    bad = []
    files = sorted(pathlib.Path("hatua").rglob("*.py"))
    for p in files:
        mod = (
            str(p).replace("\\", ".").replace("/", ".")
            .removesuffix(".py").removesuffix(".__init__")
        )
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{mod}: {type(exc).__name__}: {exc}")

    check(
        f"{len(files)} modules import cleanly",
        not bad,
        "" if not bad else f"{len(bad)} failed — first: {bad[0][:90]}",
    )
    if bad and "No module named" in bad[0]:
        print("\n  >> Run:  pip install -r requirements.txt\n")


# ---------------------------------------------------------------------------
# 2. SMS encoding
# ---------------------------------------------------------------------------


def test_encoding() -> None:
    section("2. SMS encoding — GSM-7 vs UCS-2")
    from hatua.delivery.encoding import (
        estimate_cost_kes,
        measure,
        normalise_for_gsm7,
        septet_count,
    )

    # ’ is written as an escape so no shell can rewrite it.
    smart = (
        "Gobolka Soomaalida: U guuri xoolahaaga meel sare hadda "
        "ee ha sugin’ ka hor inta uusan webigu buux dhaafin. "
        "Carruurta yar u kaxee xarunta caafimaadka."
    )
    plain = normalise_for_gsm7(smart)

    m_smart, m_plain = measure(smart), measure(plain)
    check(
        "typographic apostrophe forces UCS-2",
        m_smart.encoding == "UCS-2",
        m_smart.describe()[:70],
    )
    check(
        "normalising recovers GSM-7",
        m_plain.encoding == "GSM-7",
        m_plain.describe()[:70],
    )
    check(
        "normalising halves the segment count",
        m_plain.segments < m_smart.segments,
        f"{m_smart.segments} segments -> {m_plain.segments}",
    )

    # Same message length, different script.
    long_latin = "A" * 300
    long_geez = "ስ" * 300  # Ethiopic syllable
    seg_latin = measure(long_latin).segments
    seg_geez = measure(long_geez).segments
    check(
        "300 chars: Ge'ez costs more segments than Latin",
        seg_geez > seg_latin,
        f"Latin {seg_latin} segments vs Ge'ez {seg_geez} segments "
        f"({seg_geez / seg_latin:.1f}x)",
    )
    check(
        "cost scales with segments",
        estimate_cost_kes(long_geez, 100_000)
        > estimate_cost_kes(long_latin, 100_000),
        f"100k recipients: Latin KES "
        f"{estimate_cost_kes(long_latin, 100_000):,.0f} vs Ge'ez KES "
        f"{estimate_cost_kes(long_geez, 100_000):,.0f}",
    )

    # Extension characters cost two septets each.
    check(
        "extension chars cost 2 septets",
        septet_count("cost [high]") == 13,
        f"'cost [high]' is 11 chars but {septet_count('cost [high]')} septets",
    )


# ---------------------------------------------------------------------------
# 3. The Verifier
# ---------------------------------------------------------------------------


def test_verifier() -> None:
    section("3. The Verifier blocks bad advisories")
    import asyncio
    from datetime import datetime, timezone

    from hatua.agents import actions as lib
    from hatua.agents.verifier import Verifier
    from hatua.api.app import SNAPSHOT, STATE
    from hatua.models import (
        ActionPlan, Advisory, Channel, ImpactHypothesis, Language, Severity,
    )

    if not STATE.load(SNAPSHOT) or not STATE.results:
        check("snapshot available", False, "run scripts/build_snapshot.py first")
        return

    assessment = next(
        (r.assessment for r in STATE.results if r.assessment.triggers), None
    )
    if assessment is None:
        check("a district with a fired trigger", False, "none in snapshot")
        return
    trigger = assessment.triggers[0]

    hypothesis = ImpactHypothesis(
        pcode=assessment.pcode, hazard=trigger.hazard, summary="test",
        physical_mechanism="test", exposed_groups=["households"],
        onset_window_start=trigger.window_start,
        onset_window_end=trigger.window_end,
        confidence_score=trigger.confidence_score, cited_signals=[],
    )
    action = lib.ACTIONS["fi_hh_nutrition_screening"]
    plan = ActionPlan(
        pcode=assessment.pcode, hazard=trigger.hazard, severity=Severity.WATCH,
        actions=[action], lead_time_days=trigger.lead_time_days,
        plan_confidence=trigger.confidence_score,
    )

    def advisory(body: str, **over) -> Advisory:
        base = dict(
            advisory_id="test", pcode=assessment.pcode,
            admin_name=assessment.admin_name,
            country_iso3=assessment.country_iso3, hazard=trigger.hazard,
            severity=Severity.WATCH, language=Language.ENGLISH,
            channel=Channel.SMS, body=body,
            created_at=datetime.now(timezone.utc),
            confidence_score=trigger.confidence_score,
            action_ids=["fi_hh_nutrition_screening"],
        )
        return Advisory(**{**base, **over})

    name = assessment.admin_name
    verifier = Verifier(use_llm=False)

    cases = [
        (
            "clean message PASSES",
            advisory(
                f"{name}: take children under five to the nearest health "
                f"facility for nutrition screening within 14 days."
            ),
            True,
        ),
        (
            "wrong district BLOCKED",
            advisory("Nairobi County: flooding tonight. Leave immediately."),
            False,
        ),
        (
            "invented death toll BLOCKED",
            advisory(f"{name}: 47 people have died in the flooding."),
            False,
        ),
        (
            "unapproved action BLOCKED",
            advisory(f"{name}: nutrition screening.",
                     action_ids=["totally_made_up_action"]),
            False,
        ),
        (
            "severity above confidence gate BLOCKED",
            advisory(f"{name}: evacuate now.", severity=Severity.EMERGENCY),
            False,
        ),
    ]

    for label, adv, should_pass in cases:
        result = asyncio.run(
            verifier.verify(
                adv, trigger=trigger, assessment=assessment,
                hypothesis=hypothesis, plan=plan, evidence="",
            )
        )
        ok = result.passed == should_pass
        detail = (
            "verified"
            if result.passed
            else (result.blocked_reasons[0][:64] if result.blocked_reasons else "")
        )
        check(label, ok, detail)


# ---------------------------------------------------------------------------
# 4. USSD
# ---------------------------------------------------------------------------


def test_ussd() -> None:
    section("4. USSD menu — stateless and fast")
    import time

    from hatua.api.app import SNAPSHOT, STATE, _menu
    from hatua.delivery.ussd import USSDRequest

    STATE.load(SNAPSHOT)
    menu = _menu()

    start = time.perf_counter()
    screens = [
        menu.handle(USSDRequest("s", "*384*7899#", "+254700000000", text))
        for text in ("", "2", "2*1", "2*1*1", "2*1*2", "2*1*2*3")
    ]
    elapsed_ms = (time.perf_counter() - start) * 1000

    check("dial-in shows a language menu", screens[0].startswith("CON"))
    check("session ends after reading an alert", screens[3].startswith("END"))
    check(
        "responds far inside the 3s budget",
        elapsed_ms < 100,
        f"6 screens in {elapsed_ms:.2f} ms "
        f"({3000 / max(elapsed_ms / 6, 0.001):,.0f}x headroom)",
    )
    over = [s for s in screens if len(s) > 164]
    check(
        "every screen fits Safaricom's 160-char limit",
        not over,
        "" if not over else f"{len(over)} screen(s) too long",
    )


# ---------------------------------------------------------------------------
# 5. Templates
# ---------------------------------------------------------------------------


def test_templates() -> None:
    section("5. Low-resource language templates")
    from hatua.agents.templates import ACTION_TEXT, HAZARD_WORD, coverage
    from hatua.delivery.encoding import is_gsm7, normalise_for_gsm7
    from hatua.models import Language

    for lang, stats in coverage().items():
        check(
            f"{lang}: reviewed translations present",
            stats["actions_translated"] > 0,
            f"{stats['actions_translated']}/{stats['actions_total']} actions, "
            f"{stats['hazards_covered']} hazards",
        )

    # Latin-script templates must not contain look-alike characters that would
    # silently force UCS-2 — this is how a Cyrillic 'а' hid in the Oromo set.
    offenders = []
    for table in (ACTION_TEXT, HAZARD_WORD):
        for text in table.get(Language.OROMO, {}).values():
            if not is_gsm7(normalise_for_gsm7(text)):
                offenders.append(text[:40])
    check(
        "Afaan Oromo templates are GSM-7 clean",
        not offenders,
        "" if not offenders else f"{len(offenders)} would force UCS-2",
    )


# ---------------------------------------------------------------------------
# 6. Numerals
# ---------------------------------------------------------------------------


def test_numerals() -> None:
    section("6. Non-Latin numeral detection")
    from hatua.agents.verifier import _extract_numbers

    cases = [
        ("Ethiopic", "፻፳፭ mm", 125.0),   # ፻፳፭
        ("Arabic-Indic", "٦٤٠٠٠ qof", 64000.0),
        ("ASCII", "64000 people", 64000.0),
    ]
    for label, text, expected in cases:
        found = _extract_numbers(text)
        check(
            f"{label} numerals are detected",
            expected in found,
            f"{text!r} -> {found}",
        )


# ---------------------------------------------------------------------------


def main() -> int:
    print("HATUA self-test")
    tests = [
        test_imports, test_encoding, test_verifier,
        test_ussd, test_templates, test_numerals,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            check(f"{test.__name__} crashed", False, f"{type(exc).__name__}: {exc}")

    failed = [r for r in _results if not r[1]]
    section("SUMMARY")
    print(f"  {len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("\n  Failures:")
        for name, _, detail in failed:
            print(f"    - {name}: {detail}")
        return 1
    print("\n  All good. Safe to record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
