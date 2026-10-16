"""Minimal PDF generation (statements, receipts, KFS, FD advice, tax forms) with fpdf2.

The built-in PDF fonts are Latin-1 only, so the rupee sign is written as "Rs." and
other characters outside Latin-1 are replaced. Hindi PDFs need an embedded Devanagari
font, which the bank's document templates will provide.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from fpdf import FPDF

from core import BRAND_NAME, fmt_ist, now_iso


def _latin1(text: object) -> str:
    s = str(text if text is not None else "")
    s = s.replace("₹", "Rs. ").replace("–", "-").replace("—", "-").replace("’", "'")
    return s.encode("latin-1", "replace").decode("latin-1")


class Section:
    def __init__(self, heading: str, lines: Iterable[str] = (), table: tuple[Sequence[str], Sequence[Sequence[object]]] | None = None,
                 widths: Sequence[float] | None = None):
        self.heading = heading
        self.lines = list(lines)
        self.table = table
        self.widths = widths


def make_pdf(title: str, sections: Sequence[Section], *, password: str | None = None, footer_note: str | None = None,
             mock: bool = True) -> bytes:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 9, _latin1(BRAND_NAME), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _latin1(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 5, _latin1(f"Generated {fmt_ist(now_iso())}"), new_x="LMARGIN", new_y="NEXT")
    if mock:
        pdf.set_text_color(160, 0, 0)
        pdf.cell(0, 5, "MOCK / SYNTHETIC DATA - not a real banking document", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    for sec in sections:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, _latin1(sec.heading), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for line in sec.lines:
            pdf.multi_cell(0, 5, _latin1(line), new_x="LMARGIN", new_y="NEXT")
        if sec.table:
            headers, rows = sec.table
            n = max(1, len(headers))
            widths = list(sec.widths) if sec.widths else [usable / n] * n
            pdf.set_font("Helvetica", "B", 8)
            for h, w in zip(headers, widths):
                pdf.cell(w, 6, _latin1(h)[:60], border=1)
            pdf.ln(6)
            pdf.set_font("Helvetica", "", 8)
            for r in rows:
                for v, w in zip(r, widths):
                    text = _latin1(v)
                    max_chars = max(4, int(w / 1.8))
                    pdf.cell(w, 5.5, text[:max_chars], border=1)
                pdf.ln(5.5)
        pdf.ln(3)
    if footer_note:
        pdf.set_font("Helvetica", "I", 8)
        pdf.multi_cell(0, 4, _latin1(footer_note), new_x="LMARGIN", new_y="NEXT")
    if password:
        try:
            from fpdf.enums import EncryptionMethod

            pdf.set_encryption(owner_password=password + "#owner", user_password=password,
                               encryption_method=EncryptionMethod.AES_128)
        except Exception:  # older fpdf2 without AES support
            pdf.set_encryption(owner_password=password + "#owner", user_password=password)
    return bytes(pdf.output())
