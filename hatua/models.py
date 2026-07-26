"""
HATUA domain models.

Every stage of the pipeline speaks in these types. The contracts between the
deterministic fusion layer and the probabilistic reasoning layer are enforced
here, which is what makes the Verifier able to do its job: it can compare an
Advisory back against the exact SignalReading objects that produced it.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class _LenientEnum(Enum):
    """Enum that accepts whatever a language model actually returns.

    Different providers stringify enums differently and none of them are
    reliable about it: Gemini returns lowercase values, Groq's Llama returns
    ``'WARNING'``, and models generally will hand back the enum *name*
    (``'HEAVY_RAIN'``) as readily as its value (``'heavy_rain'``).

    Rejecting those costs a full retry — and in the Verifier's case, a failed
    parse means the advisory is blocked, so strictness here would turn a
    cosmetic formatting difference into a silently dropped warning. We
    normalise instead. Strictness belongs on what we *send*, not on what we
    tolerate from a model.
    """

    @classmethod
    def _missing_(cls, value: object) -> "Any":
        if not isinstance(value, str):
            return None
        needle = value.strip().lower().replace(" ", "_").replace("-", "_")
        for member in cls:
            if str(member.value).lower() == needle or member.name.lower() == needle:
                return member
        return None

# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

IGAD_COUNTRIES: dict[str, str] = {
    "DJI": "Djibouti",
    "ERI": "Eritrea",
    "ETH": "Ethiopia",
    "KEN": "Kenya",
    "SOM": "Somalia",
    "SSD": "South Sudan",
    "SDN": "Sudan",
    "UGA": "Uganda",
}

# ISO-3 -> ISO-2, needed because FEWS NET FDW uses alpha-2 and HAPI uses alpha-3.
ISO3_TO_ISO2: dict[str, str] = {
    "DJI": "DJ",
    "ERI": "ER",
    "ETH": "ET",
    "KEN": "KE",
    "SOM": "SO",
    "SSD": "SS",
    "SDN": "SD",
    "UGA": "UG",
}


class AdminUnit(BaseModel):
    """An admin-2 district, keyed by its humanitarian P-code.

    P-codes are the join key across HDX HAPI, COD-AB boundaries and IPC data.
    geoBoundaries does *not* carry P-codes, so it is used for cartography only,
    never as a join layer.
    """

    pcode: str
    name: str
    admin1_pcode: str | None = None
    admin1_name: str | None = None
    country_iso3: str
    centroid_lat: float | None = None
    centroid_lon: float | None = None
    population: int | None = None
    population_year: int | None = None

    @field_validator("country_iso3")
    @classmethod
    def _known_country(cls, v: str) -> str:
        if v not in IGAD_COUNTRIES:
            raise ValueError(f"{v} is not an IGAD member state")
        return v


# ---------------------------------------------------------------------------
# Hazards and signals
# ---------------------------------------------------------------------------


class HazardType(str, _LenientEnum):
    DROUGHT = "drought"
    FLOOD = "flood"
    HEAVY_RAIN = "heavy_rain"
    HEAT_STRESS = "heat_stress"
    FOOD_INSECURITY = "food_insecurity"
    CONFLICT = "conflict"
    DISPLACEMENT = "displacement"
    DISEASE_OUTBREAK = "disease_outbreak"
    LOCUST = "locust"
    WILDFIRE = "wildfire"
    EARTHQUAKE = "earthquake"


class Severity(str, _LenientEnum):
    """Ordered severity ladder. The Verifier enforces that severity is never
    asserted above what the confidence band supports."""

    NONE = "none"
    ADVISORY = "advisory"
    WATCH = "watch"
    WARNING = "warning"
    EMERGENCY = "emergency"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.NONE: 0,
    Severity.ADVISORY: 1,
    Severity.WATCH: 2,
    Severity.WARNING: 3,
    Severity.EMERGENCY: 4,
}


class Confidence(str, _LenientEnum):
    LOW = "low"          # < 0.4
    MODERATE = "moderate"  # 0.4 - 0.65
    HIGH = "high"        # 0.65 - 0.85
    VERY_HIGH = "very_high"  # > 0.85

    @classmethod
    def from_score(cls, score: float) -> "Confidence":
        if score < 0.40:
            return cls.LOW
        if score < 0.65:
            return cls.MODERATE
        if score < 0.85:
            return cls.HIGH
        return cls.VERY_HIGH


# The confidence gate. This is the single most important safety rule in HATUA:
# a low-confidence signal can never produce an emergency-severity advisory,
# no matter what the model wants to say.
MAX_SEVERITY_BY_CONFIDENCE: dict[Confidence, Severity] = {
    Confidence.LOW: Severity.ADVISORY,
    Confidence.MODERATE: Severity.WATCH,
    Confidence.HIGH: Severity.WARNING,
    Confidence.VERY_HIGH: Severity.EMERGENCY,
}


class SignalReading(BaseModel):
    """One observation or forecast value from one source, for one place.

    This is the atomic unit of provenance. Every number that appears in a
    dispatched advisory must be traceable to one of these.
    """

    source: str                      # e.g. "icpac_hazards_watch"
    source_url: str                  # the exact endpoint queried
    dataset: str                     # e.g. "weekly_precipitation_anomaly"
    hazard: HazardType
    pcode: str | None = None
    lat: float | None = None
    lon: float | None = None
    value: float | None = None
    unit: str | None = None
    normalised: float = Field(ge=0.0, le=1.0, description="0-1 hazard intensity")
    valid_from: date | None = None
    valid_to: date | None = None
    retrieved_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)
    note: str | None = None

    @property
    def citation(self) -> str:
        window = ""
        if self.valid_from:
            window = f" valid {self.valid_from}"
            if self.valid_to and self.valid_to != self.valid_from:
                window += f"–{self.valid_to}"
        val = f"{self.value}{self.unit or ''}" if self.value is not None else "n/a"
        return f"{self.source}:{self.dataset}={val}{window}"


class CAPAlert(BaseModel):
    """An official government alert from a national met agency CAP feed.

    These carry more institutional weight than anything we compute ourselves,
    so they short-circuit the fusion layer and go straight to high severity.
    """

    identifier: str
    sender: str
    sender_oid: str | None = None    # WMO Register of Alerting Authorities OID
    country_iso3: str
    sent: datetime
    event: str
    severity_raw: str | None = None
    urgency: str | None = None
    certainty: str | None = None
    headline: str | None = None
    description: str | None = None
    areas: list[str] = Field(default_factory=list)
    effective: datetime | None = None
    expires: datetime | None = None
    link: str | None = None


# ---------------------------------------------------------------------------
# Fusion output
# ---------------------------------------------------------------------------


class Exposure(BaseModel):
    population: int | None = None
    population_year: int | None = None
    cropland_fraction: float | None = None
    rangeland_fraction: float | None = None
    livestock_index: float | None = None


class Vulnerability(BaseModel):
    """Why the same hazard is not the same crisis in two different districts."""

    ipc_phase: int | None = Field(default=None, ge=1, le=5)
    ipc_population_in_phase3plus: int | None = None
    ipc_reference_period: str | None = None
    conflict_events_90d: int | None = None
    fatalities_90d: int | None = None
    idps: int | None = None
    refugees: int | None = None
    multiplier: float = 1.0
    data_gaps: list[str] = Field(default_factory=list)


class TriggerEvent(BaseModel):
    """A threshold crossing with full provenance.

    Modelled on ICPAC's own thresholds-and-triggers framework so that our
    semantics line up with eatriggersthresholds.icpac.net rather than
    competing with it.
    """

    trigger_id: str
    pcode: str
    admin_name: str
    country_iso3: str
    hazard: HazardType
    threshold_name: str
    threshold_value: float
    observed_value: float
    lead_time_days: int
    fired_at: datetime
    window_start: date
    window_end: date
    confidence_score: float = Field(ge=0.0, le=1.0)
    signals: list[SignalReading]
    cap_alerts: list[CAPAlert] = Field(default_factory=list)

    @property
    def confidence(self) -> Confidence:
        return Confidence.from_score(self.confidence_score)

    @property
    def max_permitted_severity(self) -> Severity:
        return MAX_SEVERITY_BY_CONFIDENCE[self.confidence]


class RiskAssessment(BaseModel):
    """Compound risk for one admin-2 unit at one point in time."""

    pcode: str
    admin_name: str
    country_iso3: str
    assessed_at: datetime
    compound_risk_score: float = Field(ge=0.0, le=1.0)
    hazard_scores: dict[HazardType, float] = Field(default_factory=dict)
    exposure: Exposure
    vulnerability: Vulnerability
    confidence_score: float = Field(ge=0.0, le=1.0)
    signals: list[SignalReading] = Field(default_factory=list)
    triggers: list[TriggerEvent] = Field(default_factory=list)
    data_sufficiency: float = Field(
        ge=0.0, le=1.0,
        description="Fraction of expected sources that returned usable data. "
                    "Below 0.4 we refuse to advise rather than guess.",
    )

    @property
    def dominant_hazard(self) -> HazardType | None:
        if not self.hazard_scores:
            return None
        return max(self.hazard_scores.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# Reasoning core contracts
# ---------------------------------------------------------------------------


class ImpactHypothesis(BaseModel):
    """Output of the Impact Analyst.

    Deliberately about consequences, not meteorology. "220mm onto saturated
    soils upstream of 34,000 IDPs on a floodplain", not "220mm expected".
    """

    pcode: str
    hazard: HazardType
    summary: str = Field(max_length=3000)
    physical_mechanism: str
    exposed_groups: list[str]
    estimated_people_affected: int | None = None
    onset_window_start: date
    onset_window_end: date
    compounding_factors: list[str] = Field(default_factory=list)
    confidence_score: float = Field(ge=0.0, le=1.0)
    cited_signals: list[str] = Field(
        description="SignalReading.citation strings this hypothesis rests on"
    )
    uncertainties: list[str] = Field(default_factory=list)


class ActorType(str, _LenientEnum):
    PASTORALIST = "pastoralist"
    FARMER = "farmer"
    HOUSEHOLD = "household"
    COUNTY_OFFICER = "county_officer"
    NGO_RESPONDER = "ngo_responder"
    HEALTH_WORKER = "health_worker"


class AnticipatoryAction(BaseModel):
    """A single recommended action, drawn from the curated action library.

    The Verifier checks that action_id exists in the library. The model may
    select and contextualise actions; it may not invent them.
    """

    action_id: str
    actor: ActorType
    instruction: str
    rationale: str
    deadline_days: int = Field(ge=0, le=180)
    no_regret: bool = Field(
        description="True if this action remains beneficial even if the hazard "
                    "does not materialise. Low-confidence advisories may only "
                    "recommend no-regret actions."
    )
    reversible: bool = True


class ActionPlan(BaseModel):
    """Output of the Action Planner."""

    pcode: str
    hazard: HazardType
    severity: Severity
    actions: list[AnticipatoryAction]
    lead_time_days: int
    plan_confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Delivery contracts
# ---------------------------------------------------------------------------


class Language(str, _LenientEnum):
    ENGLISH = "en"
    SWAHILI = "sw"
    SOMALI = "so"
    AMHARIC = "am"
    OROMO = "om"
    TIGRINYA = "ti"
    ARABIC = "ar"

    @property
    def is_latin_script(self) -> bool:
        """Latin-script languages fit 160 chars per SMS segment (GSM-7).
        Ge'ez and Arabic script force UCS-2 and drop to 70."""
        return self in (
            Language.ENGLISH,
            Language.SWAHILI,
            Language.SOMALI,
            Language.OROMO,
        )


class Channel(str, _LenientEnum):
    SMS = "sms"
    USSD = "ussd"
    VOICE = "voice"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    DASHBOARD = "dashboard"

    @property
    def has_hard_length_limit(self) -> bool:
        return self in (Channel.SMS, Channel.USSD)


class VerificationStatus(str, _LenientEnum):
    PENDING = "pending"
    PASSED = "passed"
    BLOCKED = "blocked"


class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class VerificationResult(BaseModel):
    """Output of the Verifier. An Advisory with status BLOCKED is never sent."""

    status: VerificationStatus
    checks: list[VerificationCheck]
    blocked_reasons: list[str] = Field(default_factory=list)
    verified_at: datetime

    @property
    def passed(self) -> bool:
        return self.status == VerificationStatus.PASSED


class Advisory(BaseModel):
    """A rendered, channel-ready message awaiting or having passed verification."""

    advisory_id: str
    pcode: str
    admin_name: str
    country_iso3: str
    hazard: HazardType
    severity: Severity
    language: Language
    channel: Channel
    body: str
    voice_audio_url: str | None = None
    created_at: datetime
    valid_until: datetime | None = None

    # Provenance chain, carried all the way to dispatch
    trigger_ids: list[str] = Field(default_factory=list)
    cited_signals: list[str] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    confidence_score: float = Field(ge=0.0, le=1.0)

    # Encoding accounting, populated by the Localizer
    encoding: Literal["GSM-7", "UCS-2"] | None = None
    segment_count: int | None = None
    septet_count: int | None = None

    # How the text was produced. Template-rendered advisories are assembled
    # from sentences a native speaker has already reviewed, with only numerals
    # and place names substituted — so there is no free-form generation for a
    # semantic check to catch. Asking a model that cannot read Afaan Oromo to
    # adjudicate Afaan Oromo would be circular, and in testing it rejected
    # correct templates. Deterministic checks still apply in full.
    source: Literal["generated", "template"] = "generated"

    verification: VerificationResult | None = None

    @property
    def dispatchable(self) -> bool:
        return self.verification is not None and self.verification.passed


# ---------------------------------------------------------------------------
# Feedback loop
# ---------------------------------------------------------------------------


class FeedbackKind(str, _LenientEnum):
    RAIN_RECEIVED = "rain_received"
    NO_RAIN = "no_rain"
    FLOODING_OBSERVED = "flooding_observed"
    LIVESTOCK_MOVED = "livestock_moved"
    ACTION_TAKEN = "action_taken"
    NEED_HELP = "need_help"
    ALREADY_DISPLACED = "already_displaced"
    NOT_APPLICABLE = "not_applicable"


class CommunityFeedback(BaseModel):
    """Ground truth from a recipient. This is what closes the loop and gives us
    a warning-to-action funnel instead of a broadcast counter."""

    feedback_id: str
    advisory_id: str | None = None
    pcode: str
    channel: Channel
    kind: FeedbackKind
    free_text: str | None = None
    received_at: datetime
    contact_hash: str = Field(
        description="Salted hash of the MSISDN. We never store raw phone numbers."
    )


class DeliveryReceipt(BaseModel):
    advisory_id: str
    channel: Channel
    recipients: int
    delivered: int
    failed: int
    cost_estimate_kes: float | None = None
    dispatched_at: datetime
    provider_response: dict[str, Any] = Field(default_factory=dict, repr=False)
