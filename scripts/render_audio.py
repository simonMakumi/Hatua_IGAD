"""
Pre-render advisory audio locally using Meta's MMS-TTS.

Run this on a machine with internet and a few hundred MB to spare. It writes
WAV files into data/audio/, which get committed and served statically by the
API. Nothing about this needs to run in production — audio is pre-rendered on a
schedule by design, so the deployed service never loads a TTS model at all.

    pip install torch --index-url https://download.pytorch.org/whl/cpu
    pip install transformers scipy
    python scripts/render_audio.py

Why MMS-TTS rather than a cloud provider
----------------------------------------
Meta's Massively Multilingual Speech covers **all seven of our languages**,
including **Afaan Oromo and Tigrinya — which no commercial cloud TTS supports
anywhere**. Azure covers Amharic, Somali and Swahili; Google, Amazon, OpenAI
and ElevenLabs cover fewer. MMS is the only option that reaches every language
we issue advisories in.

Each model is a ~145 MB VITS network and runs on laptop CPU in a few seconds
per message. Licence is CC-BY-NC-4.0 — fine for a hackathon, and a hard stop
if this were ever commercialised, which is worth knowing rather than
discovering later.

Quality note: MMS is trained on religious-domain recordings, so the register is
somewhat formal. For production the right answer for Oromo and Tigrinya remains
a native-speaker phrase bank — for early warning, hazard x severity x district
is a bounded set, so it is recordable in an afternoon and would be better audio
than any synthesis. MMS gets us real, correct-language audio today.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
AUDIO = DATA / "audio"
SNAPSHOT = DATA / "snapshot.json"

# MMS uses ISO 639-3 codes, which do NOT match the two-letter codes used
# everywhere else in this project. Note `gaz` for Afaan Oromo specifically — a
# naive lookup of `orm` or `om` silently fails, and this trips people up.
MMS_LANG: dict[str, str] = {
    "en": "eng",
    "sw": "swh",   # Swahili (individual language), not the macrolanguage `swa`
    "so": "som",
    "am": "amh",
    "om": "gaz",   # West Central Oromo — NOT `orm`
    "ti": "tir",
    "ar": "ara",
}


def main() -> int:
    try:
        import scipy.io.wavfile
        import torch
        from transformers import VitsModel, AutoTokenizer
    except ImportError:
        print(
            "Missing dependencies. Install with:\n"
            "  pip install torch --index-url "
            "https://download.pytorch.org/whl/cpu\n"
            "  pip install transformers scipy"
        )
        return 1

    if not SNAPSHOT.exists():
        print(f"No snapshot at {SNAPSHOT}. Run scripts/build_snapshot.py first.")
        return 1

    payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    advisories = [
        a
        for a in payload["advisories"]
        if (a.get("verification") or {}).get("status") == "passed"
    ]
    if not advisories:
        print("No verified advisories in the snapshot. Nothing to render.")
        return 1

    AUDIO.mkdir(parents=True, exist_ok=True)

    # One message per (language, district) — the SMS and Telegram variants of
    # the same advisory say the same thing, and quota is finite.
    seen: set[tuple[str, str]] = set()
    todo = []
    for a in advisories:
        key = (a["language"], a["pcode"])
        if key in seen:
            continue
        seen.add(key)
        todo.append(a)

    by_language: dict[str, list[dict]] = {}
    for a in todo:
        by_language.setdefault(a["language"], []).append(a)

    manifest: dict[str, str] = {}
    if (AUDIO / "manifest.json").exists():
        manifest = json.loads((AUDIO / "manifest.json").read_text())

    for language, items in sorted(by_language.items()):
        code = MMS_LANG.get(language)
        if not code:
            print(f"[skip] no MMS model for {language}")
            continue

        repo = f"facebook/mms-tts-{code}"
        print(f"\n=== {language} ({repo}) — {len(items)} message(s) ===")
        try:
            model = VitsModel.from_pretrained(repo)
            tokenizer = AutoTokenizer.from_pretrained(repo)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] could not load {repo}: {exc}")
            continue

        for item in items:
            body = item["body"]
            # Deterministic filename, matching hatua/delivery/voice.py so the
            # API finds these without a lookup table.
            import hashlib

            digest = hashlib.sha256(
                f"{language}:{body}".encode()
            ).hexdigest()[:20]
            out = AUDIO / f"{language}_{digest}.wav"

            if out.exists():
                print(f"  [cached] {item['district']}")
                manifest[item["id"]] = out.name
                continue

            inputs = tokenizer(body, return_tensors="pt")
            with torch.no_grad():
                waveform = model(**inputs).waveform

            scipy.io.wavfile.write(
                out,
                rate=model.config.sampling_rate,
                data=waveform.squeeze().cpu().numpy(),
            )
            manifest[item["id"]] = out.name
            seconds = waveform.shape[-1] / model.config.sampling_rate
            print(
                f"  [ok] {item['district']:16} {seconds:5.1f}s  "
                f"{out.stat().st_size // 1024:4d} KB  {out.name}"
            )

    (AUDIO / "manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(f.stat().st_size for f in AUDIO.glob("*.wav"))
    print(
        f"\n{len(manifest)} advisories rendered, "
        f"{total // 1024} KB total in {AUDIO}"
    )
    print("Commit data/audio/ and the API will serve it at /audio/<filename>.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
