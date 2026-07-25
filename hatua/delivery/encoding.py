"""
GSM-7 / UCS-2 septet accounting for SMS.

Why this module exists
----------------------
Almost every SMS integration counts characters with ``len(text)``. That is
wrong in three separate ways, and each one costs money or truncates a
life-safety message:

1. **Extension characters cost two septets.** ``^ { } \\ [ ~ ] | €`` are encoded
   as an escape plus a character. A 160-character message containing three
   square brackets is 163 septets and silently becomes two segments.

2. **The typographic apostrophe is not in GSM-7.** ``'`` (U+2019) — which any
   word processor, CMS or language model will happily produce in place of
   ``'`` (U+0027) — forces the entire message to UCS-2, dropping capacity from
   160 characters to 70 and more than doubling the cost. Twilio ships "Smart
   Encoding" to substitute these automatically. **Africa's Talking does not.**
   We do it ourselves.

3. **Ge'ez and Arabic script can never be GSM-7.** There is no national
   language shift table for Ethiopic or Arabic in 3GPP TS 23.038 — the shift
   tables cover Turkish, Spanish, Portuguese and several Indic scripts, and
   that is all. So Amharic and Tigrinya are permanently capped at 70
   characters per segment, 67 when concatenated.

The practical consequence, which drives the whole Localizer design:

    A 300-character flood advisory is 2 segments in Swahili and 5 in Amharic.
    Identical content, 2.5x the cost.

Hence the rule enforced in ``fits_budget``: **write the Amharic template first
and back-translate.** Design for Swahili first and you will produce a message
that cannot be delivered affordably in Ethiopia.

References: 3GPP TS 23.038 (alphabets), 3GPP TS 23.040 (SMS, UDH concatenation).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# GSM 03.38 basic character set (128 septets)
# ---------------------------------------------------------------------------

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)

# Characters reached via the ESC (0x1B) prefix. Each costs TWO septets.
GSM7_EXTENDED = "^{}\\[~]|€"

GSM7_BASIC_SET = set(GSM7_BASIC)
GSM7_EXTENDED_SET = set(GSM7_EXTENDED)

# Segment capacities. The 6-octet UDH used for 8-bit concatenation reference
# consumes 7 septets of the first segment's payload, giving 153. With a 16-bit
# reference the UDH is 7 octets and capacity drops to 152 / 66. Most
# aggregators default to 8-bit; Africa's Talking does not document which, so
# `conservative=True` assumes the smaller figure.
GSM7_SINGLE = 160
GSM7_CONCAT = 153
GSM7_CONCAT_CONSERVATIVE = 152
UCS2_SINGLE = 70
UCS2_CONCAT = 67
UCS2_CONCAT_CONSERVATIVE = 66

Encoding = Literal["GSM-7", "UCS-2"]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# Substitutions that keep a message in GSM-7 without changing its meaning.
# Every one of these is a character a model or word processor emits by default
# and which would otherwise silently halve our capacity.
_SUBSTITUTIONS: dict[str, str] = {
    "‘": "'",   # left single quote
    "’": "'",   # right single quote  <- the expensive one
    "‚": "'",
    "‛": "'",
    "′": "'",   # prime
    "ʼ": "'",   # modifier letter apostrophe (used in Oromo/Somali text!)
    "“": '"',   # left double quote
    "”": '"',
    "„": '"',
    "″": '"',
    "–": "-",   # en dash
    "—": "-",   # em dash
    "−": "-",   # minus sign
    "…": "...",  # ellipsis
    " ": " ",   # non-breaking space
    " ": " ",   # narrow no-break space
    "​": "",    # zero-width space
    "‌": "",
    "‍": "",
    "﻿": "",    # BOM
    "•": "-",   # bullet
    "·": "-",
    "\r\n": "\n",    # CRLF costs two septets; LF costs one
    "\r": "\n",
    "‹": "<",
    "›": ">",
    "«": '"',
    "»": '"',
    "⁄": "/",
    "×": "x",
}


def normalise_for_gsm7(text: str) -> str:
    """Replace look-alike characters that would needlessly force UCS-2.

    This is deliberately conservative: it only substitutes characters whose
    replacement is semantically identical. It will never transliterate Ge'ez or
    Arabic script, because a life-safety warning must not be silently rewritten
    into a script the recipient did not choose.
    """
    for bad, good in _SUBSTITUTIONS.items():
        if bad in text:
            text = text.replace(bad, good)

    # Decompose accented Latin characters that GSM-7 lacks but whose base
    # letter it has (e.g. "ā" -> "a"). Leaves non-Latin scripts untouched.
    out: list[str] = []
    for ch in text:
        if ch in GSM7_BASIC_SET or ch in GSM7_EXTENDED_SET:
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFD", ch)
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        if stripped and all(c in GSM7_BASIC_SET for c in stripped):
            out.append(stripped)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def is_gsm7(text: str) -> bool:
    return all(c in GSM7_BASIC_SET or c in GSM7_EXTENDED_SET for c in text)


def septet_count(text: str) -> int:
    """Length in septets, counting extension characters as two.

    This is the number that actually determines cost. ``len(text)`` is not.
    """
    total = 0
    for ch in text:
        if ch in GSM7_EXTENDED_SET:
            total += 2
        elif ch in GSM7_BASIC_SET:
            total += 1
        else:
            return -1  # not representable in GSM-7
    return total


def non_gsm7_characters(text: str) -> list[str]:
    """Which characters are forcing UCS-2. Used in error messages so a template
    author can see exactly what to fix."""
    seen: dict[str, None] = {}
    for ch in text:
        if ch not in GSM7_BASIC_SET and ch not in GSM7_EXTENDED_SET:
            seen.setdefault(ch, None)
    return list(seen)


@dataclass(frozen=True)
class SmsMeasurement:
    text: str
    encoding: Encoding
    units: int            # septets for GSM-7, UTF-16 code units for UCS-2
    segments: int
    capacity_this_size: int
    offending_characters: list[str]

    @property
    def is_single_segment(self) -> bool:
        return self.segments == 1

    def describe(self) -> str:
        base = (f"{self.encoding}, {self.units} units, {self.segments} segment"
                f"{'s' if self.segments != 1 else ''}")
        if self.offending_characters:
            chars = " ".join(
                f"{c!r}(U+{ord(c):04X})" for c in self.offending_characters[:6]
            )
            base += f" — forced to UCS-2 by: {chars}"
        return base


def measure(text: str, *, conservative: bool = True) -> SmsMeasurement:
    """Measure a message exactly as a carrier would bill it."""
    if is_gsm7(text):
        units = septet_count(text)
        if units <= GSM7_SINGLE:
            return SmsMeasurement(text, "GSM-7", units, 1, GSM7_SINGLE, [])
        per = GSM7_CONCAT_CONSERVATIVE if conservative else GSM7_CONCAT
        segments = -(-units // per)  # ceiling division
        return SmsMeasurement(text, "GSM-7", units, segments, per, [])

    # UCS-2. Note this counts UTF-16 code units, so an astral-plane character
    # (an emoji) costs two, not one.
    units = sum(2 if ord(c) > 0xFFFF else 1 for c in text)
    offenders = non_gsm7_characters(text)
    if units <= UCS2_SINGLE:
        return SmsMeasurement(text, "UCS-2", units, 1, UCS2_SINGLE, offenders)
    per = UCS2_CONCAT_CONSERVATIVE if conservative else UCS2_CONCAT
    segments = -(-units // per)
    return SmsMeasurement(text, "UCS-2", units, segments, per, offenders)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

# Per-language segment ceilings. Ge'ez and Arabic script are capped at 2
# segments (134 characters) because at 5 segments an Amharic advisory costs
# 2.5x its Swahili equivalent for identical content, and at national scale that
# is the difference between a programme that is funded and one that is not.
MAX_SEGMENTS: dict[str, int] = {
    "en": 2, "sw": 2, "so": 2, "om": 2,   # Latin script: 306 chars at 2 segments
    "am": 2, "ti": 2, "ar": 2,            # UCS-2: only 134 chars at 2 segments
}

# USSD is stricter still. Safaricom enforces 160 characters per screen (the
# frequently quoted 182 is the octet-derived figure); Airtel allows 184.
USSD_SCREEN_CHARS = 160


def max_characters(language: str, segments: int = 2) -> int:
    """How many characters a template author actually gets for a language."""
    latin = language in ("en", "sw", "so", "om")
    if segments == 1:
        return GSM7_SINGLE if latin else UCS2_SINGLE
    per = GSM7_CONCAT_CONSERVATIVE if latin else UCS2_CONCAT_CONSERVATIVE
    return per * segments


def fits_budget(text: str, language: str) -> tuple[bool, SmsMeasurement, str]:
    """Check a rendered advisory against its language budget.

    Returns (ok, measurement, human-readable explanation).
    """
    limit = MAX_SEGMENTS.get(language, 2)
    m = measure(text)

    if m.segments <= limit:
        return True, m, f"OK — {m.describe()} (limit {limit})"

    # If UCS-2 was forced by substitutable punctuation rather than by script,
    # say so explicitly: that is a fixable authoring bug, not a language cost.
    if m.encoding == "UCS-2" and language in ("en", "sw", "so", "om"):
        fixed = normalise_for_gsm7(text)
        if is_gsm7(fixed):
            return False, m, (
                f"BLOCKED — {m.describe()}. This is a {language} message in "
                f"Latin script that should be GSM-7. Run normalise_for_gsm7() "
                f"and it drops to {measure(fixed).describe()}."
            )

    allowed = max_characters(language, limit)
    return False, m, (
        f"BLOCKED — {m.describe()}, exceeds the {limit}-segment limit for "
        f"'{language}' ({allowed} characters). Shorten the message."
    )


def truncate_to_segments(text: str, language: str, segments: int = 2) -> str:
    """Trim to fit, cutting at a word boundary. Used only as a last resort and
    always logged — a truncated warning is a degraded warning."""
    limit_chars = max_characters(language, segments)
    if len(text) <= limit_chars:
        return text
    cut = text[:limit_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "."


def estimate_cost_kes(text: str, recipients: int) -> float:
    """Africa's Talking charges roughly KES 0.80 per segment per recipient in
    Kenya. Segments, not messages — which is exactly why the Ge'ez penalty
    matters at scale."""
    return measure(text).segments * recipients * 0.80
