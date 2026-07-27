"""
One command to get everything ready for recording.

    python scripts/prepare_demo.py

Runs the pipeline on live data, renders audio, then checks the result is
actually good enough to film — and tells you plainly if it is not.

The last part matters more than it sounds. A snapshot can complete successfully
and still make a bad demo: one district, everything passing (so the Verifier
looks decorative), or only English (so the whole last-mile argument goes
unshown). Those are the things this script checks for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data" / "snapshot.json"

DISTRICTS = int(sys.argv[1]) if len(sys.argv) > 1 else 6


def rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------------------


async def build_snapshot() -> None:
    rule(f"1/3  Building snapshot — {DISTRICTS} districts")
    print(
        "This is slow on purpose. It waits out Groq's per-minute token limits\n"
        "instead of failing. Expect several minutes. Do not interrupt it.\n"
    )
    from hatua.api.app import STATE, refresh

    started = time.time()
    await refresh(top_n=DISTRICTS)
    elapsed = time.time() - started

    if STATE.last_error:
        print(f"\n  Refresh reported: {STATE.last_error}")

    print(
        f"\n  Done in {elapsed / 60:.1f} min — "
        f"{len(STATE.results)} districts, {len(STATE.all_advisories)} advisories"
    )


def render_audio() -> None:
    rule("2/3  Rendering advisory audio")
    try:
        import torch  # noqa: F401
    except ImportError:
        print(
            "  Skipped — torch is not installed.\n"
            "  Audio is optional. To add it:\n"
            "    pip install torch --index-url "
            "https://download.pytorch.org/whl/cpu\n"
            "    pip install transformers scipy uroman"
        )
        return

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "render_audio.py")],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("\n  Audio rendering failed. Not fatal — carry on without it.")


def review() -> int:
    """Judge the snapshot as demo material, not just as valid data."""
    rule("3/3  Is this good enough to record?")

    if not SNAPSHOT.exists():
        print("  No snapshot. Something went wrong above.")
        return 1

    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assessments = data["assessments"]
    advisories = data["advisories"]
    passed = [
        a for a in advisories
        if (a.get("verification") or {}).get("status") == "passed"
    ]
    blocked = [a for a in advisories if a not in passed]
    languages = sorted({a["language"] for a in passed})
    countries = sorted({a["country_iso3"] for a in assessments})

    print("\n  RISK RANKING")
    ordered = sorted(
        assessments, key=lambda a: -a["compound_risk_score"]
    )
    for a in ordered:
        ipc = (a.get("vulnerability") or {}).get("ipc_phase")
        print(
            f"    {a['admin_name']:18} {a['country_iso3']}  "
            f"risk={a['compound_risk_score']:.3f}  "
            f"conf={a['confidence_score']:.2f}  "
            f"IPC={ipc if ipc else 'no data':>7}  "
            f"{len(a.get('triggers') or [])} trigger(s)"
        )

    print(
        f"\n  {len(assessments)} districts · {len(countries)} countries · "
        f"{len(passed)} verified · {len(blocked)} blocked · "
        f"{len(languages)} languages {languages}"
    )

    # --- the checks that decide whether this films well ---
    print("\n  DEMO READINESS")
    problems: list[str] = []

    def verdict(ok: bool, good: str, bad: str) -> None:
        print(f"    [{'OK  ' if ok else 'WEAK'}] {good if ok else bad}")
        if not ok:
            problems.append(bad)

    verdict(
        len(assessments) >= 4,
        f"{len(assessments)} districts — enough to show a ranking",
        f"only {len(assessments)} district(s) — the ranking argument needs 4+",
    )
    verdict(
        len(blocked) >= 1,
        f"{len(blocked)} blocked advisory(s) — the Verifier has something to show",
        "nothing blocked — the Verifier will look decorative on camera",
    )
    verdict(
        len(languages) >= 3,
        f"{len(languages)} languages — the last-mile argument lands",
        f"only {len(languages)} language(s) — the multilingual point won't show",
    )
    verdict(
        len(countries) >= 3,
        f"{len(countries)} countries — regional scope is visible",
        f"only {len(countries)} country(s) — looks narrower than it is",
    )

    spread = (
        ordered[0]["compound_risk_score"] - ordered[-1]["compound_risk_score"]
        if len(ordered) > 1 else 0
    )
    verdict(
        spread > 0.15,
        f"risk spread {spread:.2f} — the ranking is meaningful",
        f"risk spread only {spread:.2f} — districts look interchangeable",
    )

    # The single best moment in the demo: a district whose hazard is moderate
    # but whose vulnerability pushes it to the top.
    # The snapshot is a raw model dump using `threshold_name`; `name` is what
    # the /api/districts endpoint renames it to. Reading a snapshot with API
    # field names is a mistake I have now made twice, so accept both.
    def trigger_name(t: dict) -> str:
        return t.get("threshold_name") or t.get("name") or ""

    compound = [
        a for a in assessments
        if any(
            trigger_name(t) == "compound_crisis"
            for t in (a.get("triggers") or [])
        )
    ]
    verdict(
        bool(compound),
        f"compound_crisis fired in {compound[0]['admin_name']} — "
        f"this is your strongest single moment",
        "no compound_crisis trigger — the core argument has no example",
    )

    if problems:
        print(
            f"\n  {len(problems)} weak point(s). Usually fixed by re-running "
            f"with more districts:\n    python scripts/prepare_demo.py 8"
        )
        return 1

    print("\n  Good to record.")
    return 0


def main() -> int:
    print(f"HATUA — preparing demo material ({DISTRICTS} districts)")
    asyncio.run(build_snapshot())
    render_audio()
    code = review()

    rule("NEXT")
    print(
        "  1. Commit:\n"
        "       git add -A && git commit -m 'Demo snapshot' && git push\n\n"
        "  2. Warm the service (free tier sleeps after 15 min):\n"
        "       curl -s https://hatua.onrender.com/health\n\n"
        "  3. Follow DEMO-VIDEO-SCRIPT.md\n"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
