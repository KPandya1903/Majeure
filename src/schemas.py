"""Pydantic models for PortGuard AI. Mirrors docs/SCHEMAS.md (keep them in sync)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventType(str, Enum):
    SECURITY_THREAT = "security_threat"
    LABOR_STRIKE = "labor_strike"
    CHOKEPOINT_DISRUPTION = "chokepoint_disruption"
    PORT_CLOSURE = "port_closure"
    SEVERE_WEATHER = "severe_weather"
    NATURAL_DISASTER = "natural_disaster"
    VESSEL_INCIDENT = "vessel_incident"
    PORT_CONGESTION = "port_congestion"
    REGULATORY_SANCTIONS = "regulatory_sanctions"
    OTHER = "other"


Severity = Literal["low", "medium", "high", "critical"]
Source = Literal[
    "portwatch", "gdacs",                                   # structured hazard / disruption data
    "nga_warnings", "nhc", "jtwc", "usgs",                  # official government warnings (src/fetch_official.py)
    "gcaptain", "maritime_executive", "seatrade_maritime",  # maritime trade-press RSS feeds
    "hellenic_shipping_news", "splash247", "joc", "loadstar",
    "container_news", "world_cargo_news", "marine_link", "maritime_logistics",
    "marine_insight", "bunkerspot", "sea_news",
    "freightwaves", "offshore_energy", "supply_chain_dive",  # general logistics: maritime-filtered on ingest
    "federal_register", "white_house", "ustr", "wto",       # trade policy (src/fetch_policy.py)
    "gdelt", "google_news",                                 # news search / firehose
]
Action = Literal["monitor", "notify_customer", "reroute", "expedite", "hold_cargo", "invoke_force_majeure"]
Incoterm = Literal["EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF"]


def _to_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# --------------------------------------------------------------------------- Xtract Signal


class PortLocation(_Strict):
    name: str
    kind: Literal["port", "chokepoint", "region"]
    port_id: Optional[str] = None
    country: Optional[str] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    match_method: Literal["structured", "gazetteer", "proximity", "xtract"]


class XtractSignal(_Strict):
    signal_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    source: Source
    title: str = Field(min_length=1)
    source_url: str = Field(pattern=r"^https?://")
    published_at: Optional[datetime] = None
    port_location: Optional[PortLocation]  # required key, nullable value
    related_port_ids: list[str] = Field(default_factory=list)
    event_type: EventType
    severity: Optional[Severity] = None
    raw_text: str = Field(min_length=1)
    ingested_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    _utc = field_validator("published_at", "ingested_at")(_to_utc)


# --------------------------------------------------------------------------- Nemotron payload


class PortRef(_Strict):
    port_id: str
    name: str
    country: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


class BillOfLading(_Strict):
    bl_number: str
    carrier: Optional[str] = None
    shipper: Optional[str] = None
    consignee: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_number: Optional[str] = None
    port_of_loading: PortRef
    port_of_discharge: PortRef
    transshipment_ports: list[PortRef] = Field(default_factory=list)
    route_chokepoints: list[str] = Field(default_factory=list)
    etd: Optional[datetime] = None
    eta: datetime
    cargo_description: Optional[str] = None
    container_count: Optional[int] = Field(default=None, ge=0)
    cargo_value_usd: Optional[float] = Field(default=None, ge=0)
    imo: Optional[str] = Field(default=None, pattern=r"^\d{7}$")
    # Where the vessel identity and the carrier attribution came from. The dashboard shows these
    # verbatim: a demo that mixes registry fact with generated filler has to say which is which, or
    # every figure downstream inherits the ambiguity. See src/fleet.py.
    vessel_source: Optional[Literal["public_registry", "demo"]] = None
    carrier_source: Optional[Literal["equasis", "name_prefix", "demo"]] = None

    _utc = field_validator("etd", "eta")(_to_utc)


class ContractTerms(_Strict):
    incoterm: Optional[Incoterm] = None
    force_majeure_clause: Optional[str] = None
    demurrage_usd_per_day: Optional[float] = Field(default=None, ge=0)
    detention_usd_per_day: Optional[float] = Field(default=None, ge=0)
    free_time_days: Optional[int] = Field(default=None, ge=0)
    late_delivery_penalty_usd_per_day: Optional[float] = Field(default=None, ge=0)
    deviation_permitted: Optional[bool] = None
    notice_period_hours: Optional[int] = Field(default=None, ge=0)
    governing_law: Optional[str] = None
    # "synthetic" = generated for the demo; "extracted" = parsed from a real BL by extract_contract.py.
    # Real liner contracts are confidential and unpublished, so the demo book is necessarily synthetic
    # here even where the vessel above is real.
    terms_source: Optional[Literal["synthetic", "extracted"]] = None


class Shipment(_Strict):
    shipment_id: str
    bill_of_lading: BillOfLading
    contract_terms: ContractTerms


class MatchContext(_Strict):
    match_reason: Literal["port_of_loading", "port_of_discharge", "transshipment_port", "route_chokepoint", "manual"]
    matched_port_id: Optional[str] = None
    distance_km: Optional[float] = Field(default=None, ge=0)
    days_until_eta: Optional[float] = None


class Task(_Strict):
    objective: Literal["assess_disruption_risk"] = "assess_disruption_risk"
    allowed_actions: list[Action] = Field(
        default_factory=lambda: ["monitor", "notify_customer", "reroute", "expedite", "hold_cargo", "invoke_force_majeure"],
        min_length=1,
    )
    instructions: Optional[str] = None


class ExposureOption(_Strict):
    action: Action
    available: bool
    expected_delay_days: float = Field(ge=0)
    cost_usd: float = Field(ge=0)
    breakdown: dict[str, float] = Field(default_factory=dict)
    note: str = ""


class Exposure(_Strict):
    """Rule-based cost of each option (src/exposure.py). Figures are estimates from stated assumptions."""
    currency: Literal["USD"] = "USD"
    options: list[ExposureOption]
    cheapest_action: Optional[Action] = None
    force_majeure_covered: Optional[bool] = None  # None = the clause is too vague to tell
    force_majeure_notice_due: Optional[datetime] = None
    assumptions: list[str] = Field(default_factory=list)

    _utc = field_validator("force_majeure_notice_due")(_to_utc)


class Traffic(_Strict):
    """Measured traffic at the matched port/chokepoint (IMF PortWatch daily data)."""
    location_id: str
    name: str
    metric: Literal["transits", "port_calls"]
    latest_date: str
    avg_7d: float
    baseline_avg: float
    pct_change: Optional[float] = None


class Headline(_Strict):
    title: str
    source: str
    url: str


class Corroboration(_Strict):
    """How many independent reports describe the same event (src/cluster_events.py)."""
    event_id: str
    n_reports: int = Field(ge=1)
    n_sources: int = Field(ge=1)
    n_publishers: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    headlines: list[Headline] = Field(default_factory=list)

    _utc = field_validator("first_seen", "last_seen")(_to_utc)


class Analysis(_Strict):
    exposure: Optional[Exposure] = None
    traffic: Optional[Traffic] = None
    corroboration: Optional[Corroboration] = None


class NemotronPayload(_Strict):
    payload_version: Literal["1.1"] = "1.1"
    request_id: str
    created_at: datetime
    signal: XtractSignal
    shipment: Shipment
    match_context: MatchContext
    analysis: Analysis = Field(default_factory=Analysis)
    task: Task = Field(default_factory=Task)

    _utc = field_validator("created_at")(_to_utc)


# --------------------------------------------------------------------------- Nemotron output


class NemotronDecision(BaseModel):
    """What Nemotron must return for one payload; stored in decision_logs."""

    model_config = ConfigDict(extra="ignore")  # tolerate extra keys the model adds

    risk_score: float = Field(ge=0, le=1)
    risk_level: Severity
    recommended_action: Action
    rationale: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    estimated_cost_usd: Optional[float] = Field(default=None, ge=0)
    force_majeure_applicable: Optional[bool] = None

    @field_validator("risk_score", mode="before")
    @classmethod
    def _percent_to_fraction(cls, value: Any) -> Any:
        return value / 100 if isinstance(value, (int, float)) and 1 < value <= 100 else value

    @field_validator("risk_level", "recommended_action", mode="before")
    @classmethod
    def _normalise_label(cls, value: Any) -> Any:
        return value.strip().lower().replace(" ", "_") if isinstance(value, str) else value
