"""Provider-agnostic LLM client.

Why this is an abstraction and not just an SDK call
---------------------------------------------------
HATUA's reasoning core does four narrow, structured jobs — analyse impact, plan
actions, localise, verify. None of them need a specific vendor. What they do
need is **typed JSON out, reliably**, and the freedom to move providers when
one runs out of free quota mid-hackathon or mid-deployment.

So every provider is normalised to one method: give it a JSON schema and a
prompt, get back a validated Pydantic model or an error. Switching from Gemini
to Claude is one environment variable.

There is a second, less obvious reason. A humanitarian early warning service
that hard-depends on one commercial model vendor is not a system anyone should
deploy. Provider independence is an operational requirement here, not an
engineering nicety.

Free tiers at time of writing (verify before relying on them):
    gemini      1,500 req/day, no card          aistudio.google.com/apikey
    groq        generous free tier, no card     console.groq.com/keys
    cerebras    free tier, no card              cloud.cerebras.ai
    openrouter  models suffixed ``:free``       openrouter.ai/keys
    anthropic   paid                            console.anthropic.com
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import get_settings

log = logging.getLogger("hatua.llm")

# Defence in depth. Keys now travel in headers rather than query strings, but
# httpx logs every request URL at INFO and a future provider may require a
# query parameter. Silencing this removes a whole class of credential leak into
# application and deployment logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when the model cannot produce a valid response.

    Deliberately fatal rather than silently degrading: if the Impact Analyst
    fails, we must issue no advisory at all rather than a guessed one.
    """


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    default_model: str
    env_key: str
    style: str  # "openai" | "gemini" | "anthropic"
    # Models to fall back to when the primary returns 429 or 503. Free tiers
    # meter *per model*, so an exhausted quota on one model says nothing about
    # the next: observed on 25 Jul 2026, gemini-flash-latest, gemini-2.0-flash
    # and gemini-2.0-flash-lite were all quota-exhausted while
    # gemini-flash-lite-latest answered normally on the same key, same second.
    # An early warning service must degrade to a smaller model rather than go
    # silent.
    fallbacks: tuple[str, ...] = ()


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        # Use the rolling alias rather than a pinned version. Pinned Gemini
        # model IDs get retired for new API keys without warning — a fresh key
        # issued today is refused by gemini-2.5-flash with
        # "no longer available to new users", even though that model still
        # appears in the /models listing. The alias survives that.
        default_model="gemini-flash-latest",
        env_key="GEMINI_API_KEY",
        style="gemini",
        fallbacks=(
            "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite",
            "gemini-2.0-flash-lite",
            "gemini-2.0-flash",
        ),
    ),
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        env_key="GROQ_API_KEY",
        style="openai",
        fallbacks=(
            "llama-3.1-8b-instant",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
        ),
    ),
    "cerebras": ProviderSpec(
        name="cerebras",
        base_url="https://api.cerebras.ai/v1",
        default_model="llama-3.3-70b",
        env_key="CEREBRAS_API_KEY",
        style="openai",
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="meta-llama/llama-3.3-70b-instruct:free",
        env_key="OPENROUTER_API_KEY",
        style="openai",
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-4-5",
        env_key="ANTHROPIC_API_KEY",
        style="anthropic",
    ),
}


def _retry_delay_seconds(response: httpx.Response) -> float | None:
    """Extract the provider's own advice on how long to wait after a 429.

    Google returns it inside error.details as a RetryInfo entry; most other
    providers use the standard Retry-After header.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        payload = response.json()
        error = payload.get("error", {})

        # Google: structured RetryInfo in error.details
        for entry in error.get("details", []) or []:
            delay = entry.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                return float(delay[:-1])

        # Groq: buried in prose — "Please try again in 3.01s."
        message = error.get("message", "")
        match = re.search(r"try again in ([\d.]+)s", message)
        if match:
            return float(match.group(1))
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
        pass
    return None


def _is_token_rate_limit(response: httpx.Response) -> bool:
    """Distinguish a tokens-per-minute limit from an exhausted daily quota.

    The difference decides the right response. A TPM limit clears in seconds,
    so waiting is correct and switching models is wasteful — the sibling models
    share the same organisation budget and will be limited too. A daily quota
    does not clear, so falling back to another model is the only option.

    Observed on Render: all four Groq models reported 429 within the same
    second because the chain stampeded instead of waiting three seconds.
    """
    try:
        message = response.json().get("error", {}).get("message", "").lower()
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False
    return "tokens per minute" in message or "tpm" in message


def _extract_json(text: str) -> Any:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or fences regardless of instruction, so we try
    strict parse, then fenced block, then first balanced object.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"no parseable JSON in response: {text[:300]!r}")


class LLM:
    """One narrow interface: prompt in, validated Pydantic model out."""

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        settings = get_settings()
        name = (provider or getattr(settings, "llm_provider", "gemini")).lower()
        if name not in PROVIDERS:
            raise LLMError(
                f"unknown provider {name!r}; choose one of {sorted(PROVIDERS)}"
            )
        self.spec = PROVIDERS[name]
        self.model = model or self.spec.default_model
        self.api_key = api_key or self._key_from_settings(settings)
        if not self.api_key:
            raise LLMError(
                f"{self.spec.env_key} is not set. For a free key with no credit "
                f"card, use Gemini: https://aistudio.google.com/apikey"
            )

    def _key_from_settings(self, settings: Any) -> str:
        return str(getattr(settings, self.spec.env_key.lower(), "") or "")

    # -- request building ---------------------------------------------------

    def _build(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        *,
        model: str | None = None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        model = model or self.model
        if self.spec.style == "gemini":
            # The key goes in a HEADER, never the query string. Google's docs
            # show ?key=... and it works — but httpx, uvicorn and most proxies
            # log full request URLs at INFO, so a query-string key ends up in
            # plaintext in application logs, deployment logs and any error
            # tracker. Observed happening during development on 25 Jul 2026.
            # x-goog-api-key is the documented header form and is not logged.
            url = f"{self.spec.base_url}/models/{model}:generateContent"
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": max_tokens,
                    "responseMimeType": "application/json",
                    "responseSchema": _to_gemini_schema(schema),
                    # Gemini 2.5+ spends output tokens on internal reasoning
                    # before emitting a single character of the answer. With
                    # thinking on and a 2000-token budget, every response came
                    # back as valid JSON truncated mid-string — which surfaces
                    # as a confusing parse error rather than "you ran out of
                    # room". These tasks are constrained extraction against a
                    # schema, not open-ended reasoning, so thinking buys us
                    # nothing and costs the entire budget.
                    #
                    # Note: thinkingBudget=0 is REJECTED with HTTP 400 on
                    # gemini-flash-latest. thinkingLevel="low" is accepted and
                    # measurably yields thoughtsTokenCount=0.
                    "thinkingConfig": {"thinkingLevel": "low"},
                },
            }
            return (
                url,
                {
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
                body,
            )

        if self.spec.style == "anthropic":
            return (
                f"{self.spec.base_url}/messages",
                {
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                {
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )

        # OpenAI-compatible: groq, cerebras, openrouter.
        #
        # Unlike Gemini's responseSchema, `json_object` mode only guarantees
        # *valid JSON* — not JSON matching our shape. Observed on Groq: asked
        # for {hazard, actions}, it returned the JSON Schema document itself
        # ({"hazard": {"type": "Drought"}}) rather than values, costing a retry
        # every call. So we inline the schema into the system prompt and say
        # explicitly what we do not want.
        schema_hint = (
            f"\n\nRespond with a single JSON object matching this schema. "
            f"Return the VALUES, not the schema document itself. Do not "
            f"include 'type', 'properties' or '$defs' keys.\n"
            f"{json.dumps(schema, separators=(',', ':'))}"
        )
        return (
            f"{self.spec.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            {
                "model": model,
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system + schema_hint},
                    {"role": "user", "content": user},
                ],
            },
        )

    def _text_from(self, payload: dict[str, Any]) -> str:
        if self.spec.style == "gemini":
            cands = payload.get("candidates") or []
            if not cands:
                raise LLMError(f"gemini returned no candidates: {payload}")
            # Say plainly when the response was cut off. A truncation reported
            # as "no parseable JSON" sends you hunting for a parser bug that
            # does not exist.
            reason = cands[0].get("finishReason")
            if reason == "MAX_TOKENS":
                usage = payload.get("usageMetadata", {})
                raise LLMError(
                    f"response truncated at max_tokens "
                    f"(used {usage.get('candidatesTokenCount', '?')} output, "
                    f"{usage.get('thoughtsTokenCount', 0)} on thinking). "
                    f"Raise max_tokens or shorten the schema."
                )
            if reason == "SAFETY":
                raise LLMError("response blocked by provider safety filter")
            parts = (cands[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts)
        if self.spec.style == "anthropic":
            blocks = payload.get("content") or []
            return "".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            )
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"no choices in response: {payload}")
        return choices[0].get("message", {}).get("content", "")

    # -- public API ---------------------------------------------------------

    async def structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int = 2048,
        retries: int = 5,
    ) -> T:
        """Call the model and return a validated instance of ``response_model``.

        On a validation failure the error is fed back to the model so it can
        correct itself, which in practice fixes most schema misses on the
        first retry.
        """
        schema = response_model.model_json_schema()
        prompt = user
        last: str = ""

        # Try the configured model first, then each fallback. Quota is metered
        # per model, so exhaustion on one says nothing about the next.
        model_chain = [self.model, *(m for m in self.spec.fallbacks if m != self.model)]
        model_index = 0

        async with httpx.AsyncClient(timeout=90.0) as client:
            for attempt in range(retries + 1):
                url, headers, body = self._build(
                    system, prompt, schema, max_tokens,
                    model=model_chain[model_index],
                )
                try:
                    r = await client.post(url, headers=headers, json=body)
                except httpx.HTTPError as exc:
                    last = f"transport error: {exc}"
                    log.warning("%s attempt %d: %s", self.spec.name, attempt, last)
                    continue

                if r.status_code >= 400:
                    last = f"HTTP {r.status_code}: {r.text[:300]}"
                    log.warning("%s attempt %d: %s", self.spec.name, attempt, last)
                    if r.status_code in (429, 503):
                        delay = _retry_delay_seconds(r)

                        # A tokens-per-minute limit clears in seconds and is
                        # charged against the whole organisation, so every
                        # sibling model is limited too. Wait; do not stampede.
                        if r.status_code == 429 and _is_token_rate_limit(r):
                            wait = delay or 15.0
                            log.info(
                                "%s: token-rate limited, waiting %.1fs "
                                "(switching model would not help)",
                                model_chain[model_index],
                                wait,
                            )
                            await asyncio.sleep(wait)
                            continue

                        # Otherwise it is a per-model quota, which another
                        # model may still have. A smaller model answering now
                        # beats the right model answering after the flood.
                        if model_index + 1 < len(model_chain):
                            model_index += 1
                            log.warning(
                                "%s: %s exhausted, falling back to %s",
                                self.spec.name,
                                model_chain[model_index - 1],
                                model_chain[model_index],
                            )
                            continue

                        wait = delay or 20.0
                        log.info("all models limited, sleeping %.0fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    if r.status_code in (500, 502, 529):
                        continue
                    raise LLMError(last)

                try:
                    text = self._text_from(r.json())
                    return response_model.model_validate(_extract_json(text))
                except (LLMError, ValidationError, json.JSONDecodeError) as exc:
                    last = str(exc)[:500]
                    log.warning(
                        "%s attempt %d failed validation: %s",
                        self.spec.name,
                        attempt,
                        last,
                    )
                    prompt = (
                        f"{user}\n\n---\n"
                        f"Your previous response was rejected: {last}\n"
                        f"Return ONLY valid JSON matching this schema exactly:\n"
                        f"{json.dumps(schema)}"
                    )

        raise LLMError(
            f"{self.spec.name} failed after {retries + 1} attempts across "
            f"models {model_chain}. "
            f"Last error: {last}"
        )


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic JSON schema to Gemini's responseSchema dialect.

    Gemini rejects ``$ref``/``$defs``, ``additionalProperties``, ``anyOf`` with
    null (Pydantic's optional encoding), ``const``, and format hints it does not
    recognise. We inline definitions and strip the rest.
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(n) for n in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            key = node["$ref"].rsplit("/", 1)[-1]
            return resolve(defs.get(key, {}))

        # Pydantic renders Optional[X] as anyOf[X, null]; take the non-null arm.
        if "anyOf" in node:
            arms = [a for a in node["anyOf"] if a.get("type") != "null"]
            if len(arms) == 1:
                merged = resolve(arms[0])
                if isinstance(merged, dict) and "description" in node:
                    merged.setdefault("description", node["description"])
                return merged

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in (
                "$defs", "$ref", "additionalProperties", "const", "default",
                "title", "examples", "discriminator", "exclusiveMinimum",
                "exclusiveMaximum", "anyOf", "allOf", "oneOf",
            ):
                continue
            if key == "format" and value not in ("date", "date-time", "enum"):
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {k: resolve(v) for k, v in value.items()}
            elif key in ("items", "prefixItems"):
                out[key] = resolve(value)
            else:
                out[key] = value

        # Gemini requires an explicit type on every node.
        if "type" not in out:
            if "properties" in out:
                out["type"] = "object"
            elif "enum" in out:
                out["type"] = "string"
            elif "items" in out:
                out["type"] = "array"
        return out

    return resolve({k: v for k, v in schema.items() if k != "$defs"})


def available_providers() -> dict[str, bool]:
    """Which providers are configured. Surfaced on the dashboard health panel so
    an operator can see at a glance whether reasoning is available at all."""
    settings = get_settings()
    return {
        name: bool(getattr(settings, spec.env_key.lower(), ""))
        for name, spec in PROVIDERS.items()
    }
