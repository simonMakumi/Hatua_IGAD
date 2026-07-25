"""SMS delivery, with a pluggable provider behind one interface.

Why pluggable
-------------
The economics of this project live and die on the aggregator, and aggregators
in this market differ by more than an order of magnitude for the identical
message to the identical Safaricom handset:

    HostPinnacle      KES 0.20    Mobitech     KES 0.30
    Zettatel          KES 0.40    Africa's Talking  KES 0.80
    Twilio            KES 40.40

That last row is not a typo. Global CPaaS is roughly 130x the price of Kenyan
aggregation, which is the entire reason a service like this is affordable at
national scale — and the reason the provider must be a config value rather
than something welded into the delivery path.

Every provider here goes through :func:`send`, which refuses to dispatch an
unverified advisory. That refusal is the point: ``Advisory.dispatchable`` is
False unless the Verifier passed it, and this module will not send it anyway.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import get_settings
from ..models import Advisory, Channel, DeliveryReceipt
from .encoding import estimate_cost_kes, measure, normalise_for_gsm7

log = logging.getLogger("hatua.sms")


class SMSError(RuntimeError):
    pass


@dataclass
class SendResult:
    ok: bool
    accepted: int
    rejected: int
    provider: str
    raw: dict[str, Any]
    error: str | None = None


def normalise_msisdn(number: str, default_cc: str = "254") -> str:
    """Normalise a phone number to bare international format.

    Kenyan numbers arrive in every conceivable shape — 0712..., +254712...,
    254712..., 712... — and providers disagree about whether they want the
    leading plus. We store bare international and let each provider adapt.
    """
    digits = "".join(c for c in number if c.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = default_cc + digits[1:]
    elif len(digits) <= 9:
        digits = default_cc + digits
    return digits


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class SMSProvider(ABC):
    name: str = "abstract"
    price_per_segment_kes: float = 0.0

    @abstractmethod
    async def send(self, recipients: list[str], body: str) -> SendResult: ...

    @abstractmethod
    def configured(self) -> bool: ...


class ZettatelProvider(SMSProvider):
    """Zettatel (portal.zettatel.com).

    Authenticates with the **portal login**, not a separate API key. Their docs
    also document an ``/SMSApi/apikey/*`` family, which mints an alternative
    credential — but ``userid``+``password`` works directly against
    ``/SMSApi/send`` and is what a new account has immediately.

    Verified behaviour: an unauthenticated POST returns HTTP 200 with
    ``{"status":"error","statusCode":"216","reason":"Invalid credentials"}``.
    Note the 200 — a status-code check alone would read failure as success, so
    we parse the body.
    """

    name = "zettatel"
    price_per_segment_kes = 0.40
    endpoint = "https://portal.zettatel.com/SMSApi/send"

    def __init__(self) -> None:
        s = get_settings()
        self.username = s.zettatel_username
        self.password = s.zettatel_password
        self.sender_id = s.zettatel_sender_id or s.sms_sender_id

    def configured(self) -> bool:
        return bool(self.username and self.password)

    async def send(self, recipients: list[str], body: str) -> SendResult:
        payload = {
            "userid": self.username,
            "password": self.password,
            "mobile": ",".join(normalise_msisdn(r) for r in recipients),
            "msg": body,
            "msgType": "text" if measure(body).encoding == "GSM-7" else "unicode",
            "duplicatecheck": "true",
            "output": "json",
            "sendMethod": "quick",
        }
        if self.sender_id:
            payload["senderid"] = self.sender_id

        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(
                self.endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "HATUA/0.1",
                },
            )

        try:
            data = r.json()
        except ValueError:
            return SendResult(
                False, 0, len(recipients), self.name, {"text": r.text[:400]},
                error=f"non-JSON response (HTTP {r.status_code})",
            )

        # Zettatel returns HTTP 200 on credential failure, so trust the body.
        status = str(data.get("status", "")).lower()
        if status == "success":
            return SendResult(True, len(recipients), 0, self.name, data)
        return SendResult(
            False, 0, len(recipients), self.name, data,
            error=f"{data.get('reason') or data.get('msg') or 'unknown'} "
                  f"(code {data.get('statusCode') or data.get('code')})",
        )


class MobitechProvider(SMSProvider):
    """Mobitech Technologies. 500 free messages on signup; ``h_api_key`` header.

    New accounts default to a **promotional route that only delivers between
    08:00 and 19:00**, which is a genuine constraint for an early warning
    system — a 03:00 flood alert would not go out. A registered transactional
    sender ID lifts it.
    """

    name = "mobitech"
    price_per_segment_kes = 0.30
    endpoint = "https://api.mobitechtechnologies.com/sms/sendsms"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.mobitech_api_key
        self.sender_id = s.sms_sender_id or "MOBITECH"

    def configured(self) -> bool:
        return bool(self.api_key)

    async def send(self, recipients: list[str], body: str) -> SendResult:
        payload = {
            "mobile": ",".join(normalise_msisdn(r) for r in recipients),
            "response_type": "json",
            "sender_name": self.sender_id,
            "service_id": 0,
            "message": body,
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(
                self.endpoint,
                json=payload,
                headers={"h_api_key": self.api_key, "Content-Type": "application/json"},
            )
        try:
            data = r.json()
        except ValueError:
            data = {"text": r.text[:400]}
        ok = r.status_code < 400
        return SendResult(
            ok, len(recipients) if ok else 0, 0 if ok else len(recipients),
            self.name, data if isinstance(data, dict) else {"response": data},
            error=None if ok else f"HTTP {r.status_code}",
        )


class HostPinnacleProvider(SMSProvider):
    """HostPinnacle Kenya. Cheapest published Kenyan rate at KES 0.20/segment,
    falling to KES 0.16 above one million. This is the figure quoted for scale
    economics in the submission."""

    name = "hostpinnacle"
    price_per_segment_kes = 0.20
    endpoint = "https://smsportal.hostpinnacle.co.ke/SMSApi/send"

    def __init__(self) -> None:
        s = get_settings()
        self.user_id = s.hostpinnacle_user_id
        self.api_key = s.hostpinnacle_api_key
        self.sender_id = s.sms_sender_id or "HATUA"

    def configured(self) -> bool:
        return bool(self.user_id and self.api_key)

    async def send(self, recipients: list[str], body: str) -> SendResult:
        payload = {
            "userid": self.user_id,
            "password": self.api_key,
            "mobile": ",".join(normalise_msisdn(r) for r in recipients),
            "msg": body,
            "senderid": self.sender_id,
            "msgType": "text" if measure(body).encoding == "GSM-7" else "unicode",
            "duplicatecheck": "true",
            "output": "json",
            "sendMethod": "quick",
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(self.endpoint, data=payload)
        try:
            data = r.json()
        except ValueError:
            data = {"text": r.text[:400]}
        ok = str(data.get("status", "")).lower() == "success"
        return SendResult(
            ok, len(recipients) if ok else 0, 0 if ok else len(recipients),
            self.name, data,
            error=None if ok else str(data.get("reason") or f"HTTP {r.status_code}"),
        )


class DryRunProvider(SMSProvider):
    """Logs instead of sending. The default, deliberately.

    ``DRY_RUN=true`` ships as the default setting so that an accidental run —
    a stray test, a scheduler misfire during development — cannot message a
    real person. Sending to real handsets requires an explicit decision.
    """

    name = "dry_run"
    price_per_segment_kes = 0.0

    def configured(self) -> bool:
        return True

    async def send(self, recipients: list[str], body: str) -> SendResult:
        m = measure(body)
        log.info(
            "DRY RUN -> %d recipient(s) | %s | would cost ~KES %.2f\n%s",
            len(recipients), m.describe(),
            estimate_cost_kes(body, len(recipients)), body,
        )
        return SendResult(
            True, len(recipients), 0, self.name,
            {"dry_run": True, "recipients": recipients, "measurement": m.describe()},
        )


PROVIDERS: dict[str, type[SMSProvider]] = {
    "zettatel": ZettatelProvider,
    "mobitech": MobitechProvider,
    "hostpinnacle": HostPinnacleProvider,
    "dry_run": DryRunProvider,
}


def get_provider(name: str | None = None) -> SMSProvider:
    """Resolve the configured provider, falling back to dry run.

    Falling back rather than raising is intentional: a misconfigured SMS
    provider should degrade to logging, not crash the advisory pipeline and
    take the dashboard and Telegram channel down with it.
    """
    settings = get_settings()
    if settings.dry_run:
        return DryRunProvider()

    key = (name or settings.sms_provider or "dry_run").lower()
    cls = PROVIDERS.get(key)
    if cls is None:
        log.warning("unknown SMS provider %r, using dry run", key)
        return DryRunProvider()

    provider = cls()
    if not provider.configured():
        log.warning(
            "SMS provider %r is not configured, using dry run instead", key
        )
        return DryRunProvider()
    return provider


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def send(
    advisory: Advisory, recipients: list[str], *, provider: SMSProvider | None = None
) -> DeliveryReceipt:
    """Send a verified advisory by SMS.

    Refuses anything the Verifier has not passed. This is the last of three
    places that check — the Verifier sets the status, ``Advisory.dispatchable``
    exposes it, and this function enforces it — because the cost of one
    unverified life-safety message going out is not symmetric with the cost of
    a redundant check.
    """
    if not advisory.dispatchable:
        reasons = (
            advisory.verification.blocked_reasons
            if advisory.verification
            else ["advisory was never verified"]
        )
        raise SMSError(
            f"refusing to send unverified advisory {advisory.advisory_id}: "
            f"{'; '.join(reasons)}"
        )

    if advisory.channel not in (Channel.SMS, Channel.USSD):
        raise SMSError(f"advisory {advisory.advisory_id} is not an SMS advisory")

    provider = provider or get_provider()
    body = advisory.body
    if advisory.language.is_latin_script:
        body = normalise_for_gsm7(body)

    result = await provider.send(recipients, body)
    m = measure(body)

    if not result.ok:
        log.error(
            "SMS dispatch failed via %s: %s", result.provider, result.error
        )

    return DeliveryReceipt(
        advisory_id=advisory.advisory_id,
        channel=advisory.channel,
        recipients=len(recipients),
        delivered=result.accepted,
        failed=result.rejected,
        cost_estimate_kes=round(
            m.segments * len(recipients) * provider.price_per_segment_kes, 2
        ),
        dispatched_at=datetime.now(timezone.utc),
        provider_response=result.raw,
    )


def cost_projection(body: str, recipients: int) -> dict[str, float]:
    """What this message would cost through each provider.

    Rendered on the dashboard next to every advisory. Seeing that the same
    Amharic message costs 2.5x its Swahili equivalent, and that the provider
    choice swings the bill by 4x, is what makes those two engineering decisions
    legible to the person approving the send.
    """
    segments = measure(body).segments
    return {
        cls.name: round(segments * recipients * cls.price_per_segment_kes, 2)
        for cls in PROVIDERS.values()
        if cls is not DryRunProvider
    } | {"twilio_for_comparison": round(segments * recipients * 40.40, 2)}
