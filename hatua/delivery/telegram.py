"""Telegram delivery.

Not a consolation prize for lacking SMS budget. Telegram is one of very few
countries' dominant messaging platform in Ethiopia — Ethiopian news channels
like Radio Fana and Tikvah *are* a primary information channel, with audiences
in the millions. Publishing advisories there plugs into an existing trusted
distribution habit at zero marginal cost.

The architectural trick that matters
------------------------------------
Bots are rate-limited to roughly 30 messages/second in bulk, which would take
~55 minutes to reach 100,000 direct chats. But **a post to a channel is one API
call regardless of subscriber count**. So we publish per hazard zone to
channels and reserve direct messages for personalised alerts. Reach becomes
independent of audience size.

The one real constraint: a bot cannot cold-message a user — they must /start
first. Deep links solve this. ``t.me/<bot>?start=KE039`` delivers the payload
with the /start command, so a QR code on a poster, a radio callout or a
forwarded link subscribes someone directly to their own district.

Honest framing for the submission: only about 22% of Ethiopians are online, so
Telegram is one tier of a stack, not the stack. It reaches the connected; SMS,
USSD and voice reach everyone else.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import get_settings
from ..models import Advisory, Channel, DeliveryReceipt, Severity

log = logging.getLogger("hatua.telegram")

API_ROOT = "https://api.telegram.org"

# Rendered as a text prefix rather than an emoji. Severity must survive being
# read aloud by a screen reader or a volunteer over a radio, and must not
# depend on font support on a cheap handset.
SEVERITY_PREFIX: dict[Severity, str] = {
    Severity.ADVISORY: "ADVISORY",
    Severity.WATCH: "WATCH",
    Severity.WARNING: "WARNING",
    Severity.EMERGENCY: "EMERGENCY",
}


class TelegramError(RuntimeError):
    pass


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


class TelegramClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or get_settings().telegram_bot_token
        if not self.token:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather."
            )

    @property
    def _base(self) -> str:
        return f"{API_ROOT}/bot{self.token}"

    async def _call(
        self, method: str, payload: dict[str, Any], *, retries: int = 3
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(retries):
                r = await client.post(f"{self._base}/{method}", json=payload)
                if r.status_code == 429:
                    # Telegram tells us exactly how long to wait. Ignoring it
                    # is how bots get throttled harder.
                    wait = (
                        r.json().get("parameters", {}).get("retry_after", 5)
                    )
                    log.info("telegram rate limited, waiting %ss", wait)
                    await asyncio.sleep(float(wait))
                    continue
                data = r.json()
                if not data.get("ok"):
                    raise TelegramError(
                        f"{method}: {data.get('description', 'unknown error')}"
                    )
                return data["result"]
        raise TelegramError(f"{method}: rate limited after {retries} attempts")

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe", {})

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        html: bool = True,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if html:
            payload["parse_mode"] = "HTML"
        if keyboard:
            payload["reply_markup"] = keyboard
        return await self._call("sendMessage", payload)

    async def answer_callback(
        self, callback_query_id: str, text: str = ""
    ) -> dict[str, Any]:
        """Acknowledge a button press. Without this Telegram shows a spinner
        on the user's button until it times out."""
        return await self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200]},
        )

    async def set_my_commands(
        self, commands: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Populate Telegram's built-in Menu button.

        Worth doing properly: on a phone, discoverability is the whole battle.
        A user who has to remember or type a command will use one feature and
        never find the rest.
        """
        return await self._call("setMyCommands", {"commands": commands})

    async def set_webhook(self, url: str) -> dict[str, Any]:
        return await self._call(
            "setWebhook",
            {"url": url, "allowed_updates": ["message", "callback_query"]},
        )

    async def delete_webhook(self) -> dict[str, Any]:
        return await self._call("deleteWebhook", {})

    async def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        """Long polling. Avoids needing a public webhook URL during
        development, which matters because the deployment isn't up yet."""
        payload: dict[str, Any] = {"timeout": 0, "limit": 100}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload)

    def deep_link(self, username: str, pcode: str) -> str:
        """Subscription link for one district. Put this behind a QR code on a
        poster at the chief's office or the market."""
        return f"https://t.me/{username}?start={pcode}"


def render(advisory: Advisory, *, include_provenance: bool = True) -> str:
    """Format an advisory for Telegram.

    Telegram has no 160-character constraint, so unlike SMS we can afford to
    show the evidence. That matters: a county officer forwarding this to a
    colleague should be able to see what it rests on, and a warning that shows
    its working is easier to trust and easier to challenge.
    """
    prefix = SEVERITY_PREFIX[advisory.severity]
    lines = [
        f"<b>{prefix} — {_escape_html(advisory.admin_name)}, "
        f"{advisory.country_iso3}</b>",
        f"<i>{advisory.hazard.value.replace('_', ' ').title()}</i>",
        "",
        _escape_html(advisory.body),
    ]

    if advisory.valid_until:
        lines += [
            "",
            f"Valid until {advisory.valid_until.strftime('%d %b %Y')}",
        ]

    if include_provenance:
        lines += [
            "",
            f"<b>Confidence:</b> {advisory.confidence_score:.0%}",
        ]
        if advisory.verification:
            passed = sum(1 for c in advisory.verification.checks if c.passed)
            total = len(advisory.verification.checks)
            lines.append(
                f"<b>Verification:</b> {passed}/{total} checks passed"
            )
        if advisory.cited_signals:
            lines += ["", "<b>Based on:</b>"]
            for citation in advisory.cited_signals[:4]:
                lines.append(f"  • <code>{_escape_html(citation)}</code>")

    lines += [
        "",
        "<i>HATUA — from warning to action. "
        "Reply with your observation to help verify this alert.</i>",
    ]
    return "\n".join(lines)


async def send(
    advisory: Advisory,
    chat_ids: list[str | int],
    *,
    client: TelegramClient | None = None,
) -> DeliveryReceipt:
    """Publish a verified advisory to Telegram chats or channels."""
    if not advisory.dispatchable:
        reasons = (
            advisory.verification.blocked_reasons
            if advisory.verification
            else ["advisory was never verified"]
        )
        raise TelegramError(
            f"refusing to send unverified advisory {advisory.advisory_id}: "
            f"{'; '.join(reasons)}"
        )

    settings = get_settings()
    text = render(advisory)

    if settings.dry_run:
        log.info("DRY RUN telegram -> %s\n%s", chat_ids, text)
        return DeliveryReceipt(
            advisory_id=advisory.advisory_id,
            channel=Channel.TELEGRAM,
            recipients=len(chat_ids),
            delivered=len(chat_ids),
            failed=0,
            cost_estimate_kes=0.0,
            dispatched_at=datetime.now(timezone.utc),
            provider_response={"dry_run": True},
        )

    client = client or TelegramClient()
    delivered = failed = 0
    responses: dict[str, Any] = {}

    for chat_id in chat_ids:
        try:
            result = await client.send_message(chat_id, text)
            responses[str(chat_id)] = {"message_id": result.get("message_id")}
            delivered += 1
        except TelegramError as exc:
            log.warning("telegram send to %s failed: %s", chat_id, exc)
            responses[str(chat_id)] = {"error": str(exc)}
            failed += 1
        # ~1 message/second per chat is Telegram's documented safe rate.
        await asyncio.sleep(0.05)

    return DeliveryReceipt(
        advisory_id=advisory.advisory_id,
        channel=Channel.TELEGRAM,
        recipients=len(chat_ids),
        delivered=delivered,
        failed=failed,
        cost_estimate_kes=0.0,  # Telegram is free, and that is the point
        dispatched_at=datetime.now(timezone.utc),
        provider_response=responses,
    )
