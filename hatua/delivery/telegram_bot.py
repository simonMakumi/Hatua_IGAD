"""
Telegram inbound handling: subscription, alerts, and the feedback loop.

Why this matters more than it looks
-----------------------------------
A Telegram bot **cannot cold-message anyone**. The user has to /start it first.
That single constraint is what makes deep links the whole subscription
mechanism rather than a convenience:

    https://t.me/Hatua_bot?start=KE039

The payload after ``start=`` arrives with the /start command, so a QR code on a
poster at a chief's office, a link read out on local radio, or a message
forwarded between neighbours subscribes someone **directly to their own
district** with one tap and no form.

That is the realistic distribution path in this region. Nobody is going to type
a district code, and a signup page is a page nobody visits.

The other half of this module is the return path. A warning system with no way
to hear back cannot learn — it cannot tell whether the rain it forecast
actually fell, and it cannot report anything more useful than "messages sent".
Inline buttons on every advisory turn a broadcast into a measurement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..models import Advisory, FeedbackKind, Language

log = logging.getLogger("hatua.telegram_bot")

# Feedback options presented under every advisory. Deliberately short: these
# are read on a phone, and every extra option reduces the chance of any reply.
FEEDBACK_BUTTONS: list[tuple[str, FeedbackKind]] = [
    ("Rain has fallen", FeedbackKind.RAIN_RECEIVED),
    ("No rain here", FeedbackKind.NO_RAIN),
    ("Flooding is happening", FeedbackKind.FLOODING_OBSERVED),
    ("I have acted on this", FeedbackKind.ACTION_TAKEN),
    ("I need help", FeedbackKind.NEED_HELP),
]

LANGUAGE_BUTTONS: list[tuple[str, Language]] = [
    ("English", Language.ENGLISH),
    ("Kiswahili", Language.SWAHILI),
    ("Af-Soomaali", Language.SOMALI),
    ("አማርኛ", Language.AMHARIC),
    ("Afaan Oromoo", Language.OROMO),
    ("ትግርኛ", Language.TIGRINYA),
    ("العربية", Language.ARABIC),
]

# Registered with Telegram via setMyCommands, which turns the blue "Menu"
# button in the chat into a full command list. Users should not have to
# remember or type commands — on a phone, discoverability is the whole battle.
BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "menu", "description": "Everything HATUA can do"},
    {"command": "alert", "description": "Current advisory for my area"},
    {"command": "area", "description": "Change my area"},
    {"command": "language", "description": "Change language"},
    {"command": "sources", "description": "What this warning is based on"},
    {"command": "about", "description": "How HATUA works"},
    {"command": "stop", "description": "Unsubscribe"},
]

WELCOME = (
    "<b>HATUA — Early Warning</b>\n"
    "<i>From warning to action</i>\n\n"
    "You will receive early warning advisories for your area: drought, "
    "flooding, heavy rain and food security.\n\n"
    "Every advisory is checked against its source data before it is sent. "
    "If we cannot verify it, we do not send it.\n\n"
    "Tap <b>Menu</b> below, or send /menu, to see everything."
)

ABOUT = (
    "<b>How HATUA works</b>\n\n"
    "<b>1. It watches.</b> Live data from ICPAC, six national meteorological "
    "agencies, GloFAS river forecasts, GDACS and humanitarian datasets.\n\n"
    "<b>2. It combines.</b> A drought matters more where people are already "
    "hungry, displaced or in conflict. HATUA scores all of that together, not "
    "one hazard at a time.\n\n"
    "<b>3. It checks before sending.</b> Every advisory passes six checks. "
    "Every number must trace back to a real source. Every recommended action "
    "comes from published guidance, never invented. If a warning cannot be "
    "verified, it is not sent.\n\n"
    "<b>4. It listens.</b> Your reports tell us whether the warning matched "
    "what actually happened.\n\n"
    "<i>HATUA is an early warning service, not an emergency service. In "
    "immediate danger, contact local authorities directly.</i>"
)

HELP = (
    "<b>HATUA</b>\n\n"
    "/alert — current advisory for your area\n"
    "/area — change your area\n"
    "/language — change language\n"
    "/stop — unsubscribe\n\n"
    "Reply to any advisory with what you are seeing on the ground. "
    "It helps us check whether the warning was right."
)


@dataclass
class Subscriber:
    chat_id: int
    pcode: str | None = None
    language: Language = Language.ENGLISH
    subscribed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    active: bool = True
    # Never store the display name. A subscriber list for an early warning
    # service is a list of people in a hazard zone; in a region with active
    # conflict that is sensitive. chat_id alone is enough to deliver.


class SubscriberStore:
    """In-memory subscriber registry, persisted with the snapshot."""

    def __init__(self) -> None:
        self._by_chat: dict[int, Subscriber] = {}

    def upsert(
        self,
        chat_id: int,
        *,
        pcode: str | None = None,
        language: Language | None = None,
    ) -> Subscriber:
        sub = self._by_chat.get(chat_id) or Subscriber(chat_id=chat_id)
        if pcode:
            sub.pcode = pcode
        if language:
            sub.language = language
        sub.active = True
        self._by_chat[chat_id] = sub
        return sub

    def get(self, chat_id: int) -> Subscriber | None:
        return self._by_chat.get(chat_id)

    def deactivate(self, chat_id: int) -> None:
        if chat_id in self._by_chat:
            self._by_chat[chat_id].active = False

    def for_district(self, pcode: str) -> list[Subscriber]:
        return [
            s
            for s in self._by_chat.values()
            if s.active and s.pcode == pcode
        ]

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._by_chat.values() if s.active)

    def to_json(self) -> list[dict[str, Any]]:
        return [
            {
                "chat_id": s.chat_id,
                "pcode": s.pcode,
                "language": s.language.value,
                "subscribed_at": s.subscribed_at.isoformat(),
                "active": s.active,
            }
            for s in self._by_chat.values()
        ]

    def load_json(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            try:
                self._by_chat[int(row["chat_id"])] = Subscriber(
                    chat_id=int(row["chat_id"]),
                    pcode=row.get("pcode"),
                    language=Language(row.get("language", "en")),
                    subscribed_at=datetime.fromisoformat(row["subscribed_at"]),
                    active=bool(row.get("active", True)),
                )
            except (KeyError, ValueError) as exc:
                log.warning("skipping malformed subscriber row: %s", exc)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def district_keyboard(districts: list[tuple[str, str]]) -> dict[str, Any]:
    """Two per row — district names are long and phone screens are narrow."""
    rows = []
    for i in range(0, len(districts[:12]), 2):
        rows.append(
            [
                {"text": name, "callback_data": f"area:{pcode}"}
                for pcode, name in districts[i : i + 2]
            ]
        )
    return {"inline_keyboard": rows}


def language_keyboard(
    available: list[Language] | None = None,
) -> dict[str, Any]:
    """Offer only languages that actually exist for the user's district.

    Advisories are generated per country — Ethiopia gets Amharic, Afaan Oromo,
    Somali and English; Kenya gets Kiswahili, English and Somali. Offering
    Kiswahili to someone in Somali Region, Ethiopia is offering something we
    will never deliver, and the silent English fallback then reads as a bug
    rather than as a language nobody there speaks.
    """
    options = LANGUAGE_BUTTONS
    if available:
        allowed = set(available)
        options = [(label, l) for label, l in LANGUAGE_BUTTONS if l in allowed]
    if not options:
        options = [("English", Language.ENGLISH)]

    rows = []
    for i in range(0, len(options), 2):
        rows.append(
            [
                {"text": label, "callback_data": f"lang:{lang.value}"}
                for label, lang in options[i : i + 2]
            ]
        )
    return {"inline_keyboard": rows}


def menu_keyboard() -> dict[str, Any]:
    """One screen with every action, so nothing has to be typed or remembered."""
    return {
        "inline_keyboard": [
            [
                {"text": "Current advisory", "callback_data": "menu:alert"},
                {"text": "Sources", "callback_data": "menu:sources"},
            ],
            [
                {"text": "Change area", "callback_data": "menu:area"},
                {"text": "Change language", "callback_data": "menu:language"},
            ],
            [
                {"text": "Report what I am seeing", "callback_data": "menu:report"},
            ],
            [
                {"text": "How HATUA works", "callback_data": "menu:about"},
            ],
        ]
    }


def feedback_keyboard(pcode: str, advisory_id: str = "") -> dict[str, Any]:
    """Attached to every advisory. This is the return path that turns a
    broadcast into a measurement."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": f"fb:{kind.value}:{pcode}"}]
            for label, kind in FEEDBACK_BUTTONS
        ]
    }


# ---------------------------------------------------------------------------
# Update handling
# ---------------------------------------------------------------------------


@dataclass
class BotReply:
    """What to send back. Kept as data so the handler stays testable and the
    transport lives in one place."""

    chat_id: int
    text: str
    keyboard: dict[str, Any] | None = None
    answer_callback: str | None = None
    callback_query_id: str | None = None


class BotHandler:
    """Stateless update handler.

    ``districts`` and ``advisory_lookup`` are injected so this can be unit
    tested without an API, and so the lookup is always a cache read — the same
    discipline as USSD.
    """

    def __init__(
        self,
        store: SubscriberStore,
        districts: Callable[[], list[tuple[str, str]]],
        advisory_lookup: Callable[[str, Language], Advisory | None],
        record_feedback: Callable[[str, str, FeedbackKind], None],
        render_advisory: Callable[[Advisory], str],
        languages_for: Callable[[str], list[Language]] | None = None,
    ) -> None:
        self.store = store
        self.districts = districts
        self.lookup = advisory_lookup
        self.record_feedback = record_feedback
        self.render = render_advisory
        # Which languages actually have an advisory for a given district.
        self.languages_for = languages_for or (lambda _p: [])

    def _available(self, chat_id: int) -> list[Language]:
        sub = self.store.get(chat_id)
        return self.languages_for(sub.pcode) if sub and sub.pcode else []

    def handle(self, update: dict[str, Any]) -> BotReply | None:
        if "callback_query" in update:
            return self._callback(update["callback_query"])
        message = update.get("message") or update.get("edited_message")
        if message:
            return self._message(message)
        return None

    # -- messages ----------------------------------------------------------

    def _message(self, message: dict[str, Any]) -> BotReply | None:
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None:
            return None

        if text.startswith("/start"):
            return self._start(chat_id, text)
        if text.startswith(("/menu", "/help")):
            return self._menu(chat_id)
        if text.startswith("/about"):
            return BotReply(chat_id, ABOUT, menu_keyboard())
        if text.startswith("/sources"):
            return self._sources(chat_id)
        if text.startswith("/alert"):
            return self._alert(chat_id)
        if text.startswith("/area"):
            return BotReply(
                chat_id,
                "Choose your area:",
                district_keyboard(self.districts()),
            )
        if text.startswith("/language"):
            available = self._available(chat_id)
            note = (
                "Choose your language:"
                if not available
                else "Choose your language.\n<i>These are the languages "
                     "advisories are issued in for your area.</i>"
            )
            return BotReply(chat_id, note, language_keyboard(available))
        if text.startswith("/stop"):
            self.store.deactivate(chat_id)
            return BotReply(
                chat_id,
                "You have been unsubscribed. Send /start at any time to "
                "receive advisories again.",
            )
        # An unrecognised slash command is a typo or a command from a newer
        # build — never a field observation. Recording it as one corrupts the
        # feedback data, which is the only measurement we have of whether a
        # warning matched reality. Observed live: /menu on an older build was
        # logged as a ground report.
        if text.startswith("/"):
            return BotReply(
                chat_id,
                f"<i>Unknown command.</i>\n\n{HELP}",
                menu_keyboard(),
            )

        # Genuine free text is treated as a ground-truth report. People
        # describe what they are seeing rather than pressing a button, and
        # discarding that would waste the most valuable signal we get.
        sub = self.store.get(chat_id)
        if sub and sub.pcode and text:
            self.record_feedback(
                sub.pcode, str(chat_id), FeedbackKind.ACTION_TAKEN
            )
            return BotReply(
                chat_id,
                "Thank you — your report has been recorded and will help us "
                "check this warning against what is actually happening.",
                menu_keyboard(),
            )
        return BotReply(chat_id, HELP, menu_keyboard())

    def _start(self, chat_id: int, text: str) -> BotReply:
        # Deep-link payload: t.me/Hatua_bot?start=KE039
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        known = {p for p, _ in self.districts()}

        if payload and payload in known:
            sub = self.store.upsert(chat_id, pcode=payload)
            name = next(n for p, n in self.districts() if p == payload)
            log.info("subscribed %s to %s via deep link", chat_id, payload)
            return BotReply(
                chat_id,
                f"{WELCOME}\n\n<b>Your area: {name}</b>",
                feedback_keyboard(sub.pcode or ""),
            )

        self.store.upsert(chat_id)
        return BotReply(
            chat_id,
            f"{WELCOME}\n\nFirst, choose your area:",
            district_keyboard(self.districts()),
        )

    def _menu(self, chat_id: int) -> BotReply:
        """One screen showing state and every action.

        Leads with what HATUA currently thinks about *your* area, because a
        menu that opens with a list of commands answers a question nobody
        asked. The first line should be the reason you opened it.
        """
        sub = self.store.get(chat_id)
        lines = ["<b>HATUA — Early Warning</b>", "<i>From warning to action</i>", ""]

        if sub and sub.pcode:
            name = next(
                (n for p, n in self.districts() if p == sub.pcode), sub.pcode
            )
            advisory = self.lookup(sub.pcode, sub.language)
            available = self._available(chat_id)

            lines.append(f"<b>Your area:</b> {name}")
            lines.append(
                f"<b>Your language:</b> {sub.language.value.upper()}"
                + (
                    ""
                    if not available or sub.language in available
                    else "  <i>(not issued here — you receive English)</i>"
                )
            )
            if available:
                lines.append(
                    "<b>Issued here:</b> "
                    + ", ".join(l.value.upper() for l in available)
                )
            lines.append("")

            if advisory and advisory.dispatchable:
                checks = (
                    len(advisory.verification.checks)
                    if advisory.verification
                    else 0
                )
                passed = (
                    sum(1 for c in advisory.verification.checks if c.passed)
                    if advisory.verification
                    else 0
                )
                lines += [
                    f"<b>Active advisory:</b> {advisory.severity.value.upper()} "
                    f"— {advisory.hazard.value.replace('_', ' ')}",
                    f"Confidence {advisory.confidence_score:.0%} · "
                    f"{passed}/{checks} verification checks passed",
                ]
            else:
                lines.append(
                    "<b>No active advisory for your area.</b>\n"
                    "<i>That is a real answer, not a gap — we do not send a "
                    "warning unless the data supports one.</i>"
                )
        else:
            lines.append(
                "You have not chosen an area yet. Tap <b>Change area</b> below."
            )

        lines += ["", "<i>Choose an option:</i>"]
        return BotReply(chat_id, "\n".join(lines), menu_keyboard())

    def _sources(self, chat_id: int) -> BotReply:
        """Show what the current advisory rests on.

        A warning that cannot show its working is a warning you have to take on
        faith, and faith is exactly what an early warning system runs out of
        after one bad call.
        """
        sub = self.store.get(chat_id)
        if not sub or not sub.pcode:
            return BotReply(
                chat_id,
                "Choose your area first:",
                district_keyboard(self.districts()),
            )
        advisory = self.lookup(sub.pcode, sub.language)
        if not advisory or not advisory.dispatchable:
            return BotReply(
                chat_id,
                "There is no active advisory for your area, so there are no "
                "sources to show.",
                menu_keyboard(),
            )

        lines = [
            f"<b>What this advisory is based on</b>",
            f"<i>{advisory.admin_name}, {advisory.country_iso3}</i>",
            "",
        ]
        for citation in advisory.cited_signals[:8]:
            lines.append(f"• <code>{citation}</code>")
        lines += [
            "",
            f"<b>Confidence:</b> {advisory.confidence_score:.0%}",
        ]
        if advisory.verification:
            lines.append("<b>Checks performed:</b>")
            for check in advisory.verification.checks:
                mark = "PASS" if check.passed else "FAIL"
                lines.append(
                    f"  {mark} — {check.name.replace('_', ' ')}"
                )
        if advisory.action_ids:
            lines += [
                "",
                "<b>Recommended actions are drawn from published IGAD, FAO "
                "and WFP anticipatory action guidance</b>, not generated:",
            ]
            for action_id in advisory.action_ids:
                lines.append(f"  • <code>{action_id}</code>")
        return BotReply(chat_id, "\n".join(lines), menu_keyboard())

    def _alert(self, chat_id: int) -> BotReply:
        sub = self.store.get(chat_id)
        if not sub or not sub.pcode:
            return BotReply(
                chat_id,
                "Choose your area first:",
                district_keyboard(self.districts()),
            )
        advisory = self.lookup(sub.pcode, sub.language)
        if advisory is None or not advisory.dispatchable:
            return BotReply(
                chat_id,
                "There is no active advisory for your area right now.\n\n"
                "<i>That is good news — and it is also a real answer. We do "
                "not send a warning unless the data supports one.</i>",
            )
        text = self.render(advisory)
        if advisory.language != sub.language:
            # Be explicit. A silent switch reads as a bug, and quietly serving
            # a language the reader did not ask for is exactly the kind of
            # thing that erodes trust in a warning.
            text = (
                f"<i>No advisory is issued in your chosen language for "
                f"{advisory.admin_name}. Showing "
                f"{advisory.language.value.upper()} instead — send /language "
                f"to see what is available here.</i>\n\n{text}"
            )
        return BotReply(chat_id, text, feedback_keyboard(sub.pcode))

    # -- callbacks ---------------------------------------------------------

    def _callback(self, query: dict[str, Any]) -> BotReply | None:
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        data = query.get("data") or ""
        query_id = query.get("id")
        if chat_id is None:
            return None

        if data.startswith("menu:"):
            action = data.split(":", 1)[1]
            if action == "alert":
                reply = self._alert(chat_id)
            elif action == "sources":
                reply = self._sources(chat_id)
            elif action == "about":
                reply = BotReply(chat_id, ABOUT, menu_keyboard())
            elif action == "area":
                reply = BotReply(
                    chat_id,
                    "Choose your area:",
                    district_keyboard(self.districts()),
                )
            elif action == "language":
                available = self._available(chat_id)
                reply = BotReply(
                    chat_id,
                    "Choose your language.\n<i>These are the languages "
                    "advisories are issued in for your area.</i>"
                    if available
                    else "Choose your language:",
                    language_keyboard(available),
                )
            elif action == "report":
                sub = self.store.get(chat_id)
                reply = BotReply(
                    chat_id,
                    "<b>What are you seeing where you are?</b>\n\n"
                    "<i>Tap an option, or just type what you are seeing. "
                    "Your report helps us check whether this warning matched "
                    "reality.</i>",
                    feedback_keyboard(sub.pcode if sub and sub.pcode else ""),
                )
            else:
                return None
            reply.callback_query_id = query_id
            return reply

        if data.startswith("area:"):
            pcode = data.split(":", 1)[1]
            self.store.upsert(chat_id, pcode=pcode)
            name = next(
                (n for p, n in self.districts() if p == pcode), pcode
            )
            return BotReply(
                chat_id,
                f"<b>Your area is set to {name}.</b>\n\n"
                f"Send /alert for the current advisory, or /language to "
                f"change language.",
                callback_query_id=query_id,
                answer_callback=f"Area set to {name}",
            )

        if data.startswith("lang:"):
            try:
                language = Language(data.split(":", 1)[1])
            except ValueError:
                return None
            self.store.upsert(chat_id, language=language)
            available = self._available(chat_id)
            if available and language not in available:
                body = (
                    f"<b>Language saved.</b>\n\n<i>Note: advisories for your "
                    f"area are not issued in this language, so you will "
                    f"receive them in English. Send /language to see what is "
                    f"available.</i>"
                )
            else:
                body = "<b>Language set.</b> Send /alert for the current advisory."
            return BotReply(
                chat_id, body,
                callback_query_id=query_id,
                answer_callback="Language updated",
            )

        if data.startswith("fb:"):
            _, kind_raw, pcode = (data.split(":", 2) + ["", ""])[:3]
            try:
                kind = FeedbackKind(kind_raw)
            except ValueError:
                return None
            self.record_feedback(pcode, str(chat_id), kind)
            log.info("feedback %s from %s for %s", kind.value, chat_id, pcode)

            if kind is FeedbackKind.NEED_HELP:
                reply = (
                    "Your report has been recorded and flagged.\n\n"
                    "<i>HATUA is an early warning service, not an emergency "
                    "response service. If you are in immediate danger, contact "
                    "your local authorities or the nearest health facility "
                    "directly.</i>"
                )
            else:
                reply = (
                    "Thank you. Your report helps us check this warning "
                    "against what is actually happening on the ground."
                )
            return BotReply(
                chat_id,
                reply,
                callback_query_id=query_id,
                answer_callback="Report received",
            )

        return None


def deep_link(bot_username: str, pcode: str) -> str:
    """The subscription mechanism. Print this as a QR code on a poster."""
    return f"https://t.me/{bot_username}?start={pcode}"
