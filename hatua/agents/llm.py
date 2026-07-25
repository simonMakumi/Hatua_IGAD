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

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import get_settings

log = logging.getLogger("hatua.llm")

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


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-2.5-flash",
        env_key="GEMINI_API_KEY",
        style="gemini",
    ),
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        env_key="GROQ_API_KEY",
        style="openai",
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
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        if self.spec.style == "gemini":
            url = (
                f"{self.spec.base_url}/models/{self.model}:generateContent"
                f"?key={self.api_key}"
            )
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": max_tokens,
                    "responseMimeType": "application/json",
                    "responseSchema": _to_gemini_schema(schema),
                },
            }
            return url, {"Content-Type": "application/json"}, body

        if self.spec.style == "anthropic":
            return (
                f"{self.spec.base_url}/messages",
                {
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )

        # OpenAI-compatible: groq, cerebras, openrouter
        return (
            f"{self.spec.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            {
                "model": self.model,
                "temperature": 0.2,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )

    def _text_from(self, payload: dict[str, Any]) -> str:
        if self.spec.style == "gemini":
            cands = payload.get("candidates") or []
            if not cands:
                raise LLMError(f"gemini returned no candidates: {payload}")
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
        retries: int = 2,
    ) -> T:
        """Call the model and return a validated instance of ``response_model``.

        On a validation failure the error is fed back to the model so it can
        correct itself, which in practice fixes most schema misses on the
        first retry.
        """
        schema = response_model.model_json_schema()
        prompt = user
        last: str = ""

        async with httpx.AsyncClient(timeout=90.0) as client:
            for attempt in range(retries + 1):
                url, headers, body = self._build(
                    system, prompt, schema, max_tokens
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
                    # Rate limits and server errors are worth retrying.
                    if r.status_code in (429, 500, 502, 503, 529):
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
            f"{self.spec.name}/{self.model} failed after {retries + 1} attempts. "
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
