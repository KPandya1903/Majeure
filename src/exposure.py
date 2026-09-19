"""Cost-of-delay engine: prices the options for one (signal, shipment) pair from its contract terms.

Pure functions, no I/O. Every number comes from the shipment's ContractTerms plus the explicit
assumptions below; the assumptions travel with the result (Exposure.assumptions) so Nemotron and
humans can see them. Rules: docs/DATA_SOURCES.md does not apply here; see README "exposure.py".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from schemas import Exposure, ExposureOption, MatchContext, Shipment, Traffic, XtractSignal

# Expected delay (days) if you wait it out, by event type and severity (low, medium, high/critical).
DELAY_DAYS = {
    "security_threat": (2, 5, 10), "labor_strike": (2, 5, 9), "chokepoint_disruption": (2, 6, 12),
    "port_closure": (1, 3, 7), "severe_weather": (1, 2, 4), "natural_disaster": (1, 4, 10),
    "vessel_incident": (1, 2, 5), "port_congestion": (1, 3, 6), "regulatory_sanctions": (0, 2, 5), "other": (0, 1, 2),
}
SEVERITY_INDEX = {"low": 0, "medium": 1, "high": 2, "critical": 2}
# Sea-route alternatives around a chokepoint: (route, extra sailing days). None = no sea alternative.
REROUTES = {
    "chokepoint:suez-canal": ("via the Cape of Good Hope", 12),
    "chokepoint:bab-el-mandeb-strait": ("via the Cape of Good Hope", 12),
    "chokepoint:red-sea": ("via the Cape of Good Hope", 12),
    "chokepoint:gulf-of-aden": ("via the Cape of Good Hope", 12),
    "chokepoint:panama-canal": ("via the Suez Canal or Cape Horn", 10),
    "chokepoint:malacca-strait": ("via the Lombok and Makassar Straits", 3),
    "chokepoint:taiwan-strait": ("east of Taiwan", 1),
    "chokepoint:dover-strait": ("north of Scotland", 2),
    "chokepoint:strait-of-hormuz": None,  # Gulf ports have no sea route that avoids Hormuz
    "chokepoint:bosporus-strait": None,   # Black Sea ports likewise
    "chokepoint:black-sea": None,
}
REROUTE_SURCHARGE_PER_CONTAINER = 1500.0  # carrier contingency surcharge for a long diversion (USD)
PORT_DIVERSION_FEE_PER_CONTAINER = 300.0  # carrier fee to discharge at an alternate port (USD)
HAULAGE_USD_PER_KM_PER_CONTAINER = 2.0    # trucking/rail from the alternate port to the original one
HAULAGE_KM_PER_DAY = 500.0
PORT_SIDE_REASONS = {"port_of_discharge", "port_of_loading", "transshipment_port"}

# What a force majeure clause must mention to cover each event type (case-insensitive).
FM_TERMS = {
    "security_threat": r"war|hostilit|terror|pira|blockade|armed conflict|military|security incident|riot|civil commotion",
    "chokepoint_disruption": r"war|hostilit|blockade|closure of (?:ports? or )?canals?|canal closure",
    "labor_strike": r"strike|labou?r|industrial action|lockout",
    "port_closure": r"closure|closed port|government[- ]ordered",
    "severe_weather": r"weather|storm|flood|natural disaster|act of god|hurricane|typhoon|cyclone",
    "natural_disaster": r"natural disaster|earthquake|tsunami|act of god|volcan|wildfire",
    "vessel_incident": r"accident|collision|fire|explosion|casualty",
    "regulatory_sanctions": r"sanction|embargo|government action|change (?:in|of) law",
    "port_congestion": None,  # congestion is practically never force majeure
    "other": None,
}
_GENERIC_FM = re.compile(r"beyond (?:its|their|the parties'?) (?:reasonable )?control|bimco|act of god", re.I)
_EXCLUSION = re.compile(r"exclud|not (?:include|cover)|does not apply|shall not (?:include|cover)|remain at", re.I)

ASSUMPTIONS = [
    "Expected delay if waiting comes from a per-event-type table (low/medium/high severity), raised to high when "
    "measured traffic at the location is down 50% or more.",
    "Demurrage and detention are charged per container per day, only for port-side matches, after the free time.",
    f"Rerouting around a chokepoint adds the listed sailing days plus a ${REROUTE_SURCHARGE_PER_CONTAINER:,.0f} per "
    "container carrier surcharge; diverting to another port adds "
    f"${PORT_DIVERSION_FEE_PER_CONTAINER:,.0f}/container plus ${HAULAGE_USD_PER_KM_PER_CONTAINER:g}/km/container haulage.",
    "Invoking force majeure excuses the late-delivery penalty but not demurrage/detention; it requires notice within "
    "the contract's notice period, counted from when PortGuard first saw the signal.",
    "These are planning estimates, not quotes.",
]


def force_majeure_covers(clause: Optional[str], event_type: str) -> Optional[bool]:
    """True/False when the clause clearly covers/excludes this event type; None when it can't be told."""
    if not clause:
        return False
    terms = FM_TERMS.get(event_type)
    if terms is None:
        return False
    pattern = re.compile(terms, re.I)
    sentences = [s for s in re.split(r"(?<=[.;])\s+", clause) if s.strip()]
    if any(_EXCLUSION.search(s) and pattern.search(s) for s in sentences):
        return False
    if any(pattern.search(s) and not _EXCLUSION.search(s) for s in sentences):
        return True
    return None if _GENERIC_FM.search(clause) else False


def expected_delay_days(signal: XtractSignal, traffic: Optional[Traffic]) -> float:
    idx = SEVERITY_INDEX.get(signal.severity or "medium", 1)
    if traffic and traffic.pct_change is not None and traffic.pct_change <= -50:
        idx = 2
    return float(DELAY_DAYS.get(signal.event_type, DELAY_DAYS["other"])[idx])


def compute_exposure(signal: XtractSignal, shipment: Shipment, match: MatchContext, traffic: Optional[Traffic] = None,
                     aware_at: Optional[datetime] = None, alternate_port: Optional[dict] = None) -> Exposure:
    """alternate_port: {"port_id", "name", "distance_km"} for port-side matches (see run_nemotron.alternate_port)."""
    terms, bl = shipment.contract_terms, shipment.bill_of_lading
    containers = bl.container_count or 1
    late_per_day = terms.late_delivery_penalty_usd_per_day or 0.0
    port_rate = ((terms.demurrage_usd_per_day or 0.0) + (terms.detention_usd_per_day or 0.0)) * containers
    free_days = terms.free_time_days or 0
    port_side = match.match_reason in PORT_SIDE_REASONS
    delay = expected_delay_days(signal, traffic)

    def port_charges(days: float) -> float:
        return max(0.0, days - free_days) * port_rate if port_side else 0.0

    options: list[ExposureOption] = []
    wait_late, wait_port = delay * late_per_day, port_charges(delay)
    options.append(ExposureOption(
        action="monitor", available=True, expected_delay_days=delay, cost_usd=round(wait_late + wait_port, 2),
        breakdown={"late_delivery_penalty": wait_late, "demurrage_detention": wait_port},
        note=f"Wait it out: ~{delay:g} days of delay ({signal.event_type}, severity {signal.severity or 'unknown'})."))

    deviation_blocked = terms.deviation_permitted is False
    if match.match_reason == "route_chokepoint":
        route = REROUTES.get(match.matched_port_id or "", "unknown")
        if route is None or route == "unknown":
            options.append(ExposureOption(
                action="reroute", available=False, expected_delay_days=delay, cost_usd=0,
                note="No sea route avoids this chokepoint." if route is None else "No known alternative route."))
        else:
            name, extra_days = route
            surcharge = REROUTE_SURCHARGE_PER_CONTAINER * containers
            options.append(ExposureOption(
                action="reroute", available=not deviation_blocked, expected_delay_days=float(extra_days),
                cost_usd=round(extra_days * late_per_day + surcharge, 2),
                breakdown={"late_delivery_penalty": extra_days * late_per_day, "carrier_surcharge": surcharge},
                note=f"Reroute {name} (+{extra_days} days)." + (" The contract does not permit deviation." if deviation_blocked else "")))
    elif alternate_port:
        km = float(alternate_port["distance_km"])
        extra_days = 1 + round(km / HAULAGE_KM_PER_DAY, 1)
        haulage = km * HAULAGE_USD_PER_KM_PER_CONTAINER * containers
        fee = PORT_DIVERSION_FEE_PER_CONTAINER * containers
        options.append(ExposureOption(
            action="reroute", available=not deviation_blocked, expected_delay_days=extra_days,
            cost_usd=round(extra_days * late_per_day + haulage + fee, 2),
            breakdown={"late_delivery_penalty": extra_days * late_per_day, "haulage": haulage, "diversion_fee": fee},
            note=f"Divert to {alternate_port['name']} ({km:.0f} km away) and haul the rest."
                 + (" The contract does not permit deviation." if deviation_blocked else "")))
    else:
        options.append(ExposureOption(action="reroute", available=False, expected_delay_days=delay, cost_usd=0,
                                      note="No alternate port found nearby."))

    covered = force_majeure_covers(terms.force_majeure_clause, signal.event_type)
    notice_due = (aware_at + timedelta(hours=terms.notice_period_hours)
                  if covered is not False and aware_at and terms.notice_period_hours else None)
    fm_note = {True: "The clause covers this kind of event.", False: "The clause does not cover this kind of event.",
               None: "The clause is too general to be sure; get a legal check."}[covered]
    if notice_due:
        fm_note += f" Written notice is due by {notice_due:%Y-%m-%d %H:%M} UTC."
    options.append(ExposureOption(
        action="invoke_force_majeure", available=covered is not False, expected_delay_days=delay,
        cost_usd=round(wait_port, 2), breakdown={"demurrage_detention": wait_port, "late_delivery_penalty": 0.0},
        note=fm_note))

    available = [o for o in options if o.available]
    cheapest = min(available, key=lambda o: o.cost_usd).action if available else None
    return Exposure(options=options, cheapest_action=cheapest, force_majeure_covered=covered,
                    force_majeure_notice_due=notice_due, assumptions=ASSUMPTIONS)
