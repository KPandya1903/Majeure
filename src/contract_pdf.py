"""Generate demonstration Bill of Lading / sales contracts as PDFs.

These exist so the demo can show the whole loop: a contract arrives as a PDF, extract_contract.py
reads it with Nemotron, the ports and straits it names resolve to PortWatch ids, and the shipment
then matches live disruption like any other.

What is real and what is not, deliberately:

* **Real** -- the vessel name and IMO number (check-digit validated), the carrier, and every port.
  These are public facts and can be looked up in Equasis or on any AIS site while you watch.
* **Fictional** -- the shipper, the consignee, the contract number, and every commercial term.
  Putting a real company's name on a fabricated contract with invented payment terms would be
  creating a false commercial record, so the parties are invented and the footer says so.

The clauses vary on purpose, because the variation is the point of the product: one contract
forbids deviation, another permits it; notice windows run 24 to 72 hours; one force majeure clause
names war and blockade while another excludes congestion. Two shipments hit by the same event get
different answers, and that only shows if the contracts genuinely differ.

    python src/contract_pdf.py                 # writes samples/contracts/*.pdf
    python src/contract_pdf.py --list          # show them without writing
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from fpdf import FPDF

from common import ROOT, setup_logging, utcnow

OUT_DIR = ROOT / "samples" / "contracts"

_SUBS = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"',
         "”": '"', "…": "...", " ": " "}


def _ascii(text: str) -> str:
    for a, b in _SUBS.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


def valid_imo(imo: str) -> bool:
    d = re.sub(r"\D", "", imo)
    return len(d) == 7 and sum(int(d[i]) * (7 - i) for i in range(6)) % 10 == int(d[6])


class Contract(NamedTuple):
    ref: str
    bl: str
    vessel: str
    imo: str
    carrier: str
    voyage: str
    shipper: str
    consignee: str
    pol: str
    pod: str
    transship: str
    route: str
    goods: str
    containers: int
    value_usd: int
    incoterm: str
    etd_offset: int          # days from today
    transit_days: int
    late_penalty: int
    free_days: int
    demurrage: int
    detention: int
    deviation_allowed: bool
    notice_hours: int
    law: str
    fm_clause: str


# Force majeure wordings that genuinely differ in what they cover. The narrow one is here because
# a clause that does NOT cover the event is the case the product has to get right -- recommending
# force majeure on a clause that excludes the event is the expensive kind of wrong.
FM_BROAD = ("Neither party shall be liable for any failure or delay in performance caused by war, "
            "hostilities, armed conflict, terrorism, piracy, blockade, strikes or labour disputes, "
            "or the closure or obstruction of any canal, strait or port, provided that the affected "
            "party notifies the other party in writing within {hours} hours of becoming aware of the "
            "event. Port congestion arising in the ordinary course of business shall not constitute "
            "force majeure.")
FM_WEATHER = ("Neither party shall be liable for failure or delay caused by acts of God, severe "
              "weather, earthquake, flood, fire or other natural catastrophe, provided written "
              "notice is given within {hours} hours. For the avoidance of doubt, this clause does "
              "not extend to war, hostilities, blockade or any act of a governmental authority.")
FM_STANDARD = ("Neither party shall be liable for delay or failure to perform caused by war, "
               "blockade, hostilities, piracy, strikes, closure of ports or canals, sanctions or "
               "government action, or any other cause beyond its reasonable control, provided that "
               "written notice is given within {hours} hours of the party becoming aware of the event.")

CONTRACTS = (
    # Red Sea exposed, deviation FORBIDDEN -- the expensive case, and the demo's centrepiece.
    Contract("OOCL-SC-2026-4417", "OOLU7256014", "OOCL WASHINGTON", "9417256", "OOCL", "118W",
             "Meridian Components (Ningbo) Co., Ltd.", "Northwall Distribution B.V.",
             "Ningbo, China", "Rotterdam, The Netherlands", "Singapore",
             "the Malacca Strait, the Bab el-Mandeb Strait and the Suez Canal",
             "Consumer electronics and small domestic appliances", 21, 1_480_000, "CIF",
             -2, 39, 2200, 5, 240, 130, False, 48, "English law", FM_BROAD),
    # Red Sea exposed, deviation PERMITTED -- same event, different answer.
    Contract("MSC-SC-2026-8812", "MSCU5641022", "MSC ILONA", "9225641",
             "Mediterranean Shipping Company", "241E",
             "Aurelia Textiles S.p.A.", "Halvorsen Retail Group AS",
             "Port Klang, Malaysia", "Hamburg, Germany", "",
             "the Bab el-Mandeb Strait and the Suez Canal",
             "Textiles, footwear and household linen", 14, 690_000, "FOB",
             -6, 34, 900, 7, 175, 95, True, 72, "Singapore law", FM_STANDARD),
    # Hormuz -- no sea alternative exists, so reroute must come back unavailable.
    Contract("HMM-SC-2026-2290", "HDMU5686007", "HYUNDAI SMART", "9475686", "HMM", "077W",
             "Gulf Polymer Industries FZE", "Deccan Plastics Pvt. Ltd.",
             "Jebel Ali, United Arab Emirates", "Mumbai-Jawaharlal Nehru (Nhava Sheva), India", "",
             "the Strait of Hormuz",
             "Polyethylene and polypropylene resin in 20' dry containers", 30, 845_000, "CFR",
             -1, 9, 1400, 3, 210, 120, True, 24, "English law", FM_STANDARD),
    # A NARROW clause: weather only. A security event must NOT qualify -- this is the negative case.
    Contract("ZIM-SC-2026-1175", "ZIMU4922009", "ZIM VIETNAM", "9244922",
             "ZIM Integrated Shipping", "312N",
             "Calabria Marble Works S.r.l.", "Tidewater Stone Supply LLC",
             "Piraeus, Greece", "New York-New Jersey, United States", "",
             "the Strait of Gibraltar",
             "Cut marble slabs and building stone on flat racks", 8, 410_000, "DAP",
             -9, 21, 650, 5, 160, 85, True, 48, "New York law", FM_WEATHER),
    # Panama -- a real alternative exists but it is long and costly.
    Contract("CMA-SC-2026-6034", "CMDU5972003", "CMA CGM GOYA", "9365972", "CMA CGM", "089E",
             "Pacific Harvest Foods Ltd.", "Atlantic Provision Partners Inc.",
             "Yantian, China", "New York-New Jersey, United States", "",
             "the Panama Canal",
             "Canned and preserved foodstuffs", 26, 1_120_000, "CIF",
             -4, 31, 1750, 5, 225, 115, True, 72, "English law", FM_STANDARD),
)


class _Doc(FPDF):
    def __init__(self, ref: str):
        super().__init__(format="A4", unit="mm")
        self._ref = ref
        self.set_auto_page_break(auto=True, margin=24)

    def footer(self) -> None:
        self.set_y(-18)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(140, 146, 154)
        self.multi_cell(166, 3.6, _ascii(
            f"{self._ref}   |   page {self.page_no()}   |   Demonstration document prepared for a "
            f"product evaluation. The vessel, IMO number, carrier and ports are real and publicly "
            f"verifiable; the parties and all commercial terms are fictional."), align="C")


def _row(pdf: _Doc, label: str, value: str) -> None:
    y = pdf.get_y()
    pdf.set_xy(22, y)
    pdf.set_font("Helvetica", size=9.5)
    pdf.set_text_color(115, 122, 131)
    pdf.cell(46, 5.6, _ascii(label))
    pdf.set_xy(68, y)
    pdf.set_font("Helvetica", "B", 9.5)
    pdf.set_text_color(25, 32, 40)
    pdf.multi_cell(120, 5.6, _ascii(value))
    pdf.set_x(22)


def _clause(pdf: _Doc, number: int, heading: str, text: str) -> None:
    pdf.ln(3)
    pdf.set_x(22)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(25, 32, 40)
    pdf.multi_cell(166, 5.6, _ascii(f"{number}.  {heading}"))
    pdf.set_x(22)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(40, 47, 56)
    pdf.multi_cell(166, 5.2, _ascii(text))


def build(c: Contract, today: Optional[datetime] = None) -> bytes:
    today = today or utcnow()
    etd = today + timedelta(days=c.etd_offset)
    eta = etd + timedelta(days=c.transit_days)
    fmt = "%d %B %Y"

    pdf = _Doc(f"Contract {c.ref}")
    pdf.add_page()
    pdf.set_margins(22, 20, 22)

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(25, 32, 40)
    pdf.multi_cell(166, 7.5, "SALES CONTRACT AND SHIPPING PARTICULARS")
    pdf.ln(1)
    pdf.set_draw_color(195, 202, 210)
    pdf.line(22, pdf.get_y(), 188, pdf.get_y())
    pdf.ln(5)

    for label, value in (
        ("Contract No.", c.ref),
        ("Bill of Lading No.", c.bl),
        ("Seller / Shipper", c.shipper),
        ("Buyer / Consignee", c.consignee),
        ("Carrier", c.carrier),
        ("Vessel / Voyage", f"{c.vessel}  (IMO {c.imo})  /  Voyage {c.voyage}"),
    ):
        _row(pdf, label, value)

    _clause(pdf, 1, "GOODS",
            f"{c.goods}, {c.containers} x 40' containers. "
            f"Invoice value: USD {c.value_usd:,}.")
    _clause(pdf, 2, "SHIPMENT",
            f"Port of loading: {c.pol}. "
            + (f"Transshipment: {c.transship}. " if c.transship else "")
            + f"Port of discharge: {c.pod}. Customary route: via {c.route}. "
              f"Estimated time of departure: {etd.strftime(fmt)}. "
              f"Estimated time of arrival: {eta.strftime(fmt)}.")
    _clause(pdf, 3, "DELIVERY TERMS", f"{c.incoterm} {c.pod.split(',')[0]} (Incoterms 2020).")
    _clause(pdf, 4, "LATE DELIVERY",
            f"If the goods arrive after the agreed arrival date, the Seller shall pay the Buyer "
            f"liquidated damages of USD {c.late_penalty:,} per day of delay.")
    _clause(pdf, 5, "PORT CHARGES",
            f"Free time at the port of discharge: {c.free_days} days. Thereafter demurrage of "
            f"USD {c.demurrage} per container per day and detention of USD {c.detention} per "
            f"container per day shall apply.")
    _clause(pdf, 6, "ROUTE AND DEVIATION",
            ("The Carrier may deviate from the customary route, including by sailing via the Cape "
             "of Good Hope or any other passage, where the customary route is unsafe, obstructed "
             "or closed." if c.deviation_allowed else
             "The Carrier shall not deviate from the customary route without the prior written "
             "consent of the Buyer. Any deviation made without such consent shall be at the "
             "Carrier's own cost and risk."))
    _clause(pdf, 7, "FORCE MAJEURE", c.fm_clause.format(hours=c.notice_hours))
    _clause(pdf, 8, "GOVERNING LAW", f"This contract is governed by {c.law}.")

    return bytes(pdf.output())


def filename(c: Contract) -> str:
    return f"{c.bl}_{re.sub(r'[^A-Za-z0-9]+', '-', c.vessel)}.pdf"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--list", action="store_true", help="describe them without writing files")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    log = logging.getLogger("portguard.contracts")

    bad = [c.vessel for c in CONTRACTS if not valid_imo(c.imo)]
    if bad:
        log.error("invalid IMO check digit: %s", ", ".join(bad))
        return 1

    if args.list:
        for c in CONTRACTS:
            print(f"  {c.bl:<13} {c.vessel:<18} IMO {c.imo}  {c.pol.split(',')[0]} -> "
                  f"{c.pod.split(',')[0]:<28} deviation={'yes' if c.deviation_allowed else 'NO '} "
                  f"notice={c.notice_hours}h")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for c in CONTRACTS:
        path = args.out / filename(c)
        path.write_bytes(build(c))
        log.info("%-34s %s", path.name, f"{c.vessel} - {c.pol.split(',')[0]} to {c.pod.split(',')[0]}")
    log.info("wrote %d contracts -> %s", len(CONTRACTS), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
