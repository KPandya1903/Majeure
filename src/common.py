"""Shared helpers for the PortGuard AI ingestion scripts.

Paths, HTTP with retry/backoff, text + URL cleaning, the rule-based event classifier and the
port/chokepoint gazetteer. Rules are documented in docs/DATA_SOURCES.md ("Shared enrichment").
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np
import pandas as pd
import requests

from schemas import EventType, PortLocation, XtractSignal

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PORTS_CSV = DATA_DIR / "portwatch_ports.csv"
USER_AGENT = "PortGuardAI/0.1 (maritime disruption research)"  # some WAFs (GDACS) block "python-requests"

log = logging.getLogger("portguard")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """UTC ISO-8601 with a Z suffix, the timestamp format used everywhere."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


# --------------------------------------------------------------------------- HTTP

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retry_after(resp: requests.Response) -> Optional[float]:
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


def http_get(
    url: str,
    *,
    params: Optional[dict] = None,
    timeout: float = 30.0,
    retries: int = 4,
    backoff: float = 2.0,
) -> requests.Response:
    """GET with exponential backoff on connection errors, timeouts, 429 and 5xx."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT})
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == retries:
                raise
            wait = backoff * 2 ** (attempt - 1)
            log.warning("%s on %s (attempt %d/%d); retrying in %.0fs", type(exc).__name__, url, attempt, retries, wait)
            time.sleep(wait)
            continue
        if resp.status_code in RETRYABLE_STATUS and attempt < retries:
            wait = _retry_after(resp) or backoff * 2 ** (attempt - 1)
            log.warning("HTTP %d from %s (attempt %d/%d); retrying in %.0fs", resp.status_code, url, attempt, retries, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- text + URLs

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TRACKING_PARAM_RE = re.compile(r"^(?:utm_\w+|fbclid|gclid|mc_cid|mc_eid)$", re.I)


def clean_text(value: object) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = html.unescape(_TAG_RE.sub(" ", str(value)))
    return _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()


def as_sentence(text: str) -> str:
    return text if not text or text.endswith((".", "!", "?")) else text + "."


def canonical_url(url: str) -> str:
    """Citation-safe URL: lowercase scheme/host, no fragment, no tracking params."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAM_RE.match(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))


def url_key(url: str) -> str:
    """De-duplication key: canonical URL without scheme, 'www.' or trailing slash."""
    parts = urlsplit(canonical_url(url))
    key = parts.netloc.removeprefix("www.") + parts.path.rstrip("/")
    return f"{key}?{parts.query}" if parts.query else key


def make_signal_id(url: str) -> str:
    return hashlib.sha1(url_key(url).encode("utf-8")).hexdigest()[:16]


def write_signals(path: Path, signals: list[XtractSignal]) -> None:
    """Write signals as a JSON array, atomically (readers never see a half-written file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [s.model_dump(mode="json") for s in signals]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


# --------------------------------------------------------------------------- chokepoints


class Chokepoint(NamedTuple):
    name: str
    pattern: str  # case-insensitive regex used for text matching
    lat: float
    lon: float
    portwatch_id: Optional[str] = None  # id used in PortWatch `affectedports`

    @property
    def location_id(self) -> str:
        return "chokepoint:" + re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")

    def location(self, match_method: str) -> PortLocation:
        return PortLocation(name=self.name, kind="chokepoint", port_id=self.location_id,
                            lat=self.lat, lon=self.lon, match_method=match_method)


# The 28 PortWatch chokepoints (names/coordinates from PortWatch_chokepoints_database) plus three
# sea areas that news reports name instead of a strait. Order = match priority (specific first).
CHOKEPOINTS: tuple[Chokepoint, ...] = (
    Chokepoint("Suez Canal", r"suez", 30.5933, 32.4369, "chokepoint1"),
    Chokepoint("Panama Canal", r"panama canal", 9.1205, -79.7672, "chokepoint2"),
    Chokepoint("Bosporus Strait", r"bosp?h?orus|turkish straits|dardanelles", 41.1693, 29.0915, "chokepoint3"),
    Chokepoint("Bab el-Mandeb Strait", r"bab[\s-]*(?:el|al)[\s-]*mand[ae]b", 12.7886, 43.3495, "chokepoint4"),
    Chokepoint("Malacca Strait", r"malacca", 1.5170, 102.6651, "chokepoint5"),
    Chokepoint("Strait of Hormuz", r"hormuz", 26.2969, 56.8598, "chokepoint6"),
    Chokepoint("Cape of Good Hope", r"cape of good hope", -34.9273, 20.8827, "chokepoint7"),
    Chokepoint("Gibraltar Strait", r"strait of gibraltar|gibraltar strait", 35.9423, -5.7549, "chokepoint8"),
    Chokepoint("Dover Strait", r"dover strait|strait of dover|english channel", 51.0302, 1.5058, "chokepoint9"),
    Chokepoint("Oresund Strait", r"[oø]re ?sund", 55.5078, 12.8508, "chokepoint10"),
    Chokepoint("Taiwan Strait", r"taiwan strait", 24.7235, 119.8314, "chokepoint11"),
    Chokepoint("Korea Strait", r"korea strait|tsushima strait", 34.1308, 129.2092, "chokepoint12"),
    Chokepoint("Tsugaru Strait", r"tsugaru strait", 41.3280, 140.3533, "chokepoint13"),
    Chokepoint("Luzon Strait", r"luzon strait", 20.4889, 121.3523, "chokepoint14"),
    Chokepoint("Lombok Strait", r"lombok strait", -8.4191, 115.8014, "chokepoint15"),
    Chokepoint("Ombai Strait", r"ombai strait", -8.3985, 125.0910, "chokepoint16"),
    Chokepoint("Bohai Strait", r"bohai strait", 38.3730, 120.9000, "chokepoint17"),
    Chokepoint("Torres Strait", r"torres strait", -9.8625, 142.2475, "chokepoint18"),
    Chokepoint("Sunda Strait", r"sunda strait", -5.9668, 105.7752, "chokepoint19"),
    Chokepoint("Makassar Strait", r"makassar strait", 0.3523, 119.2571, "chokepoint20"),
    Chokepoint("Magellan Strait", r"strait of magellan|magellan strait", -52.6403, -69.5948, "chokepoint21"),
    Chokepoint("Yucatan Channel", r"yucat[aá]n channel", 21.8153, -85.6473, "chokepoint22"),
    Chokepoint("Windward Passage", r"windward passage", 19.9862, -73.6975, "chokepoint23"),
    Chokepoint("Mona Passage", r"mona passage", 18.4487, -67.7114, "chokepoint24"),
    Chokepoint("Balabac Strait", r"balabac strait", 7.4136, 117.1146, "chokepoint25"),
    Chokepoint("Bering Strait", r"bering strait", 65.9665, -165.5498, "chokepoint26"),
    Chokepoint("Mindoro Strait", r"mindoro strait", 12.4683, 120.4034, "chokepoint27"),
    Chokepoint("Kerch Strait", r"kerch strait", 45.2668, 36.5439, "chokepoint28"),
    Chokepoint("Red Sea", r"red sea", 20.0, 38.5),
    Chokepoint("Gulf of Aden", r"gulf of aden", 12.5, 47.5),
    Chokepoint("Black Sea", r"black sea", 43.4, 34.0),
)
CHOKEPOINTS_BY_PORTWATCH_ID = {c.portwatch_id: c for c in CHOKEPOINTS if c.portwatch_id}
# News often names a sea instead of its strait: a Red Sea report concerns ships using Bab el-Mandeb/Suez.
# init_db.py adds these as extra (non-primary) signal_ports so matching and clustering see the link.
SEA_AREA_LINKS = {
    "chokepoint:red-sea": ("chokepoint:bab-el-mandeb-strait", "chokepoint:suez-canal"),
    "chokepoint:gulf-of-aden": ("chokepoint:bab-el-mandeb-strait",),
    "chokepoint:black-sea": ("chokepoint:bosporus-strait", "chokepoint:kerch-strait"),
}

# --------------------------------------------------------------------------- GDACS codes (PortWatch + GDACS)

GDACS_EVENT_TYPES = {
    "TC": EventType.SEVERE_WEATHER, "FL": EventType.SEVERE_WEATHER, "DR": EventType.SEVERE_WEATHER,
    "EQ": EventType.NATURAL_DISASTER, "VO": EventType.NATURAL_DISASTER, "WF": EventType.NATURAL_DISASTER,
    "TS": EventType.NATURAL_DISASTER,
}
ALERT_SEVERITY = {"RED": "high", "ORANGE": "medium", "GREEN": "low"}

# --------------------------------------------------------------------------- relevance + geo

# Does a headline talk about shipping at all? Used to filter firehose sources (GDELT raw, Google News).
# Deliberately excludes navy/crew/terminal/carrier: they mostly match defence, aviation and sport stories.
_MARITIME_RE = re.compile(
    r"\b(?:ports?|seaports?|harbou?rs?|shipping|shipments?|maritime|vessels?|ships?|tankers?|container ?ships?|"
    r"containers?|freight|cargo|canal|straits?|dock(?:s|workers?)?|longshore\w*|seafarers?|mariners?|shipowners?|"
    r"supply chains?|chokepoints?|sea lanes?|red sea|hormuz|suez|malacca|bab[\s-]*(?:el|al)[\s-]*mand[ae]b)\b",
    re.I,
)


def is_maritime(text: str) -> bool:
    return bool(_MARITIME_RE.search(text or ""))


def load_ports(path: Path = PORTS_CSV) -> Optional[pd.DataFrame]:
    if not path.exists():
        log.warning("%s not found (run fetch_portwatch.py first)", path)
        return None
    return pd.read_csv(path, dtype={"port_id": str})


def nearby_ports(ports: pd.DataFrame, lat: float, lon: float, radius_km: float, limit: int = 25) -> pd.DataFrame:
    """Ports within radius_km of a point, nearest first, with a distance_km column."""
    if radius_km <= 0:
        return ports.iloc[0:0].assign(distance_km=[])
    lat1, lon1 = np.radians(lat), np.radians(lon)
    lat2, lon2 = np.radians(ports["lat"].to_numpy()), np.radians(ports["lon"].to_numpy())
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    distance = 6371.0 * 2 * np.arcsin(np.sqrt(a))
    near = ports.assign(distance_km=distance.round(1))
    return near[near["distance_km"] <= radius_km].sort_values("distance_km").head(limit)


# --------------------------------------------------------------------------- event classifier

# First match wins, so order resolves ambiguity (e.g. "missile strike" -> security_threat).
_EVENT_RULES: list[tuple[EventType, re.Pattern[str]]] = [
    (rule, re.compile(rf"\b(?:{pattern})\b", re.I))
    for rule, pattern in [
        (EventType.SECURITY_THREAT,
         r"pira(?:cy|tes?)|hijack(?:ed|ing|s)?|missiles?|drones?|houthis?|attack(?:s|ed|ing)?|"
         r"seiz(?:e|ed|es|ing|ure)|warships?|naval|navy|military|(?<!trade )war|warfare|conflict|gunfire|armed|"
         r"invasion|(?:ship|vessel|tanker|carrier|boat)s? strikes?|strikes? on (?:a |the )?(?:ship|vessel|tanker)s?|"
         r"(?:ship|vessel|tanker|carrier|boat)s?(?: was| were| is| are)? (?:hit|struck|targeted)"),
        (EventType.LABOR_STRIKE,
         # a bare "strike" is too ambiguous (missile strike, ship strike): require labour context
         r"(?:ports?|dock|workers?|unions?|labou?r|pilots?|tug|crane|truckers?|drivers?|rail|staff|employees|"
         r"customs|general|nationwide|national) strikes?|strikes? (?:by|over|action|ballot|notice|threat)|"
         r"on strike|striking (?:workers|dockworkers|employees)|walkouts?|dock ?workers?|longshore(?:men|man)?|"
         r"ILA|ILWU|work stoppages?|lockouts?|industrial action|labou?r (?:dispute|unrest|action)|wage talks|"
         r"collective bargaining"),
        (EventType.CHOKEPOINT_DISRUPTION, "|".join(c.pattern for c in CHOKEPOINTS)),
        (EventType.PORT_CLOSURE,
         r"closures?|closed|closes|shut(?:s|down)?|shut down|halt(?:s|ed|ing)?|reopen(?:s|ed|ing)?|"
         r"suspend(?:s|ed|ing)? (?:operations|services|sailings|loading|port calls|calls|traffic|transits?)"),
        (EventType.SEVERE_WEATHER,
         r"typhoons?|hurricanes?|cyclones?|tropical storms?|storms?|flood(?:s|ing|ed)?|fog|droughts?|monsoon|"
         r"gales?|heavy rain|high winds?|blizzards?|heatwaves?|low water"),
        (EventType.NATURAL_DISASTER,
         r"earthquakes?|quakes?|tsunamis?|volcan(?:o|oes|ic)|eruptions?|wildfires?|forest fires?|landslides?"),
        (EventType.VESSEL_INCIDENT,
         r"collisions?|collided|allision|grounding|grounded|aground|capsiz(?:e|ed|es)|sank|sinking|sunk|"
         r"fire (?:on ?board|aboard)|blaze|explosions?|oil spills?|spills?|containers? (?:lost|overboard)|"
         r"bridge collapse|collapsed?|salvage|mayday|blackout|engine failure"),
        (EventType.PORT_CONGESTION,
         r"congest(?:ion|ed)|backlogs?|bottlenecks?|queues?|wait(?:ing)? times?|dwell times?|delays?|delayed|gridlock"),
        (EventType.REGULATORY_SANCTIONS,
         r"sanction(?:s|ed)?|tariffs?|embargo(?:es)?|customs|export bans?|import bans?|port fees?|trade war|"
         r"regulat(?:ion|ions|ory)|USTR|blacklist(?:ed)?"),
    ]
]


def classify_event(text: str) -> EventType:
    for event_type, pattern in _EVENT_RULES:
        if pattern.search(text or ""):
            return event_type
    return EventType.OTHER


# --------------------------------------------------------------------------- port gazetteer

# Port names that are common English words, surnames or maritime terms in title-case headlines.
AMBIGUOUS_PORT_NAMES = frozenset({
    "Anchorage", "Banana", "Bar", "Barry", "Brake", "Chester", "Colon", "Empire", "Esperance", "Hamilton",
    "Hull", "Koko", "Linden", "Male", "Manta", "Mobile", "Moss", "Natal", "Nelson", "Nice", "Providence",
    "Rio Grande", "Rodeo", "Santos", "Split", "Stade", "Tuna", "Turbo", "Victoria",
})
# Generic words that must never become an alias on their own (e.g. from "Port-Gentil").
_GENERIC_ALIAS_WORDS = frozenset({
    "Port", "Porto", "Puerto", "Bandar", "Mina", "Saint", "Santa", "Terminal", "Channel", "Ras Al", "Yung",
})
# Extra aliases for compound names that the generic rules below can't split safely.
PORT_ALIASES: dict[str, tuple[str, ...]] = {
    "Los Angeles-Long Beach": ("Long Beach",),
    "New York-New Jersey": ("New Jersey",),
    "Tianjin Xin Gang": ("Tianjin",),
    "Mumbai-Jawaharlal Nehru (Nhava Sheva)": ("Jawaharlal Nehru", "JNPT"),
}
_PAREN_RE = re.compile(r"^(?P<base>[^(]+?)\s*\((?P<inner>[^)]+)\)$")


def _aliases(name: str) -> list[str]:
    """Text forms of a PortWatch port name.

    'Qingdao Port' -> Qingdao; 'Guangzhou (Nansha)' -> Guangzhou, Nansha; 'Houston (US-TX)' -> Houston;
    'Tangier-Mediterranean' -> Tangier (only the part before a tight hyphen: the rest is often generic).
    '<Country> - Offshore Oil Terminal N' -> nothing (otherwise every country name would match).
    """
    if "Offshore Oil Terminal" in name:
        return []
    out = {name, *PORT_ALIASES.get(name, ())}
    if m := _PAREN_RE.match(name):
        out.add(m["base"])
        inner = m["inner"].strip()
        if inner[:1].isupper() and not re.fullmatch(r"[A-Z]{2}-[A-Z]{2,3}", inner):
            out.add(inner)
    for alias in list(out):
        if re.search(r"\w-\w", alias) and "(" not in alias:
            out.add(alias.split("-")[0])
    out |= {a[: -len(" Port")] for a in out if a.endswith(" Port")}
    return [a for a in out if len(a) >= 4 and a not in AMBIGUOUS_PORT_NAMES and a not in _GENERIC_ALIAS_WORDS]


class PortGazetteer:
    """Finds the most likely port (or chokepoint) named in free text."""

    def __init__(self, ports: Optional[pd.DataFrame]):
        # alias -> (rank, location); rank 0 = busiest port, so ties go to the bigger port.
        self._ports: dict[str, tuple[int, PortLocation]] = {}
        self._port_re: Optional[re.Pattern[str]] = None
        if ports is not None and not ports.empty:
            ranked = ports.sort_values("vessel_count_total", ascending=False, na_position="last")
            for rank, row in enumerate(ranked.itertuples(index=False)):
                loc = PortLocation(
                    name=row.port_name, kind="port", port_id=row.port_id, country=row.country,
                    lat=row.lat, lon=row.lon, match_method="gazetteer",
                )
                for alias in _aliases(row.port_name):
                    self._ports.setdefault(alias, (rank, loc))
            names = sorted(self._ports, key=len, reverse=True)  # longest first: "Port Said" before "Said"
            # Case-sensitive (port names are proper nouns); "Singapore-flagged" is a flag, not a port call.
            self._port_re = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, names)) + r")(?!\w|-flag)")
        self._chokepoints = [(re.compile(rf"\b(?:{c.pattern})\b", re.I), c.location("gazetteer")) for c in CHOKEPOINTS]

    @classmethod
    def from_csv(cls, path: Path = PORTS_CSV) -> "PortGazetteer":
        if not path.exists():
            log.warning("%s not found (run fetch_portwatch.py first); port_location will only match chokepoints", path)
            return cls(None)
        return cls(pd.read_csv(path, dtype={"port_id": str}))

    @property
    def port_count(self) -> int:
        return len({loc.port_id for _, loc in self._ports.values()})

    def _match_port(self, text: str) -> Optional[PortLocation]:
        if not self._port_re:
            return None
        hits = [self._ports[m.group(0)] for m in self._port_re.finditer(text)]
        return min(hits, key=lambda hit: hit[0])[1] if hits else None

    def _match_chokepoint(self, text: str) -> Optional[PortLocation]:
        """Earliest mention wins (the headline's subject); ties go to list order."""
        hits = [(m.start(), i, loc) for i, (pattern, loc) in enumerate(self._chokepoints) if (m := pattern.search(text))]
        return min(hits, key=lambda hit: hit[:2])[2] if hits else None

    def match(self, *texts: str, chokepoint_hints: str = "") -> Optional[PortLocation]:
        """Search texts in priority order (title first); ports beat chokepoints within a text.

        chokepoint_hints is lower-priority text (e.g. RSS categories) searched for chokepoints only.
        """
        for text in texts:
            if text and (loc := self._match_port(text) or self._match_chokepoint(text)):
                return loc
        return self._match_chokepoint(chokepoint_hints) if chokepoint_hints else None
