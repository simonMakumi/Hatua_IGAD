"""Configuration. Everything secret comes from the environment; everything
about the region is a constant that lives here so it can be reviewed."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "hatua" / "data"
CACHE_DIR = REPO_ROOT / ".cache"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Reasoning core ---
    # Provider-agnostic. A humanitarian warning service should not hard-depend
    # on one commercial model vendor, and free-tier quotas move around, so the
    # provider is a config value rather than an architectural commitment.
    llm_provider: str = "gemini"
    llm_model: str = ""  # blank means the provider's default

    gemini_api_key: str = ""
    groq_api_key: str = ""
    cerebras_api_key: str = ""
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""

    # --- Humanitarian data (required, but self-issued and free) ---
    hdx_hapi_app_identifier: str = ""

    # --- Delivery ---
    # SMS provider is pluggable. Mobitech gives 500 free messages on signup and
    # costs KES 0.30 at volume; HostPinnacle is KES 0.20. Africa's Talking is
    # KES 0.80 and its sandbox never reaches a real handset, so it is retained
    # only for USSD, where it has no free competitor in Kenya.
    sms_provider: str = "mobitech"
    mobitech_api_key: str = ""
    hostpinnacle_api_key: str = ""
    hostpinnacle_user_id: str = ""
    zettatel_api_key: str = ""
    sms_sender_id: str = ""

    at_username: str = "sandbox"
    at_api_key: str = ""
    at_ussd_service_code: str = "*384*7899#"
    demo_phone_number: str = ""

    telegram_bot_token: str = ""

    # --- Localisation ---
    google_translate_api_key: str = ""
    azure_speech_key: str = ""
    azure_speech_region: str = "eastus"

    # --- Optional enrichment ---
    firms_map_key: str = ""
    reliefweb_appname: str = ""

    # --- Infrastructure ---
    database_url: str = "sqlite+aiosqlite:///./hatua.db"
    public_base_url: str = "http://localhost:8000"
    contact_salt: str = "hatua-dev-salt-change-me"

    # --- Behaviour ---
    dry_run: bool = Field(
        default=True,
        description="When true, dispatch is simulated and logged but nothing "
                    "is actually sent. Defaults to true so an accidental run "
                    "never messages a real person.",
    )
    min_data_sufficiency: float = Field(
        default=0.40,
        description="Below this fraction of expected sources we refuse to "
                    "issue an advisory rather than guess.",
    )

    @property
    def llm_api_key(self) -> str:
        return str(getattr(self, f"{self.llm_provider}_api_key", "") or "")

    @property
    def has_reasoning(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def has_hapi(self) -> bool:
        return bool(self.hdx_hapi_app_identifier)

    def missing_required(self) -> list[str]:
        missing = []
        if not self.llm_api_key:
            missing.append(f"{self.llm_provider.upper()}_API_KEY")
        if not self.hdx_hapi_app_identifier:
            missing.append("HDX_HAPI_APP_IDENTIFIER")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Regional constants
# ---------------------------------------------------------------------------

# Capitals, used for the multi-coordinate Open-Meteo call that covers all eight
# countries in a single HTTP request.
IGAD_CAPITALS: dict[str, tuple[float, float]] = {
    "KEN": (-1.286, 36.817),   # Nairobi
    "ETH": (9.030, 38.740),    # Addis Ababa
    "SOM": (2.046, 45.318),    # Mogadishu
    "SDN": (15.500, 32.560),   # Khartoum
    "SSD": (4.859, 31.571),    # Juba
    "UGA": (0.347, 32.582),    # Kampala
    "DJI": (11.588, 43.145),   # Djibouti City
    "ERI": (15.322, 38.925),   # Asmara
}

# National met agency CAP feeds, verified reachable 2026-07-25.
# Six of eight share an identical URL path, so one parser handles all of them.
CAP_FEEDS: dict[str, str] = {
    "KEN": "https://meteo.go.ke/api/cap/rss.xml",
    "ETH": "https://www.ethiomet.gov.et/api/cap/rss.xml",
    "SOM": "https://meteosomalia.so/api/cap/rss.xml",
    "SDN": "https://meteosudan.sd/api/cap/rss.xml",
    "SSD": "https://meteosouthsudan.com.ss/api/cap/rss.xml",
    "DJI": "https://meteodjibouti.dj/api/cap/rss.xml",
    # Uganda's feed is registered but stale (latest item 2023). Kept for
    # completeness; the connector flags staleness rather than trusting it.
    "UGA": "https://cap-sources.s3.amazonaws.com/ug-unma-en/rss.xml",
    # Eritrea has no CAP feed registered with WMO.
}

# WMO Register of Alerting Authorities OIDs, used to authenticate CAP senders.
WMO_ALERTING_OIDS: dict[str, str] = {
    "KEN": "2.49.0.0.404.0",
    "ETH": "2.49.0.0.231.0",
    "SOM": "2.49.0.0.706.0",
    "SDN": "2.49.0.0.729.0",
    "SSD": "2.49.0.0.728.0",
    "DJI": "2.49.0.0.262.0",
    "UGA": "2.49.0.0.800.0",
}

# Base URLs. All verified live 2026-07-25.
ICPAC_HAZARDS_WATCH = "https://eahazardswatch.icpac.net"
ICPAC_DROUGHT_WATCH = "https://droughtwatch.icpac.net"
ICPAC_TRIGGERS = "https://eatriggersthresholds.icpac.net"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_FLOOD = "https://flood-api.open-meteo.com/v1/flood"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
GDACS_API = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
GDACS_GEOMETRY = "https://www.gdacs.org/gdacsapi/api/polygons/getgeometry"
HAPI_BASE = "https://hapi.humdata.org/api/v2"
FDW_BASE = "https://fdw.fews.net/api"
CLIMATESERV_BASE = "https://climateserv.servirglobal.net/api"
WHO_DON = "https://www.who.int/api/news/diseaseoutbreaknews"
USGS_QUAKES = "https://earthquake.usgs.gov/fdsnws/event/1/query"
GEOBOUNDARIES = "https://www.geoboundaries.org/api/current/gbOpen"

# Bounding box for the Greater Horn of Africa, matching ICPAC's own pg_tileserv
# bounds: [21.84, -11.75, 51.42, 23.15]
GHA_BBOX = (21.84, -11.75, 51.42, 23.15)

HTTP_TIMEOUT = float(os.getenv("HATUA_HTTP_TIMEOUT", "30"))
USER_AGENT = "HATUA/0.1 (IGAD Hackathon 2026; early warning research)"
