"""Shared HTTP plumbing for connectors.

Design notes
------------
Every connector returns ``list[SignalReading]`` and never raises on a source
being down. A degraded source must reduce ``data_sufficiency`` rather than
crash the pipeline — in an early warning system, one dead API cannot take the
whole region offline.

Caching is on-disk and generous. Several of these sources are slow (ClimateSERV
is async) or enormous (an unfiltered FEWS NET country dump is 42 MB), and the
USSD path has a hard sub-3-second budget, so nothing may be fetched live on a
user request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..config import CACHE_DIR, HTTP_TIMEOUT, USER_AGENT

log = logging.getLogger("hatua.ingest")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SourceHealth:
    """Per-source outcome, aggregated into RiskAssessment.data_sufficiency."""

    source: str
    ok: bool
    records: int = 0
    error: str | None = None
    latency_ms: int = 0
    from_cache: bool = False


@dataclass
class IngestResult:
    """What a connector run produced, including its failures."""

    readings: list[Any] = field(default_factory=list)
    health: list[SourceHealth] = field(default_factory=list)

    @property
    def sufficiency(self) -> float:
        if not self.health:
            return 0.0
        return sum(1 for h in self.health if h.ok) / len(self.health)

    def extend(self, other: "IngestResult") -> None:
        self.readings.extend(other.readings)
        self.health.extend(other.health)


class Cache:
    """Dead simple on-disk JSON cache. No Redis to operate, no eviction policy
    to get wrong, and it survives a process restart during a demo."""

    def __init__(self, root: Path = CACHE_DIR) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        return self.root / f"{digest}.json"

    def get(self, key: str, max_age_s: int) -> Any | None:
        p = self._path(key)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > max_age_s:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, key: str, value: Any) -> None:
        try:
            self._path(key).write_text(
                json.dumps(value, default=str), encoding="utf-8"
            )
        except (OSError, TypeError) as exc:
            log.warning("cache write failed for %s: %s", key[:60], exc)


CACHE = Cache()


class Fetcher:
    """Async HTTP with retry, caching and structured health reporting."""

    def __init__(
        self,
        *,
        timeout: float = HTTP_TIMEOUT,
        retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "Fetcher":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_s: int = 1800,
    ) -> tuple[Any | None, SourceHealth]:
        return await self._get(url, params, headers, cache_s, "json")

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_s: int = 1800,
    ) -> tuple[str | None, SourceHealth]:
        return await self._get(url, params, headers, cache_s, "text")

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        cache_s: int,
        mode: str,
    ) -> tuple[Any | None, SourceHealth]:
        key = f"{mode}:{url}:{json.dumps(params or {}, sort_keys=True)}"
        label = url.split("//")[-1].split("/")[0]

        if cache_s > 0:
            hit = CACHE.get(key, cache_s)
            if hit is not None:
                return hit, SourceHealth(label, ok=True, from_cache=True)

        assert self._client is not None, "use Fetcher as an async context manager"

        start = time.monotonic()
        last_error = "unknown"
        for attempt in range(self.retries + 1):
            try:
                r = await self._client.get(url, params=params, headers=headers)
                elapsed = int((time.monotonic() - start) * 1000)

                if r.status_code >= 400:
                    last_error = f"HTTP {r.status_code}"
                    # Client errors are not worth retrying; server errors are.
                    if r.status_code < 500:
                        break
                else:
                    payload = r.json() if mode == "json" else r.text
                    if cache_s > 0:
                        CACHE.set(key, payload)
                    return payload, SourceHealth(
                        label, ok=True, latency_ms=elapsed
                    )
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:200]

            if attempt < self.retries:
                await asyncio.sleep(0.6 * (2**attempt))

        elapsed = int((time.monotonic() - start) * 1000)
        log.warning("source %s failed: %s", label, last_error)
        return None, SourceHealth(
            label, ok=False, error=last_error, latency_ms=elapsed
        )


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def normalise_linear(value: float, low: float, high: float) -> float:
    """Map a raw value onto 0-1 hazard intensity.

    ``low`` is the level at which the hazard becomes meaningful; ``high`` is
    the level at which it is unambiguously severe. Values are clamped, so an
    extreme outlier saturates at 1.0 rather than distorting a composite score.
    """
    if high == low:
        return 0.0
    return clamp01((value - low) / (high - low))


def normalise_inverse(value: float, low: float, high: float) -> float:
    """For indicators where *lower* is worse — SPI, forage biomass, NDVI."""
    return normalise_linear(-value, -high, -low)
