"""
Voice rendering and IVR.

Why voice is not optional
-------------------------
Adult literacy is roughly 60% in Ethiopia and 54% in Somalia — and materially
lower among women: **50% of Ethiopian women and 44% of Somali women.** A
text-only early warning system therefore cannot reach about half of the adult
women in two of the countries most exposed to drought.

Those are also the people who manage household water, who take children to a
health facility, and who are least likely to be reached by a radio broadcast
aimed at a market or a chief's baraza. Audio is not an accessibility
enhancement here; it is the difference between a warning arriving and not.

Provider reality, verified
--------------------------
Neither Twilio's nor Africa's Talking's ``<Say>`` verb helps: both route to
Google Cloud TTS, which has **no GA voice** for Swahili, Amharic, Somali,
Afaan Oromo or Tigrinya. So local-language audio must be **pre-rendered** and
served to the telephony provider as an audio URL via ``<Play>``.

Azure AI Speech is the only GA provider covering Amharic, Somali and both
Swahili variants under one key, at 500,000 characters/month on the free tier:

    am-ET-MekdesNeural / am-ET-AmehaNeural
    so-SO-UbaxNeural   / so-SO-MuuseNeural
    sw-KE-ZuriNeural   / sw-KE-RafikiNeural
    sw-TZ-RehemaNeural / sw-TZ-DaudiNeural
    ar-*  (16 locales)

**No commercial cloud TTS supports Afaan Oromo or Tigrinya.** For those two we
fall back to a bounded pre-recorded phrase bank — for early warning, hazard ×
severity × district is a finite set, so recording it with native speakers is
feasible and produces better audio than synthesis would anyway.

Audio is pre-rendered on the schedule, never on the call path, for the same
reason USSD is: a caller will not wait.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from ..config import get_settings
from ..models import Advisory, Language, Severity

log = logging.getLogger("hatua.voice")

AUDIO_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "audio"

# Verified Azure neural voices. Female voices are listed first deliberately:
# the primary audience for household-level advisories in these contexts is
# women, and field experience in the region consistently reports higher
# comprehension and trust when advisories are voiced by a woman.
AZURE_VOICES: dict[Language, str] = {
    Language.AMHARIC: "am-ET-MekdesNeural",
    Language.SOMALI: "so-SO-UbaxNeural",
    Language.SWAHILI: "sw-KE-ZuriNeural",
    Language.ARABIC: "ar-EG-SalmaNeural",
    Language.ENGLISH: "en-KE-AsiliaNeural",
}

# No *commercial* cloud TTS covers these — not Azure, Google, Amazon, OpenAI or
# ElevenLabs. Meta's open-weights MMS-TTS does, via scripts/render_audio.py,
# which is why that is the primary path rather than a fallback.
NO_COMMERCIAL_VOICE: frozenset[Language] = frozenset(
    {Language.OROMO, Language.TIGRINYA}
)

# MMS-TTS (ISO 639-3) covers every language we issue advisories in. Each code
# verified against the Hub — mms-tts-gaz/-gax/-hae/-orc do not exist, Oromo is
# published under the macrolanguage code.
MMS_LANG: dict[Language, str] = {
    Language.ENGLISH: "eng",
    Language.SWAHILI: "swh",
    Language.SOMALI: "som",
    Language.AMHARIC: "amh",
    Language.OROMO: "orm",
    Language.TIGRINYA: "tir",
    Language.ARABIC: "ara",
}

# Slower than default. These are life-safety instructions delivered over a
# noisy GSM line, often to someone hearing the language spoken by a machine for
# the first time.
SPEAKING_RATE = "-15%"

# Telephony audio. 8kHz mono mu-law is what a GSM voice channel actually
# carries; rendering at 24kHz then downsampling wastes bandwidth and quota.
AZURE_OUTPUT_FORMAT = "riff-8khz-8bit-mono-mulaw"


class VoiceError(RuntimeError):
    pass


def audio_path(advisory: Advisory) -> Path:
    """Deterministic path so re-rendering an unchanged advisory is free."""
    digest = hashlib.sha256(
        f"{advisory.language.value}:{advisory.body}".encode()
    ).hexdigest()[:20]
    return AUDIO_DIR / f"{advisory.language.value}_{digest}.wav"


def build_ssml(advisory: Advisory) -> str:
    """Wrap the advisory in SSML with pacing suited to a phone call.

    Two deliberate touches: a pause after the location so the listener knows
    whether this concerns them before the instruction arrives, and a slower
    rate throughout.
    """
    voice = AZURE_VOICES[advisory.language]
    locale = voice.split("-", 2)[0] + "-" + voice.split("-")[1]

    body = escape(advisory.body)
    # A beat after the first sentence — usually the place and hazard.
    for terminator in (". ", "። ", "۔ "):
        if terminator in body:
            head, _, tail = body.partition(terminator)
            body = f"{head}{terminator.strip()}<break time='700ms'/> {tail}"
            break

    urgency = (
        "<emphasis level='strong'>"
        if advisory.severity in (Severity.WARNING, Severity.EMERGENCY)
        else ""
    )
    close = "</emphasis>" if urgency else ""

    return (
        f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        f"xml:lang='{locale}'>"
        f"<voice name='{voice}'>"
        f"<prosody rate='{SPEAKING_RATE}'>{urgency}{body}{close}</prosody>"
        f"<break time='500ms'/>"
        f"</voice></speak>"
    )


async def render(advisory: Advisory, *, force: bool = False) -> Path | None:
    """Synthesise advisory audio. Returns None if the language has no voice.

    Refuses unverified advisories, like every other delivery path.
    """
    if not advisory.dispatchable:
        raise VoiceError(
            f"refusing to voice unverified advisory {advisory.advisory_id}"
        )

    # Check for a pre-rendered file FIRST, before any language gating. MMS-TTS
    # covers Afaan Oromo and Tigrinya, so bailing out on those languages up
    # front — as an earlier version did — would discard audio for exactly the
    # two languages nothing else can produce.
    path = audio_path(advisory)
    if path.exists() and not force:
        return path

    # Beyond this point we are asking a commercial provider, which is where the
    # coverage gap actually bites.
    if advisory.language in NO_COMMERCIAL_VOICE:
        log.info(
            "no commercial TTS voice for %s and no pre-rendered file — "
            "run scripts/render_audio.py (MMS-TTS %s)",
            advisory.language.value,
            MMS_LANG.get(advisory.language, "?"),
        )
        return None
    if advisory.language not in AZURE_VOICES:
        return None

    settings = get_settings()
    if not settings.azure_speech_key:
        log.info(
            "no pre-rendered audio for %s and AZURE_SPEECH_KEY not set — "
            "run scripts/render_audio.py",
            advisory.language.value,
        )
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    endpoint = (
        f"https://{settings.azure_speech_region}.tts.speech.microsoft.com"
        f"/cognitiveservices/v1"
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            endpoint,
            headers={
                "Ocp-Apim-Subscription-Key": settings.azure_speech_key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": AZURE_OUTPUT_FORMAT,
                "User-Agent": "HATUA",
            },
            content=build_ssml(advisory).encode("utf-8"),
        )

    if response.status_code != 200:
        raise VoiceError(
            f"Azure TTS failed ({response.status_code}): {response.text[:200]}"
        )

    path.write_bytes(response.content)
    log.info(
        "rendered %s audio for %s (%d bytes)",
        advisory.language.value, advisory.pcode, len(response.content),
    )
    return path


async def render_all(advisories: list[Advisory]) -> dict[str, str]:
    """Pre-render every dispatchable advisory. Called by the scheduler.

    Never on the call path — a caller will not wait for synthesis, and the
    telephony provider will time out first anyway.
    """
    rendered: dict[str, str] = {}
    for advisory in advisories:
        if not advisory.dispatchable:
            continue
        try:
            path = await render(advisory)
            if path:
                rendered[advisory.advisory_id] = path.name
        except (VoiceError, httpx.HTTPError) as exc:
            log.warning(
                "voice render failed for %s: %s", advisory.advisory_id, exc
            )
    return rendered


# ---------------------------------------------------------------------------
# IVR
# ---------------------------------------------------------------------------


def ivr_xml(audio_url: str | None, *, fallback_text: str = "") -> str:
    """Africa's Talking voice XML.

    Uses ``<Play>`` with pre-rendered audio rather than ``<Say>``, because
    ``<Say>`` routes to Google TTS which has no GA voice for any of our target
    languages. ``<Play>`` takes any audio URL and works everywhere.
    """
    if audio_url:
        body = f"    <Play url='{escape(audio_url)}'/>"
    else:
        # English fallback only — better to say nothing in a language than to
        # say it badly.
        body = (
            f"    <Say voice='en-KE-Standard-A'>"
            f"{escape(fallback_text or 'No alert is active for your area.')}"
            f"</Say>"
        )

    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<Response>\n"
        f"{body}\n"
        "    <GetDigits timeout='10' numDigits='1' "
        "callbackUrl='/voice/feedback'>\n"
        "        <Play url='' />\n"
        "    </GetDigits>\n"
        "</Response>"
    )


def coverage() -> dict[str, str]:
    """Which languages have audio, and by what means. Surfaced on the
    dashboard so the gap is visible rather than implied."""
    out: dict[str, str] = {}
    for language in Language:
        pre_rendered = any(
            f.name.startswith(f"{language.value}_")
            for f in (AUDIO_DIR.glob("*.wav") if AUDIO_DIR.exists() else [])
        )
        if pre_rendered:
            out[language.value] = f"pre-rendered (MMS-TTS {MMS_LANG[language]})"
        elif language in AZURE_VOICES:
            out[language.value] = f"Azure neural: {AZURE_VOICES[language]}"
        elif language in NO_COMMERCIAL_VOICE:
            out[language.value] = (
                f"no commercial voice exists — MMS-TTS {MMS_LANG[language]} "
                f"via scripts/render_audio.py"
            )
        else:
            out[language.value] = "not configured"
    return out
