"""Render an approved draft as a formal PDF a counterparty can read, sign and file.

An email body is a message; a notice of force majeure is a document. The difference matters,
because the thing a counterparty has to do with it is sign it and put it on file, and the thing
their lawyer will ask for months later is the document, not the mail thread.

So the PDF carries what a notice has to carry to be useful on its own:

* the reference block -- B/L number, vessel and IMO, carrier, route -- so it identifies the
  shipment without the email around it;
* the notice deadline, stated explicitly, because a force majeure clause turns on having given
  notice inside the window and the document should prove it did;
* the sources the assertion rests on, printed as URLs rather than links, so they survive being
  printed and scanned;
* a signature block, so acknowledgement happens on the document itself.

fpdf2 rather than a browser renderer: pure Python with no system libraries, which is what makes
it work on Render's free tier. The output is deliberately plain -- Helvetica, one column, no
colour beyond a rule -- because this is correspondence, not a brochure.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fpdf import FPDF

# Latin-1 is fpdf2's built-in encoding for the core fonts. Embedding a Unicode TTF would drag a
# font file into the deployment for the sake of a few punctuation marks, so the handful of
# characters the drafts actually produce are folded to ASCII instead.
_SUBSTITUTIONS = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", "→": "->",
    " ": " ", "•": "-", "·": "-",
}


def _ascii(text: str) -> str:
    for src, dst in _SUBSTITUTIONS.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", "replace").decode("latin-1")


_URL_RE = re.compile(r"https?://[^\s)\]]+")


def _chunk(text: str, size: int) -> list[str]:
    """Split an unbreakable run (a URL) into printable pieces."""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class _Notice(FPDF):
    def __init__(self, reference: str):
        super().__init__(format="A4", unit="mm")
        self._reference = reference
        self.set_auto_page_break(auto=True, margin=22)

    def footer(self) -> None:
        self.set_y(-16)
        self.set_font("Helvetica", size=7.5)
        self.set_text_color(130, 136, 145)
        self.cell(0, 4, _ascii(f"{self._reference}  ---  page {self.page_no()} of {{nb}}"),
                  align="C")


def build(kind: str, subject: str, body: str, shipment: dict,
          deadline_at: Optional[str] = None, generated_by: str = "") -> bytes:
    """Return the notice as PDF bytes. `shipment` is a row from the shipments table."""
    title = ("NOTICE OF FORCE MAJEURE" if kind == "force_majeure_notice"
             else "SHIPMENT STATUS NOTICE")
    reference = shipment.get("bl_number") or shipment.get("shipment_id") or "-"

    pdf = _Notice(f"B/L {reference}")
    pdf.add_page()
    pdf.set_margins(22, 20, 22)

    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(25, 32, 40)
    pdf.multi_cell(166, 8, _ascii(title))
    pdf.ln(1)

    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(105, 112, 122)
    issued = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    pdf.multi_cell(166, 5, _ascii(f"Issued {issued}"))
    pdf.ln(2)
    pdf.set_draw_color(200, 206, 213)
    pdf.line(22, pdf.get_y(), 188, pdf.get_y())
    pdf.ln(5)

    # Reference block: what shipment this concerns, so the document stands alone.
    rows = [
        ("Bill of Lading", shipment.get("bl_number")),
        ("Vessel", f"{shipment.get('vessel_name') or '-'}"
                   f"{'  (IMO ' + shipment['imo'] + ')' if shipment.get('imo') else ''}"),
        ("Carrier", shipment.get("carrier")),
        ("Voyage", shipment.get("voyage_number")),
        ("Contractual ETA", (shipment.get("eta") or "")[:10]),
        ("Governing law", shipment.get("governing_law")),
    ]
    if deadline_at:
        rows.append(("Notice due by", _ascii(str(deadline_at)[:16].replace("T", " ") + " UTC")))
    # Two columns by explicit x, not by letting cell() advance the cursor: fpdf2's cell() leaves x
    # wherever the label ended, and a following multi_cell(0) measures its width from there, which
    # raises "not enough horizontal space to render a single character" rather than wrapping.
    label_x, value_x, right = 22, 62, 188
    for label, value in rows:
        if not value:
            continue
        y = pdf.get_y()
        pdf.set_xy(label_x, y)
        pdf.set_font("Helvetica", size=9.5)
        pdf.set_text_color(120, 127, 136)
        pdf.cell(value_x - label_x, 6, _ascii(label))
        pdf.set_xy(value_x, y)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(25, 32, 40)
        pdf.multi_cell(right - value_x, 6, _ascii(str(value)))
        pdf.set_x(label_x)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(25, 32, 40)
    pdf.multi_cell(166, 6, _ascii(subject))
    pdf.ln(2)

    # Body, with the source URLs pulled out so the prose reads cleanly and the citations survive
    # being printed. A notice nobody can check is just an assertion.
    sources = list(dict.fromkeys(_URL_RE.findall(body)))
    prose = _URL_RE.sub("", body)
    # Lifting the URLs out leaves the label behind: "(source: )" inline, or a trailing "Source:"
    # on its own line. Both read as a formatting fault on a document someone is asked to sign.
    prose = re.sub(r"\(\s*sources?:\s*\)", "", prose, flags=re.I)
    prose = re.sub(r"(?im)^\s*sources?\s*:\s*$", "", prose)
    prose = re.sub(r"(?i)\bsources?:\s*(?=$|\n)", "", prose)
    prose = re.sub(r"\n{3,}", "\n\n", prose)
    prose = re.sub(r"[ \t]{2,}", " ", prose).strip()

    pdf.set_font("Helvetica", size=10.5)
    pdf.set_text_color(38, 45, 54)
    for para in [p.strip() for p in prose.split("\n") if p.strip()]:
        pdf.set_x(22)
        pdf.multi_cell(166, 5.6, _ascii(para))
        pdf.ln(2)

    if sources:
        pdf.ln(2)
        pdf.set_x(22)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(105, 112, 122)
        pdf.multi_cell(166, 5, "EVIDENCE RELIED UPON")
        pdf.set_font("Helvetica", size=8.5)
        for i, url in enumerate(sources, start=1):
            pdf.set_x(22)
            # A URL contains no spaces, so fpdf2 has nowhere to break it and raises rather than
            # wrapping. Hard-chunk it instead; the full address still reads correctly down the page,
            # which matters because a notice whose citations are truncated cannot be checked.
            pdf.multi_cell(166, 4.6, _ascii(f"[{i}] " + "\n     ".join(_chunk(url, 96))))
        pdf.ln(1)

    # Signature block: the point of sending a document rather than an email.
    pdf.ln(6)
    if pdf.get_y() > 225:
        pdf.add_page()
    pdf.set_draw_color(200, 206, 213)
    pdf.line(22, pdf.get_y(), 188, pdf.get_y())
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(105, 112, 122)
    pdf.multi_cell(166, 5, "ACKNOWLEDGED AND RECEIVED")
    pdf.ln(7)
    pdf.set_draw_color(150, 157, 166)
    y = pdf.get_y()
    for x, width, label in ((22, 74, "Signature"), (104, 40, "Name"), (150, 38, "Date")):
        pdf.line(x, y, x + width, y)
        pdf.set_xy(x, y + 1)
        pdf.set_font("Helvetica", size=8)
        pdf.set_text_color(130, 136, 145)
        pdf.cell(width, 4, label)
    pdf.ln(12)

    pdf.set_font("Helvetica", "I", 7.5)
    pdf.set_text_color(140, 146, 154)
    pdf.multi_cell(166, 4, _ascii(
        f"Prepared by Majeure{' (' + generated_by + ')' if generated_by else ''} from the "
        f"cited sources and the contract terms on file. Cost and delay figures are estimates from "
        f"stated assumptions, not quotations. This document requires human approval before it is "
        f"served and does not itself constitute legal advice."))

    out = pdf.output()
    return bytes(out)


def filename(kind: str, shipment: dict) -> str:
    ref = re.sub(r"[^A-Za-z0-9_-]+", "-", str(shipment.get("bl_number") or "notice"))
    stem = "Force-Majeure-Notice" if kind == "force_majeure_notice" else "Shipment-Notice"
    return f"{stem}_{ref}.pdf"
