"""USSD handler — the channel that reaches a feature phone with no credit.

Why USSD matters here more than any other channel
--------------------------------------------------
GSMA's 2026 Mobile Gender Gap Report: **68% of Ethiopian women own a mobile
phone; 9% use mobile internet daily.** That 59-point gap is the population most
exposed to climate shocks and least reachable by any app or dashboard. USSD
reaches them. It needs no data bundle, no smartphone, no literacy in a second
language, and it works on a handset that costs under fifteen dollars.

It is also *pull* rather than push: a person dials in when they want to know,
which sidesteps the opt-in and cost problems of broadcast entirely.

Two hard engineering constraints
--------------------------------
**1. The gateway is stateless and so are we.** Africa's Talking posts the full
input history in ``text``, joined by ``*`` — "" then "1" then "1*2" then
"1*2*3". We derive state purely from ``text.split('*')``. No session store, no
Redis, no TTL bugs, and a pod restart mid-session is invisible to the user.

**2. There is a hard three-second budget.** Airtel's connect-to-application
timeout is 10 seconds and its response timeout is 15, but real handsets and
real networks eat most of that. So **no LLM call, no forecast fetch, no
translation may happen on this path.** Every advisory served here is
pre-computed by the scheduler and read from cache. This constraint is the
reason the whole pipeline is built around generating advisories on a schedule
rather than on demand — USSD dictated the architecture, not the other way
round.

**3. Latin script only.** Ethiopic and Arabic in USSD are rendered by the
handset's dialer firmware, which on cheap feature phones frequently lacks the
font — Arabic USSD arriving blank is a documented failure mode. UCS-2 also
halves the screen budget. So Amharic and Tigrinya speakers are offered
Latin-script Oromo/Somali/English here and receive Ge'ez by SMS, where the
messaging app renders it properly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..models import Advisory, Channel, FeedbackKind, Language
from .encoding import USSD_SCREEN_CHARS, is_gsm7, normalise_for_gsm7

log = logging.getLogger("hatua.ussd")

CON = "CON"  # keep the session open, await input
END = "END"  # terminate the session

# Only Latin-script languages are offered over USSD. See module docstring.
USSD_LANGUAGES: list[tuple[Language, str]] = [
    (Language.SWAHILI, "Kiswahili"),
    (Language.SOMALI, "Af-Soomaali"),
    (Language.OROMO, "Afaan Oromoo"),
    (Language.ENGLISH, "English"),
]

MENU_LABELS: dict[Language, dict[str, str]] = {
    Language.SWAHILI: {
        "title": "HATUA Onyo la Mapema",
        "pick_area": "Chagua eneo lako:",
        "current": "Onyo la sasa",
        "feedback": "Toa taarifa",
        "no_alert": "Hakuna onyo kwa eneo lako sasa. Asante.",
        "thanks": "Asante. Taarifa yako imepokelewa.",
        "invalid": "Chaguo si sahihi.",
        "fb_prompt": "Hali ikoje kwako?",
        "fb_rain": "Mvua imenyesha",
        "fb_norain": "Hakuna mvua",
        "fb_flood": "Mafuriko yapo",
        "fb_moved": "Nimehamisha mifugo",
        "fb_help": "Nahitaji msaada",
    },
    Language.SOMALI: {
        "title": "HATUA Digniin Hore",
        "pick_area": "Dooro degmadaada:",
        "current": "Digniinta hadda",
        "feedback": "Warbixin dir",
        "no_alert": "Digniin kuma jirto degmadaada hadda. Mahadsanid.",
        "thanks": "Mahadsanid. Warbixintaada waa la helay.",
        "invalid": "Doorasho khaldan.",
        "fb_prompt": "Xaaladdu sidee tahay?",
        "fb_rain": "Roob ayaa da'ay",
        "fb_norain": "Roob ma da'in",
        "fb_flood": "Daad ayaa jira",
        "fb_moved": "Xoolaha waan guuriyay",
        "fb_help": "Caawimaad baan u baahanahay",
    },
    Language.OROMO: {
        "title": "HATUA Akeekkachiisa",
        "pick_area": "Naannoo kee filadhu:",
        "current": "Akeekkachiisa ammaa",
        "feedback": "Odeeffannoo ergi",
        "no_alert": "Naannoo keetiif akeekkachiisni hin jiru. Galatoomi.",
        "thanks": "Galatoomi. Odeeffannoon kee nu gahe.",
        "invalid": "Filannoo dogoggoraa.",
        "fb_prompt": "Haalli maal fakkaata?",
        "fb_rain": "Roobni roobeera",
        "fb_norain": "Roobni hin roobne",
        "fb_flood": "Lolaan jira",
        "fb_moved": "Horii sochoosee jira",
        "fb_help": "Gargaarsa barbaada",
    },
    Language.ENGLISH: {
        "title": "HATUA Early Warning",
        "pick_area": "Choose your area:",
        "current": "Current alert",
        "feedback": "Send a report",
        "no_alert": "No alert for your area right now. Thank you.",
        "thanks": "Thank you. Your report has been received.",
        "invalid": "Invalid choice.",
        "fb_prompt": "What is happening where you are?",
        "fb_rain": "Rain has fallen",
        "fb_norain": "No rain",
        "fb_flood": "Flooding is happening",
        "fb_moved": "I moved my livestock",
        "fb_help": "I need help",
    },
}

FEEDBACK_OPTIONS: list[tuple[str, FeedbackKind]] = [
    ("fb_rain", FeedbackKind.RAIN_RECEIVED),
    ("fb_norain", FeedbackKind.NO_RAIN),
    ("fb_flood", FeedbackKind.FLOODING_OBSERVED),
    ("fb_moved", FeedbackKind.LIVESTOCK_MOVED),
    ("fb_help", FeedbackKind.NEED_HELP),
]


@dataclass
class USSDRequest:
    """Africa's Talking posts application/x-www-form-urlencoded."""

    session_id: str
    service_code: str
    phone_number: str
    text: str

    @classmethod
    def from_form(cls, form: dict[str, str]) -> "USSDRequest":
        return cls(
            session_id=form.get("sessionId", ""),
            service_code=form.get("serviceCode", ""),
            phone_number=form.get("phoneNumber", ""),
            text=form.get("text", ""),
        )

    @property
    def steps(self) -> list[str]:
        """Input history. Empty on the first request of a session."""
        return [p for p in self.text.split("*") if p != ""]


def _fit(body: str, limit: int = USSD_SCREEN_CHARS) -> str:
    """Fit text to one USSD screen.

    Safaricom enforces 160 characters (the frequently quoted 182 is the
    octet-derived figure); Airtel allows 184. We target the stricter one. Any
    non-GSM-7 character is stripped rather than risking a blank screen on a
    handset whose dialer lacks the font.
    """
    body = normalise_for_gsm7(body)
    if not is_gsm7(body):
        body = "".join(c for c in body if is_gsm7(c))
    body = " ".join(body.split())
    if len(body) <= limit:
        return body
    cut = body[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "."


class USSDMenu:
    """Stateless USSD menu.

    ``advisory_lookup`` must be a **cache read**. If it touches the network or
    a model, the session times out and the user sees nothing.
    """

    def __init__(
        self,
        districts: list[tuple[str, str]],
        advisory_lookup: Callable[[str, Language], Advisory | None],
        feedback_sink: Callable[[str, str, FeedbackKind], None] | None = None,
    ) -> None:
        # (pcode, display name), capped at 8 so the menu fits one screen
        self.districts = districts[:8]
        self.lookup = advisory_lookup
        self.feedback_sink = feedback_sink

    def handle(self, request: USSDRequest) -> str:
        steps = request.steps

        # --- Level 0: language ---
        if not steps:
            lines = [MENU_LABELS[Language.ENGLISH]["title"]]
            lines += [
                f"{i}. {label}" for i, (_, label) in enumerate(USSD_LANGUAGES, 1)
            ]
            return f"{CON} " + "\n".join(lines)

        language = self._language(steps[0])
        if language is None:
            return f"{END} {MENU_LABELS[Language.ENGLISH]['invalid']}"
        labels = MENU_LABELS[language]

        # --- Level 1: district ---
        if len(steps) == 1:
            lines = [labels["pick_area"]]
            lines += [
                f"{i}. {name}"
                for i, (_, name) in enumerate(self.districts, 1)
            ]
            return f"{CON} " + _fit("\n".join(lines), USSD_SCREEN_CHARS)

        district = self._district(steps[1])
        if district is None:
            return f"{END} {labels['invalid']}"
        pcode, name = district

        # --- Level 2: action ---
        if len(steps) == 2:
            return (
                f"{CON} {name}\n"
                f"1. {labels['current']}\n"
                f"2. {labels['feedback']}"
            )

        choice = steps[2]

        # --- 2.1 current alert (cache read only) ---
        if choice == "1":
            advisory = self.lookup(pcode, language)
            if advisory is None or not advisory.dispatchable:
                return f"{END} {labels['no_alert']}"
            return f"{END} {_fit(advisory.body)}"

        # --- 2.2 feedback ---
        if choice == "2":
            if len(steps) == 3:
                lines = [labels["fb_prompt"]]
                lines += [
                    f"{i}. {labels[key]}"
                    for i, (key, _) in enumerate(FEEDBACK_OPTIONS, 1)
                ]
                return f"{CON} " + _fit("\n".join(lines))

            try:
                index = int(steps[3]) - 1
                _, kind = FEEDBACK_OPTIONS[index]
            except (ValueError, IndexError):
                return f"{END} {labels['invalid']}"

            if self.feedback_sink:
                try:
                    self.feedback_sink(pcode, request.phone_number, kind)
                except Exception as exc:  # noqa: BLE001
                    # Never fail the user's session because our storage broke.
                    log.error("feedback sink failed: %s", exc)
            return f"{END} {labels['thanks']}"

        return f"{END} {labels['invalid']}"

    def _language(self, token: str) -> Language | None:
        try:
            return USSD_LANGUAGES[int(token) - 1][0]
        except (ValueError, IndexError):
            return None

    def _district(self, token: str) -> tuple[str, str] | None:
        try:
            index = int(token) - 1
            if index < 0:
                return None
            return self.districts[index]
        except (ValueError, IndexError):
            return None


def sms_fallback_note(language: Language) -> str:
    """For Ge'ez and Arabic script speakers, who are offered Latin-script
    options in USSD but should receive their own script by SMS."""
    return {
        Language.AMHARIC: "የተሟላ መልእክት በኤስኤምኤስ ይላካል።",
        Language.TIGRINYA: "ምሉእ መልእኽቲ ብኤስኤምኤስ ክለኣኽ እዩ።",
        Language.ARABIC: "سيتم إرسال الرسالة الكاملة عبر الرسائل القصيرة.",
    }.get(language, "The full message will be sent by SMS.")
