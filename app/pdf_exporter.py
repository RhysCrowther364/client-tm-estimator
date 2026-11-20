"""Generates an executive RWS 2025 Branded PDF Suitability Report matching the application dashboard."""
from __future__ import annotations

import datetime as dt
import io
import math
from pathlib import Path
from typing import Optional

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.config import AppSettings, load_settings
from app.models import AnalysisJob, PatentAnalysisRow
from app.tm_engine import compute_job_summary

# Official RWS 2025 Brand Book Palette
C_GALAXY = HexColor("#38006B")
C_GALAXY_DEEP = HexColor("#240047")
C_PINK = HexColor("#E60050")
C_PINK_DARK = HexColor("#B80040")
C_PINK_SOFT = HexColor("#FDE8EF")
C_PINK_LIGHT = HexColor("#FF85B0")
C_BLUE = HexColor("#7B5CF5")
C_LAVENDER = HexColor("#B79EFF")
C_LAV_BG = HexColor("#F4F0FA")
C_INK = HexColor("#0B0B0F")
C_MUTED = HexColor("#5B5866")
C_SUBTLE = HexColor("#8E8A9E")
C_CANVAS = HexColor("#F7F6FA")
C_WHITE = HexColor("#FFFFFF")
C_BORDER = HexColor("#E4E0F0")
C_BAND_MID = HexColor("#9E9BB0")
C_BAND_LOW = HexColor("#E2E0EC")
C_ZEBRA = HexColor("#FAF9FD")

_FONTS_REGISTERED = False
_FONT_LATIN_REG = "Helvetica"
_FONT_LATIN_BOLD = "Helvetica-Bold"
_FONT_CJK_REG = "Helvetica"
_FONT_CJK_BOLD = "Helvetica-Bold"


def _ensure_fonts() -> None:
    """Register Windows Segoe UI (Latin) and Malgun Gothic (CJK/Unicode) TrueType fonts with fallback."""
    global _FONTS_REGISTERED, _FONT_LATIN_REG, _FONT_LATIN_BOLD, _FONT_CJK_REG, _FONT_CJK_BOLD
    if _FONTS_REGISTERED:
        return

    win_fonts = Path(r"C:\Windows\Fonts")
    try:
        seg_r = win_fonts / "segoeui.ttf"
        seg_b = win_fonts / "segoeuib.ttf"
        if seg_r.exists() and seg_b.exists():
            pdfmetrics.registerFont(TTFont("RWSSans", str(seg_r)))
            pdfmetrics.registerFont(TTFont("RWSSans-Bold", str(seg_b)))
            _FONT_LATIN_REG = "RWSSans"
            _FONT_LATIN_BOLD = "RWSSans-Bold"
    except Exception:
        pass

    try:
        mal_r = win_fonts / "malgun.ttf"
        mal_b = win_fonts / "malgunbd.ttf"
        if mal_r.exists() and mal_b.exists():
            pdfmetrics.registerFont(TTFont("RWSUnicode", str(mal_r)))
            pdfmetrics.registerFont(TTFont("RWSUnicode-Bold", str(mal_b)))
            _FONT_CJK_REG = "RWSUnicode"
            _FONT_CJK_BOLD = "RWSUnicode-Bold"
        else:
            _FONT_CJK_REG = _FONT_LATIN_REG
            _FONT_CJK_BOLD = _FONT_LATIN_BOLD
    except Exception:
        _FONT_CJK_REG = _FONT_LATIN_REG
        _FONT_CJK_BOLD = _FONT_LATIN_BOLD

    _FONTS_REGISTERED = True


def _pick_font(text: str, bold: bool = False) -> str:
    """Use Segoe UI for Western text and Malgun Gothic whenever CJK/Hangul characters appear."""
    has_cjk = any("\u2e80" <= ch <= "\ud7af" or "\uff00" <= ch <= "\uffef" for ch in text)
    if has_cjk:
        return _FONT_CJK_BOLD if bold else _FONT_CJK_REG
    return _FONT_LATIN_BOLD if bold else _FONT_LATIN_REG


def _fmt_int(val: float | int) -> str:
    return f"{int(round(val or 0)):,}"


def _fmt_pct(val: float) -> str:
    return f"{(val or 0.0) * 100:.2f}%"


def _truncate_to_width(text: str, font_name: str, font_size: float, max_width: float) -> str:
    if pdfmetrics.stringWidth(text, font_name, font_size) <= max_width:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        cand = text[:mid] + ell
        if pdfmetrics.stringWidth(cand, font_name, font_size) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ell


def _draw_rws_logo_white(c: canvas.Canvas, x: float, y: float, scale: float = 0.62) -> float:
    """Draw official RWS 2025 white illuminated double-pin vector mark + wordmark on Galaxy Purple."""
    c.saveState()
    c.translate(x, y)
    c.scale(scale, scale)
    c.setStrokeColor(C_WHITE)
    c.setLineWidth(2.5)
    c.setLineJoin(1)

    # Pin 1 (left)
    p1 = c.beginPath()
    p1.arc(7, 15, 37, 45, startAng=315, extent=270)
    p1.lineTo(22, 4.8)
    p1.close()
    c.drawPath(p1, stroke=1, fill=0)

    # Pin 2 (right overlapping)
    p2 = c.beginPath()
    p2.arc(16, 20, 46, 50, startAng=315, extent=270)
    p2.lineTo(31, 9.8)
    p2.close()
    c.drawPath(p2, stroke=1, fill=0)

    c.setFillColor(C_WHITE)
    c.setFont(_FONT_LATIN_BOLD, 33)
    c.drawString(54, 17, "rws")
    c.restoreState()
    return 115 * scale


def _draw_title_with_pink_dot(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    size: float,
    text_color: Color,
    max_width: float = 520.0,
) -> None:
    font = _pick_font(text, bold=True)
    clean = _truncate_to_width(text.rstrip("."), font, size, max_width - size * 0.5)
    c.setFont(font, size)
    c.setFillColor(text_color)
    c.drawString(x, y, clean)
    w = pdfmetrics.stringWidth(clean, font, size)
    c.setFont(_FONT_LATIN_BOLD, size * 1.08)
    c.setFillColor(C_PINK)
    c.drawString(x + w + 0.5, y, ".")


def _draw_card(
    c: canvas.Canvas,
    x: float,
    y_bottom: float,
    w: float,
    h: float,
    fill_color: Color = C_WHITE,
    stroke_color: Optional[Color] = C_BORDER,
    radius: float = 9.0,
) -> None:
    c.saveState()
    c.setFillColor(fill_color)
    if stroke_color is not None:
        c.setStrokeColor(stroke_color)
        c.setLineWidth(0.75)
        c.roundRect(x, y_bottom, w, h, radius, stroke=1, fill=1)
    else:
        c.roundRect(x, y_bottom, w, h, radius, stroke=0, fill=1)
    c.restoreState()


def _draw_page_footer(c: canvas.Canvas, page_w: float, margin: float, client_name: str, page_num: int, total_pages: int) -> None:
    y = 14.0
    c.saveState()
    c.setStrokeColor(C_BORDER)
    c.setLineWidth(0.6)
    c.line(margin, y + 11, page_w - margin, y + 11)

    c.setFont(_FONT_LATIN_BOLD, 7.5)
    c.setFillColor(C_GALAXY)
    lead = "RWS  •  Unlock global understanding"
    c.drawString(margin, y, lead)
    lw = pdfmetrics.stringWidth(lead, _FONT_LATIN_BOLD, 7.5)
    c.setFillColor(C_PINK)
    c.drawString(margin + lw + 0.5, y, ".")

    mid_txt = f"Prepared for {client_name}  •  Deterministic Trados Studio 22 Simulation (Zero AI)"
    mid_font = _pick_font(mid_txt, bold=False)
    mid_clean = _truncate_to_width(mid_txt, mid_font, 7.2, 380)
    c.setFont(mid_font, 7.2)
    c.setFillColor(C_MUTED)
    c.drawCentredString(page_w / 2.0, y, mid_clean)

    c.setFont(_FONT_LATIN_BOLD, 7.5)
    c.setFillColor(C_MUTED)
    c.drawRightString(page_w - margin, y, f"Page {page_num} of {total_pages}")
    c.restoreState()


def _nice_axis_max(max_val: float) -> float:
    if max_val <= 10.0:
        return 10.0
    step = 10.0 if max_val > 25.0 else 5.0
    return min(100.0, math.ceil((max_val * 1.12) / step) * step)


def _draw_growth_chart(c: canvas.Canvas, x: float, y_bottom: float, w: float, h: float, rows: list[PatentAnalysisRow]) -> None:
    """Render Vector Chart 1: Individual Patent Bars (Hidcote Blue) + Rolling Cumulative Line (Hillier Pink)."""
    _draw_card(c, x, y_bottom, w, h)
    top = y_bottom + h

    _draw_title_with_pink_dot(c, "TM Development & Savings Growth Curve", x + 14, top - 19, 10.5, C_GALAXY, w - 28)
    c.setFont(_FONT_LATIN_REG, 7.3)
    c.setFillColor(C_MUTED)
    c.drawString(x + 14, top - 30, "Chronological progression: Patent #1 (Blank TM) → Latest Patent")

    # Legend (row below subtitle, right-aligned or inline)
    leg_y = top - 42
    # Item 1: Bar swatch
    c.setFillColor(C_BLUE)
    c.roundRect(x + 14, leg_y - 1, 8, 6, 1.5, stroke=0, fill=1)
    c.setFont(_FONT_LATIN_BOLD, 7.0)
    c.setFillColor(C_INK)
    c.drawString(x + 25, leg_y, "Individual Patent Saving %")

    # Item 2: Line + node swatch
    lx2 = x + 148
    c.setStrokeColor(C_PINK)
    c.setLineWidth(2.0)
    c.line(lx2, leg_y + 2, lx2 + 12, leg_y + 2)
    c.setFillColor(C_GALAXY)
    c.circle(lx2 + 6, leg_y + 2, 2.3, stroke=0, fill=1)
    c.setFillColor(C_INK)
    c.drawString(lx2 + 16, leg_y, "Rolling Cumulative TM Saving %")

    # Plot area
    px = x + 38
    py = y_bottom + 24
    pw = w - 52
    ph = h - 76

    if not rows:
        return

    indiv = [r.pct_saving * 100.0 for r in rows]
    cumul = [r.cumulative_pct_saving * 100.0 for r in rows]
    y_max = _nice_axis_max(max(max(indiv, default=0.0), max(cumul, default=0.0), 10.0))

    # Gridlines & Y ticks
    ticks = 5
    for i in range(ticks + 1):
        val = (y_max / ticks) * i
        gy = py + (val / y_max) * ph
        c.setStrokeColor(C_BORDER if i > 0 else HexColor("#C9C3DC"))
        c.setLineWidth(0.5 if i > 0 else 0.8)
        c.line(px, gy, px + pw, gy)
        c.setFont(_FONT_LATIN_REG, 6.8)
        c.setFillColor(C_MUTED)
        c.drawRightString(px - 5, gy - 2.3, f"{int(round(val))}%")

    n = len(rows)
    slot_w = pw / n
    bar_w = max(2.2, min(14.0, slot_w * 0.62))

    # Bars (Hidcote Blue)
    c.saveState()
    c.setFillColor(Color(0.482, 0.361, 0.961, alpha=0.76))
    c.setStrokeColor(C_BLUE)
    c.setLineWidth(0.5)
    pts_cum: list[tuple[float, float]] = []

    for idx, r in enumerate(rows):
        cx = px + slot_w * (idx + 0.5)
        bh = max(0.8, (indiv[idx] / y_max) * ph) if indiv[idx] > 0 else 0.0
        if bh > 0:
            c.roundRect(cx - bar_w / 2.0, py, bar_w, bh, min(2.0, bar_w * 0.3), stroke=1, fill=1)
        cy = py + (cumul[idx] / y_max) * ph
        pts_cum.append((cx, cy))
    c.restoreState()

    # Rolling cumulative curve (Hillier Pink)
    if len(pts_cum) >= 2:
        c.saveState()
        c.setStrokeColor(C_PINK)
        c.setLineWidth(2.1)
        c.setLineJoin(1)
        c.setLineCap(1)
        path = c.beginPath()
        path.moveTo(pts_cum[0][0], pts_cum[0][1])
        for pt_x, pt_y in pts_cum[1:]:
            path.lineTo(pt_x, pt_y)
        c.drawPath(path, stroke=1, fill=0)
        c.restoreState()

    # Nodes (show every node when n <= 32, otherwise thin gracefully + always show first & last)
    node_step = 1 if n <= 32 else 2
    for idx, (pt_x, pt_y) in enumerate(pts_cum):
        if idx % node_step == 0 or idx == n - 1:
            c.setFillColor(C_GALAXY)
            c.setStrokeColor(C_WHITE)
            c.setLineWidth(0.9)
            c.circle(pt_x, pt_y, 2.5 if idx < n - 1 else 3.2, stroke=1, fill=1)

    # Final cumulative callout badge
    end_x, end_y = pts_cum[-1]
    end_lbl = f"{cumul[-1]:.1f}%"
    bw_lbl = pdfmetrics.stringWidth(end_lbl, _FONT_LATIN_BOLD, 6.8) + 8
    bx_lbl = min(px + pw - bw_lbl, max(px, end_x - bw_lbl / 2.0))
    by_lbl = min(py + ph - 9, end_y + 5)
    c.setFillColor(C_GALAXY)
    c.roundRect(bx_lbl, by_lbl, bw_lbl, 10.5, 3, stroke=0, fill=1)
    c.setFont(_FONT_LATIN_BOLD, 6.8)
    c.setFillColor(C_WHITE)
    c.drawCentredString(bx_lbl + bw_lbl / 2.0, by_lbl + 2.5, end_lbl)

    # X-axis labels (max ~10 evenly spaced labels)
    lbl_step = max(1, math.ceil(n / 10))
    c.setFont(_FONT_LATIN_REG, 6.6)
    c.setFillColor(C_MUTED)
    for idx in range(n):
        if idx == 0 or idx == n - 1 or (idx + 1) % lbl_step == 0:
            if idx < n - 1 and (n - 1 - idx) < max(1, lbl_step // 2) and n > 10:
                continue
            cx = px + slot_w * (idx + 0.5)
            c.drawCentredString(cx, py - 10, f"#{rows[idx].sequence_index}")


def _draw_match_bands_chart(c: canvas.Canvas, x: float, y_bottom: float, w: float, h: float, rows: list[PatentAnalysisRow]) -> None:
    """Render Vector Chart 2: 100% Stacked Match-Band Leverage Evolution."""
    _draw_card(c, x, y_bottom, w, h)
    top = y_bottom + h

    _draw_title_with_pink_dot(c, "Match-Band Leverage Evolution", x + 14, top - 19, 10.5, C_GALAXY, w - 28)
    c.setFont(_FONT_LATIN_REG, 7.3)
    c.setFillColor(C_MUTED)
    c.drawString(x + 14, top - 30, "Share of words (%) in each Trados Studio 22 match band per patent")

    # Compact single-line 6-band legend
    legend_items = [
        ("100%", C_GALAXY),
        ("Reps", C_PINK),
        ("95–99%", C_BLUE),
        ("85–94%", C_LAVENDER),
        ("50–84%", C_BAND_MID),
        ("No Match", C_BAND_LOW),
    ]
    lx = x + 14
    ly = top - 42
    for lbl, col in legend_items:
        c.setFillColor(col)
        c.roundRect(lx, ly - 0.8, 6.5, 6.0, 1.2, stroke=0, fill=1)
        c.setFont(_FONT_LATIN_BOLD, 6.6)
        c.setFillColor(C_INK)
        c.drawString(lx + 8.5, ly, lbl)
        lx += 8.5 + pdfmetrics.stringWidth(lbl, _FONT_LATIN_BOLD, 6.6) + 9.5

    px = x + 36
    py = y_bottom + 24
    pw = w - 50
    ph = h - 76

    if not rows:
        return

    for pct in (0, 25, 50, 75, 100):
        gy = py + (pct / 100.0) * ph
        c.setStrokeColor(C_BORDER if pct > 0 else HexColor("#C9C3DC"))
        c.setLineWidth(0.5 if pct > 0 else 0.8)
        c.line(px, gy, px + pw, gy)
        c.setFont(_FONT_LATIN_REG, 6.8)
        c.setFillColor(C_MUTED)
        c.drawRightString(px - 5, gy - 2.3, f"{pct}%")

    n = len(rows)
    slot_w = pw / n
    bar_w = max(2.4, min(14.5, slot_w * 0.68))

    for idx, r in enumerate(rows):
        tot = r.words_studio or 1
        cx = px + slot_w * (idx + 0.5)
        bx = cx - bar_w / 2.0
        bands = [
            (r.match_100, C_GALAXY),
            (r.repetitions, C_PINK),
            (r.match_95_99, C_BLUE),
            (r.match_85_94, C_LAVENDER),
            (r.match_75_84 + r.match_50_74, C_BAND_MID),
            (r.no_match, C_BAND_LOW),
        ]
        curr_y = py
        for val, col in bands:
            if val <= 0:
                continue
            seg_h = (val / tot) * ph
            c.setFillColor(col)
            c.rect(bx, curr_y, bar_w, seg_h, stroke=0, fill=1)
            curr_y += seg_h

    lbl_step = max(1, math.ceil(n / 10))
    c.setFont(_FONT_LATIN_REG, 6.6)
    c.setFillColor(C_MUTED)
    for idx in range(n):
        if idx == 0 or idx == n - 1 or (idx + 1) % lbl_step == 0:
            if idx < n - 1 and (n - 1 - idx) < max(1, lbl_step // 2) and n > 10:
                continue
            cx = px + slot_w * (idx + 0.5)
            c.drawCentredString(cx, py - 10, f"#{rows[idx].sequence_index}")


def _draw_executive_summary_page(
    c: canvas.Canvas,
    page_w: float,
    page_h: float,
    margin: float,
    job: AnalysisJob,
    use_rte: bool,
    settings: AppSettings,
    total_pages: int,
) -> None:
    """Render Page 1: Full RWS Dashboard View (Hero Banner + 4 KPI Cards + 2 Vector Charts + Portfolio Summary)."""
    summary = compute_job_summary(job.rows, include_rte=use_rte)
    usable_w = page_w - 2 * margin

    # Soft light brand canvas backdrop
    c.setFillColor(C_CANVAS)
    c.rect(0, 0, page_w, page_h, stroke=0, fill=1)

    # -------------------------------------------------------------------------
    # 1. Hero Header Banner (Galaxy Purple #38006B with RWS Decals)
    # -------------------------------------------------------------------------
    hero_h = 74.0
    hero_y = page_h - margin - hero_h
    c.saveState()
    p_clip = c.beginPath()
    p_clip.roundRect(margin, hero_y, usable_w, hero_h, 11)
    c.clipPath(p_clip, stroke=0, fill=0)

    c.setFillColor(C_GALAXY)
    c.rect(margin, hero_y, usable_w, hero_h, stroke=0, fill=1)

    # Subtle RWS Brand Book overlapping circle decals on right edge
    c.setFillColor(Color(0.482, 0.361, 0.961, alpha=0.22))
    c.circle(margin + usable_w - 58, hero_y + 12, 64, stroke=0, fill=1)
    c.setFillColor(Color(0.902, 0.0, 0.314, alpha=0.28))
    c.circle(margin + usable_w - 8, hero_y + 58, 54, stroke=0, fill=1)
    c.restoreState()

    # Left side of Hero: RWS Logo + Eyebrow + Client Name
    logo_w = _draw_rws_logo_white(c, margin + 16, hero_y + 40, scale=0.52)
    c.setFont(_FONT_LATIN_BOLD, 7.6)
    c.setFillColor(C_PINK_LIGHT)
    eyebrow_txt = (
        "TRADOS STUDIO 22  •  TM & RTE SUITABILITY ASSESSMENT"
        if use_rte
        else "TRADOS STUDIO 22  •  TM SUITABILITY ASSESSMENT"
    )
    c.drawString(margin + 22 + logo_w, hero_y + 52, eyebrow_txt)

    date_str = (job.created_at or "")[:10] or dt.date.today().isoformat()
    src_used = job.data_source_used or job.data_source_requested
    meta_line = (
        f"Source: {src_used}   •   Language: {job.language}   •   "
        f"Sample: {summary.total_patents} Chronological Patents (1 per Family)   •   Assessed: {date_str}"
    )
    c.setFont(_pick_font(meta_line, False), 7.8)
    c.setFillColor(HexColor("#DFD7F4"))
    c.drawString(margin + 22 + logo_w, hero_y + 40, _truncate_to_width(meta_line, _pick_font(meta_line, False), 7.8, 490))

    _draw_title_with_pink_dot(
        c,
        job.client_name,
        margin + 18,
        hero_y + 13,
        18.5,
        C_WHITE,
        max_width=usable_w - 220 if use_rte else usable_w - 190,
    )

    # Right side of Hero: Status Badge (only mentions RTE when RTE is explicitly included)
    if use_rte:
        badge_w, badge_h = 166.0, 24.0
        badge_x = margin + usable_w - badge_w - 16
        badge_y = hero_y + 13
        c.setFillColor(C_PINK)
        c.roundRect(badge_x, badge_y, badge_w, badge_h, 12, stroke=0, fill=1)
        c.setFont(_FONT_LATIN_BOLD, 7.6)
        c.setFillColor(C_WHITE)
        c.drawCentredString(badge_x + badge_w / 2.0, badge_y + 8.5, "RTE DEDUCTION: INCLUDED")
    else:
        badge_w, badge_h = 152.0, 24.0
        badge_x = margin + usable_w - badge_w - 16
        badge_y = hero_y + 13
        c.setFillColor(C_GALAXY_DEEP)
        c.setStrokeColor(HexColor("#7B5CF5"))
        c.setLineWidth(0.9)
        c.roundRect(badge_x, badge_y, badge_w, badge_h, 12, stroke=1, fill=1)
        c.setFont(_FONT_LATIN_BOLD, 7.6)
        c.setFillColor(HexColor("#E8E2FA"))
        c.drawCentredString(badge_x + badge_w / 2.0, badge_y + 8.5, "ROLLING TM SIMULATION")

    # -------------------------------------------------------------------------
    # 2. KPI Summary Cards Row (4 Cards when RTE Included, 3 Pure-TM Cards when Off)
    # -------------------------------------------------------------------------
    kpi_h = 78.0
    kpi_y = hero_y - 9 - kpi_h
    gap = 10.0

    saved_words = max(0.0, summary.total_words_studio - summary.reduced_tm_total)
    last_tm_segs = job.rows[-1].tm_size_segments_after if job.rows else 0

    if use_rte:
        kpis = [
            {
                "galaxy": True,
                "accent": C_PINK_LIGHT,
                "val_color": C_WHITE,
                "step": "01  •  HEADLINE DISCOUNT",
                "label": "Projected Cumulative TM Saving",
                "value": _fmt_pct(summary.projected_cumulative_tm_saving_pct),
                "foot": f"{_fmt_int(saved_words)} weighted words saved across {summary.total_patents} patents",
            },
            {
                "galaxy": False,
                "accent": C_BLUE,
                "val_color": C_GALAXY,
                "step": "02  •  MATURE RUN-RATE",
                "label": "Mature TM Saving (2nd Half Avg)",
                "value": _fmt_pct(summary.mature_tm_saving_pct),
                "foot": f"Ramps from {_fmt_pct(summary.first_patent_saving_pct)} (#1) → {_fmt_pct(summary.last_patent_saving_pct)} (#{summary.total_patents})",
            },
            {
                "galaxy": False,
                "accent": C_PINK,
                "val_color": C_PINK_DARK,
                "step": "03  •  REMOVABLE ELEMENTS",
                "label": "Removable RTE Saving (Active)",
                "value": _fmt_pct(summary.projected_cumulative_rte_saving_pct),
                "foot": f"Included: {_fmt_int(summary.total_rte_table_numbers)} table ints + {_fmt_int(summary.total_rte_nmr_words)} NMR words",
            },
            {
                "galaxy": False,
                "accent": C_BLUE,
                "val_color": C_INK,
                "step": "04  •  PORTFOLIO VOLUME",
                "label": "Assessed Trados Studio Words",
                "value": _fmt_int(summary.total_words_studio),
                "foot": f"{_fmt_int(summary.total_words_word)} MS Word  •  {_fmt_int(last_tm_segs)} TM segments",
            },
        ]
    else:
        kpis = [
            {
                "galaxy": True,
                "accent": C_PINK_LIGHT,
                "val_color": C_WHITE,
                "step": "01  •  HEADLINE DISCOUNT",
                "label": "Projected Cumulative TM Saving",
                "value": _fmt_pct(summary.projected_cumulative_tm_saving_pct),
                "foot": f"{_fmt_int(saved_words)} weighted words saved across {summary.total_patents} patents",
            },
            {
                "galaxy": False,
                "accent": C_BLUE,
                "val_color": C_GALAXY,
                "step": "02  •  MATURE RUN-RATE",
                "label": "Mature TM Saving (2nd Half Avg)",
                "value": _fmt_pct(summary.mature_tm_saving_pct),
                "foot": f"Ramps from {_fmt_pct(summary.first_patent_saving_pct)} (#1) → {_fmt_pct(summary.last_patent_saving_pct)} (#{summary.total_patents})",
            },
            {
                "galaxy": False,
                "accent": C_BLUE,
                "val_color": C_INK,
                "step": "03  •  PORTFOLIO VOLUME",
                "label": "Assessed Trados Studio Words",
                "value": _fmt_int(summary.total_words_studio),
                "foot": f"{_fmt_int(summary.total_words_word)} MS Word  •  {_fmt_int(last_tm_segs)} unique TM segments",
            },
        ]

    num_cards = len(kpis)
    card_w = (usable_w - (num_cards - 1) * gap) / float(num_cards)

    for idx, kpi in enumerate(kpis):
        kx = margin + idx * (card_w + gap)
        if kpi["galaxy"]:
            _draw_card(c, kx, kpi_y, card_w, kpi_h, fill_color=C_GALAXY, stroke_color=None)
            c.setFont(_FONT_LATIN_BOLD, 6.8)
            c.setFillColor(C_PINK_LIGHT)
            c.drawString(kx + 12, kpi_y + kpi_h - 15, kpi["step"])

            c.setFont(_FONT_LATIN_BOLD, 8.0)
            c.setFillColor(HexColor("#E8E0FA"))
            c.drawString(kx + 12, kpi_y + kpi_h - 27, kpi["label"])

            c.setFont(_FONT_LATIN_BOLD, 21.0)
            c.setFillColor(C_WHITE)
            c.drawString(kx + 12, kpi_y + 23, kpi["value"])

            c.setFont(_FONT_LATIN_REG, 6.8)
            c.setFillColor(HexColor("#D7CCF2"))
            c.drawString(kx + 12, kpi_y + 9, _truncate_to_width(kpi["foot"], _FONT_LATIN_REG, 6.8, card_w - 22))
        else:
            _draw_card(c, kx, kpi_y, card_w, kpi_h, fill_color=C_WHITE, stroke_color=C_BORDER)
            accent = kpi["accent"]
            c.setFillColor(accent)
            c.roundRect(kx + 12, kpi_y + kpi_h - 3.5, 34, 2.5, 1.2, stroke=0, fill=1)

            c.setFont(_FONT_LATIN_BOLD, 6.8)
            c.setFillColor(accent)
            c.drawString(kx + 12, kpi_y + kpi_h - 15, kpi["step"])

            c.setFont(_FONT_LATIN_BOLD, 8.0)
            c.setFillColor(C_MUTED)
            c.drawString(kx + 12, kpi_y + kpi_h - 27, _truncate_to_width(kpi["label"], _FONT_LATIN_BOLD, 8.0, card_w - 20))

            c.setFont(_FONT_LATIN_BOLD, 20.5)
            c.setFillColor(kpi["val_color"])
            c.drawString(kx + 12, kpi_y + 23, kpi["value"])

            c.setFont(_FONT_LATIN_REG, 6.8)
            c.setFillColor(C_MUTED)
            c.drawString(kx + 12, kpi_y + 9, _truncate_to_width(kpi["foot"], _FONT_LATIN_REG, 6.8, card_w - 22))

    # -------------------------------------------------------------------------
    # 3. Side-by-Side Vector Charts Row
    # -------------------------------------------------------------------------
    charts_h = 212.0
    charts_y = kpi_y - 9 - charts_h
    chart_w = (usable_w - 10.0) / 2.0

    _draw_growth_chart(c, margin, charts_y, chart_w, charts_h, job.rows)
    _draw_match_bands_chart(c, margin + chart_w + 10.0, charts_y, chart_w, charts_h, job.rows)

    # -------------------------------------------------------------------------
    # 4. Bottom Portfolio Match-Band & Methodology Summary Strip
    # -------------------------------------------------------------------------
    bot_y = 32.0
    bot_h = charts_y - 9 - bot_y
    left_w = usable_w * 0.61
    right_w = usable_w - left_w - 10.0

    # Left Card: Match-Band Payable Breakdown Table
    _draw_card(c, margin, bot_y, left_w, bot_h)
    _draw_title_with_pink_dot(
        c,
        "Portfolio Match-Band & Payable Rate Summary",
        margin + 13,
        bot_y + bot_h - 17,
        9.6,
        C_GALAXY,
        left_w - 26,
    )

    tot_st = summary.total_words_studio or 1
    band_summary_rows = [
        ("Repetitions (Internal)", summary.total_repetitions, settings.weight_repetitions),
        ("100% TM Match (incl. Context)", summary.total_match_100, settings.weight_100),
        ("95% – 99% Fuzzy Match", summary.total_match_95_99, settings.weight_95_99),
        ("85% – 94% Fuzzy Match", summary.total_match_85_94, settings.weight_85_94),
        (
            "50% – 84% & No Match (<50%)",
            summary.total_match_75_84 + summary.total_match_50_74 + summary.total_no_match,
            1.00,
        ),
    ]

    tx = margin + 12
    tw = left_w - 24
    hdr_y = bot_y + bot_h - 31
    c.setFillColor(C_GALAXY)
    c.roundRect(tx, hdr_y - 3, tw, 13, 3, stroke=0, fill=1)
    c.setFont(_FONT_LATIN_BOLD, 6.7)
    c.setFillColor(C_WHITE)

    col_x = [tx + 6, tx + 190, tx + 252, tx + 314, tx + 380, tx + tw - 6]
    c.drawString(col_x[0], hdr_y, "Trados Studio 22 Match Band")
    c.drawRightString(col_x[1], hdr_y, "Studio Words")
    c.drawRightString(col_x[2], hdr_y, "Portfolio %")
    c.drawRightString(col_x[3], hdr_y, "Payable Rate")
    c.drawRightString(col_x[4], hdr_y, "Payable Words")
    c.drawRightString(col_x[5], hdr_y, "Words Saved")

    row_step = 13.0
    curr_ry = hdr_y - 14.0
    for idx, (b_lbl, b_words, mult) in enumerate(band_summary_rows):
        if idx % 2 == 1:
            c.setFillColor(C_ZEBRA)
            c.rect(tx, curr_ry - 3, tw, row_step, stroke=0, fill=1)
        payable = b_words * mult
        saved = b_words - payable
        share = b_words / tot_st

        c.setFont(_FONT_LATIN_BOLD if mult < 1.0 else _FONT_LATIN_REG, 6.9)
        c.setFillColor(C_INK)
        c.drawString(col_x[0], curr_ry, b_lbl)
        c.setFont(_FONT_LATIN_REG, 6.9)
        c.drawRightString(col_x[1], curr_ry, _fmt_int(b_words))
        c.drawRightString(col_x[2], curr_ry, f"{share * 100:.1f}%")
        c.drawRightString(col_x[3], curr_ry, f"{int(round(mult * 100))}%")
        c.drawRightString(col_x[4], curr_ry, _fmt_int(payable))
        c.setFont(_FONT_LATIN_BOLD, 6.9)
        c.setFillColor(C_PINK_DARK if saved > 0 else C_MUTED)
        c.drawRightString(col_x[5], curr_ry, _fmt_int(saved))
        curr_ry -= row_step

    # Total Bar
    c.setFillColor(C_LAV_BG)
    c.roundRect(tx, curr_ry - 3.5, tw, 14, 3, stroke=0, fill=1)
    c.setFont(_FONT_LATIN_BOLD, 7.1)
    c.setFillColor(C_GALAXY)
    c.drawString(col_x[0], curr_ry, "PORTFOLIO TOTAL / NET PAYABLE")
    c.drawRightString(col_x[1], curr_ry, _fmt_int(summary.total_words_studio))
    c.drawRightString(col_x[2], curr_ry, "100.0%")
    c.drawRightString(col_x[3], curr_ry, f"{(1.0 - summary.projected_cumulative_tm_saving_pct) * 100:.1f}% eff.")
    c.drawRightString(col_x[4], curr_ry, _fmt_int(summary.reduced_tm_total))
    c.setFillColor(C_PINK)
    c.drawRightString(col_x[5], curr_ry, _fmt_int(saved_words))

    # Right Card: Methodology (& RTE Breakdown ONLY when use_rte is True)
    rx = margin + left_w + 10.0
    _draw_card(c, rx, bot_y, right_w, bot_h)
    _draw_title_with_pink_dot(
        c,
        "Assessment Scope & RTE Breakdown" if use_rte else "Assessment Scope & Methodology",
        rx + 13,
        bot_y + bot_h - 17,
        9.6,
        C_GALAXY,
        right_w - 26,
    )

    wc_delta_sign = "+" if summary.wc_diff_pct >= 0 else ""
    notes = [
        (
            "Sequential TM Build: ",
            f"Sorted oldest → newest (1 per family). Patent #1 runs against a Blank TM; each analyzed patent updates the rolling TM ({_fmt_int(last_tm_segs)} segments).",
        ),
        (
            "Assessed Sections: ",
            "Title, Abstract, Full Description, Claims, and Tables.",
        ),
        (
            "Word Count Comparison: ",
            f"{_fmt_int(summary.total_words_word)} MS Word vs. {_fmt_int(summary.total_words_studio)} Trados Studio ({wc_delta_sign}{summary.wc_diff_pct * 100:.2f}% delta).",
        ),
    ]
    if use_rte:
        notes.append(
            (
                "Removable RTE Included: ",
                f"{_fmt_int(summary.total_rte_words)} words ({_fmt_pct(summary.potential_rte_saving_pct)} of Word count) — {_fmt_int(summary.total_rte_table_numbers)} table whole numbers + {_fmt_int(summary.total_rte_nmr_words)} NMR shift words.",
            )
        )
    else:
        notes.append(
            (
                "Payable Weighting: ",
                f"{int(round(settings.weight_100 * 100))}% payable rate on Repetitions, 100% TM & 95–99% bands; {int(round(settings.weight_85_94 * 100))}% on 85–94%; 100% below 85%.",
            )
        )

    ny = bot_y + bot_h - 32
    max_note_w = right_w - 26
    for lead, body in notes:
        # Simple two-line word wrapper for crisp alignment
        full = lead + body
        words = full.split(" ")
        lines: list[str] = []
        curr = ""
        for wd in words:
            test = f"{curr} {wd}".strip()
            if pdfmetrics.stringWidth(test, _FONT_LATIN_REG, 6.9) <= max_note_w - 8:
                curr = test
            else:
                if curr:
                    lines.append(curr)
                curr = wd
        if curr:
            lines.append(curr)

        # Pink bullet dot
        c.setFillColor(C_PINK)
        c.circle(rx + 15, ny + 2.2, 1.8, stroke=0, fill=1)

        for l_idx, ln in enumerate(lines[:3]):
            lx_text = rx + 21
            if l_idx == 0 and ln.startswith(lead.rstrip()):
                lead_clean = lead
                rest = ln[len(lead_clean):]
                c.setFont(_FONT_LATIN_BOLD, 6.9)
                c.setFillColor(C_INK)
                c.drawString(lx_text, ny, lead_clean)
                lw_b = pdfmetrics.stringWidth(lead_clean, _FONT_LATIN_BOLD, 6.9)
                c.setFont(_FONT_LATIN_REG, 6.9)
                c.setFillColor(C_MUTED)
                c.drawString(lx_text + lw_b, ny, rest)
            else:
                c.setFont(_FONT_LATIN_REG, 6.9)
                c.setFillColor(C_MUTED)
                c.drawString(lx_text, ny, ln)
            ny -= 9.5
        ny -= 2.5
        if ny < bot_y + 8:
            break

    _draw_page_footer(c, page_w, margin, job.client_name, 1, total_pages)


def _draw_patent_table_page(
    c: canvas.Canvas,
    page_w: float,
    page_h: float,
    margin: float,
    job: AnalysisJob,
    slice_rows: list[PatentAnalysisRow],
    is_last_table_page: bool,
    use_rte: bool,
    page_num: int,
    total_pages: int,
) -> None:
    """Render Page 2+: Chronological Patent-by-Patent Breakdown Table with clickable patent links."""
    summary = compute_job_summary(job.rows, include_rte=use_rte)
    usable_w = page_w - 2 * margin

    c.setFillColor(C_CANVAS)
    c.rect(0, 0, page_w, page_h, stroke=0, fill=1)

    # Compact Top Header Strip
    hdr_h = 38.0
    hdr_y = page_h - margin - hdr_h
    _draw_card(c, margin, hdr_y, usable_w, hdr_h, fill_color=C_GALAXY, stroke_color=None, radius=8)
    logo_w = _draw_rws_logo_white(c, margin + 12, hdr_y + 6, scale=0.40)

    _draw_title_with_pink_dot(
        c,
        "Sequential Patent-by-Patent Analysis Breakdown",
        margin + 18 + logo_w,
        hdr_y + 13,
        12.0,
        C_WHITE,
        max_width=400,
    )

    right_meta = f"{job.client_name}  •  {job.language}  •  {len(job.rows)} Chronological Patents"
    rm_font = _pick_font(right_meta, bold=True)
    c.setFont(rm_font, 8.2)
    c.setFillColor(HexColor("#E8E0FA"))
    c.drawRightString(margin + usable_w - 14, hdr_y + 14, _truncate_to_width(right_meta, rm_font, 8.2, 260))

    # Table Container Card (sized snugly to fit rows + optional total bar so partial pages never leave giant empty boxes)
    row_h = 14.8 if len(job.rows) <= 30 else 16.5
    th_h = 20.0
    extra_tot_h = 26.0 if (is_last_table_page and job.rows) else 6.0
    needed_h = th_h + len(slice_rows) * row_h + extra_tot_h

    tbl_top = hdr_y - 8.0
    tbl_bot = max(32.0, tbl_top - needed_h)
    tbl_h = tbl_top - tbl_bot
    _draw_card(c, margin, tbl_bot, usable_w, tbl_h, fill_color=C_WHITE, stroke_color=C_BORDER, radius=8)

    # Columns across usable_w (~785.89 pt): 16 columns when RTE included, 15 columns when RTE omitted
    if use_rte:
        col_defs = [
            ("#", 22, "left"),
            ("Publication Ref.", 86, "left"),
            ("Pub Date", 52, "left"),
            ("Word WC", 48, "right"),
            ("Studio WC", 50, "right"),
            ("Reps", 42, "right"),
            ("100%", 44, "right"),
            ("95–99%", 44, "right"),
            ("85–94%", 44, "right"),
            ("75–84%", 42, "right"),
            ("50–74%", 42, "right"),
            ("No Match", 48, "right"),
            ("Payable WC", 54, "right"),
            ("% Saving", 56, "right"),
            ("Cumul. %", 50, "right"),
            ("RTE Words", 51.89, "right"),
        ]
    else:
        col_defs = [
            ("#", 24, "left"),
            ("Publication Ref.", 98, "left"),
            ("Pub Date", 56, "left"),
            ("Word WC", 50, "right"),
            ("Studio WC", 52, "right"),
            ("Reps", 45, "right"),
            ("100%", 47, "right"),
            ("95–99%", 47, "right"),
            ("85–94%", 47, "right"),
            ("75–84%", 44, "right"),
            ("50–74%", 44, "right"),
            ("No Match", 50, "right"),
            ("Payable WC", 60, "right"),
            ("% Saving", 62, "right"),
            ("Cumul. %", 59.89, "right"),
        ]

    # Table Header Bar
    th_h = 20.0
    th_y = tbl_top - th_h
    c.saveState()
    p_th = c.beginPath()
    p_th.roundRect(margin, th_y, usable_w, th_h, 7)
    c.clipPath(p_th, stroke=0, fill=0)
    c.setFillColor(HexColor("#2D0057"))
    c.rect(margin, th_y, usable_w, th_h, stroke=0, fill=1)
    c.restoreState()
    # Square bottom corners of header
    c.setFillColor(HexColor("#2D0057"))
    c.rect(margin, th_y, usable_w, 7, stroke=0, fill=1)

    c.setFont(_FONT_LATIN_BOLD, 6.8)
    c.setFillColor(C_WHITE)
    cx = margin + 5
    col_positions: list[tuple[float, float, str]] = []
    for title, width, align in col_defs:
        col_positions.append((cx, width - 8, align))
        if align == "left":
            c.drawString(cx, th_y + 6.5, title)
        else:
            c.drawRightString(cx + width - 8, th_y + 6.5, title)
        cx += width

    # Rows
    curr_y = th_y - row_h
    for r_idx, row in enumerate(slice_rows):
        if r_idx % 2 == 1:
            c.setFillColor(C_ZEBRA)
            c.rect(margin + 0.5, curr_y, usable_w - 1.0, row_h, stroke=0, fill=1)

        c.setStrokeColor(HexColor("#F0EDF7"))
        c.setLineWidth(0.4)
        c.line(margin + 4, curr_y, margin + usable_w - 4, curr_y)

        ty = curr_y + (4.4 if row_h < 15.5 else 5.2)

        # Col 0: #
        x0, w0, _ = col_positions[0]
        c.setFont(_FONT_LATIN_REG, 7.0)
        c.setFillColor(C_MUTED)
        c.drawString(x0, ty, str(row.sequence_index))

        # Col 1: Publication Ref (clickable link if source_url present)
        x1, w1, _ = col_positions[1]
        ref_txt = _truncate_to_width(row.file_ref, _FONT_LATIN_BOLD, 7.1, w1)
        c.setFont(_FONT_LATIN_BOLD, 7.1)
        c.setFillColor(C_BLUE if row.source_url else C_GALAXY)
        c.drawString(x1, ty, ref_txt)
        if row.source_url:
            rw = pdfmetrics.stringWidth(ref_txt, _FONT_LATIN_BOLD, 7.1)
            c.linkURL(row.source_url, (x1, curr_y + 1.5, x1 + rw, curr_y + row_h - 1.5), relative=0)

        # Col 2: Pub Date
        x2, w2, _ = col_positions[2]
        c.setFont(_FONT_LATIN_REG, 6.9)
        c.setFillColor(C_MUTED)
        c.drawString(x2, ty, _truncate_to_width(row.pub_date or "—", _FONT_LATIN_REG, 6.9, w2))

        # Numeric columns 3..12
        num_vals = [
            row.words_word,
            row.words_studio,
            row.repetitions,
            row.match_100,
            row.match_95_99,
            row.match_85_94,
            row.match_75_84,
            row.match_50_74,
            row.no_match,
            round(row.tm_words),
        ]
        for offset, val in enumerate(num_vals, start=3):
            xn, wn, _ = col_positions[offset]
            c.setFont(_FONT_LATIN_BOLD if offset == 12 else _FONT_LATIN_REG, 7.0)
            c.setFillColor(C_INK)
            c.drawRightString(xn + wn, ty, _fmt_int(val))

        # Col 13: % TM Saving pill badge
        pill_h = 11.0 if row_h < 15.5 else 12.0
        pill_y = curr_y + (row_h - pill_h) / 2.0
        x13, w13, _ = col_positions[13]
        sav_str = _fmt_pct(row.pct_saving)
        pill_w = min(w13, 44.0)
        c.setFillColor(C_PINK_SOFT)
        c.roundRect(x13 + w13 - pill_w, pill_y, pill_w, pill_h, 5.2, stroke=0, fill=1)
        c.setFont(_FONT_LATIN_BOLD, 6.9)
        c.setFillColor(C_PINK_DARK)
        c.drawCentredString(x13 + w13 - pill_w / 2.0, ty, sav_str)

        # Col 14: Cumulative TM %
        x14, w14, _ = col_positions[14]
        c.setFont(_FONT_LATIN_BOLD, 7.1)
        c.setFillColor(C_GALAXY)
        c.drawRightString(x14 + w14, ty, _fmt_pct(row.cumulative_pct_saving))

        # Col 15: Flagged RTE Words (rendered only when RTE is selected)
        if use_rte:
            x15, w15, _ = col_positions[15]
            rte_w = row.rte.total_rte_words if row.rte else 0
            if rte_w > 0:
                c.setFillColor(C_LAV_BG)
                chip_w = min(w15, 40.0)
                c.roundRect(x15 + w15 - chip_w, pill_y, chip_w, pill_h, 4.0, stroke=0, fill=1)
                c.setFont(_FONT_LATIN_BOLD, 6.9)
                c.setFillColor(C_GALAXY)
                c.drawCentredString(x15 + w15 - chip_w / 2.0, ty, _fmt_int(rte_w))
            else:
                c.setFont(_FONT_LATIN_REG, 6.9)
                c.setFillColor(C_SUBTLE)
                c.drawRightString(x15 + w15, ty, "0")

        curr_y -= row_h

    # Portfolio Total Footer Row on final table page
    if is_last_table_page and job.rows:
        tot_y = max(tbl_bot + 4, curr_y - 2)
        c.setFillColor(C_LAV_BG)
        c.setStrokeColor(C_BLUE)
        c.setLineWidth(0.7)
        c.roundRect(margin + 3, tot_y, usable_w - 6, 19.0, 5, stroke=1, fill=1)

        ty = tot_y + 6.0
        c.setFont(_FONT_LATIN_BOLD, 7.3)
        c.setFillColor(C_GALAXY)
        c.drawString(col_positions[0][0], ty, f"TOTAL ({len(job.rows)} Patents)")

        tot_nums = [
            summary.total_words_word,
            summary.total_words_studio,
            summary.total_repetitions,
            summary.total_match_100,
            summary.total_match_95_99,
            summary.total_match_85_94,
            summary.total_match_75_84,
            summary.total_match_50_74,
            summary.total_no_match,
            round(summary.reduced_tm_total),
        ]
        for offset, val in enumerate(tot_nums, start=3):
            xn, wn, _ = col_positions[offset]
            c.drawRightString(xn + wn, ty, _fmt_int(val))

        x13, w13, _ = col_positions[13]
        c.setFillColor(C_PINK)
        c.drawRightString(x13 + w13, ty, _fmt_pct(summary.projected_cumulative_tm_saving_pct))

        x14, w14, _ = col_positions[14]
        c.setFillColor(C_GALAXY)
        c.drawRightString(x14 + w14, ty, _fmt_pct(summary.projected_cumulative_tm_saving_pct))

        if use_rte:
            x15, w15, _ = col_positions[15]
            c.drawRightString(x15 + w15, ty, _fmt_int(summary.total_rte_words))

    _draw_page_footer(c, page_w, margin, job.client_name, page_num, total_pages)


def export_job_to_pdf(
    job: AnalysisJob,
    include_rte: Optional[bool] = None,
    settings: Optional[AppSettings] = None,
) -> bytes:
    """Render a complete RWS-branded Executive Dashboard & Sequential Patent Breakdown PDF."""
    _ensure_fonts()
    use_rte = job.include_rte if include_rte is None else include_rte
    cfg = settings or load_settings()

    page_w, page_h = landscape(A4)  # 841.89 x 595.27 pt
    margin = 28.0
    rows_per_page = 30 if len(job.rows or []) <= 30 else 26

    all_rows = job.rows or []
    table_chunks: list[list[PatentAnalysisRow]] = [
        all_rows[i: i + rows_per_page] for i in range(0, len(all_rows), rows_per_page)
    ]
    if not table_chunks:
        table_chunks = [[]]

    total_pages = 1 + len(table_chunks)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle(f"RWS Trados TM Suitability Report - {job.client_name}")
    c.setAuthor("RWS Patent TM Suitability Estimator")
    subj = (
        f"Sequential Trados Studio 22 TM & RTE Assessment for {job.client_name}"
        if use_rte
        else f"Sequential Trados Studio 22 TM Suitability Assessment for {job.client_name}"
    )
    c.setSubject(subj)

    # Page 1: Executive Dashboard
    _draw_executive_summary_page(c, page_w, page_h, margin, job, use_rte, cfg, total_pages)
    c.showPage()

    # Pages 2..N: Chronological Patent Breakdown Table
    for idx, chunk in enumerate(table_chunks):
        is_last = idx == len(table_chunks) - 1
        _draw_patent_table_page(
            c=c,
            page_w=page_w,
            page_h=page_h,
            margin=margin,
            job=job,
            slice_rows=chunk,
            is_last_table_page=is_last,
            use_rte=use_rte,
            page_num=2 + idx,
            total_pages=total_pages,
        )
        c.showPage()

    c.save()
    return buf.getvalue()
