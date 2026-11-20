"""Removable RTE (Repetitive/Removable Text Elements) Detector.

Per team specification, when RTE mode is toggled ON, this strictly flags word counts of:
  1. Whole numbers inside tables (e.g., 12, 100, 1,250, -5 inside table cells)
  2. NMR spectroscopic characterization data blocks — both the NMR header AND the full shift/multiplicity
     data following it (e.g., 1H NMR, 13C NMR, 19F NMR, 31P NMR, H-NMR, δH/δC blocks, and NMR table columns)
"""
from __future__ import annotations

import re
from app.models import PatentDocument, RTEBreakdown
from app.segmentation import count_ms_word

# Whole number token inside a table cell (strictly integers, optionally signed or comma-grouped: 42, -10, 1,250)
# Excludes decimals (3.14), percentages (50%), alphanumeric codes (A12), and units (100mg)
_WHOLE_NUMBER_TOKEN_RE = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)$")

# Fallback plain-text table line detector (when tables were flattened with pipes or >=2 tab columns)
_PLAIN_TABLE_LINE_RE = re.compile(r"^(?:\|.*\|.*|\S[^\n]*\t[^\n]*\t[^\n]*)$")

# Explicit NMR header: 1H NMR, ¹H-NMR, 1 H-NMR (even if sup tag introduced space), 13C NMR, 19F NMR,
# 31P NMR, 11B NMR, 15N NMR, 2H NMR, 29Si NMR, H-NMR / C-NMR, or standalone NMR
_NMR_EXPLICIT_START_RE = re.compile(
    r"(?<![A-Za-z])"
    r"(?:"
    r"(?:(?:1|¹)\s*H\s*[/,&]\s*)?"
    r"(?:(?:1|¹|2|²|13|¹³|19|¹⁹|31|³¹|11|¹¹|15|¹⁵|29|²⁹)\s*(?:Si|[HCFPNB])|[HCFP]\s*[-\u2010-\u2015])"
    r"\s*[-\u2010-\u2015/\s]*NMR"
    r"|NMR"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)

# Standalone δ / δH / δC shift-block start (e.g., British/RSC patent style "δH (400 MHz, CDCl3) 8.45..."
# or synthesis characterization lines/cells "δ = 8.55 (1H, d), 8.12 (2H, d)...").
# Intentionally CASE-SENSITIVE on \u03b4 (lowercase δ) so uppercase Δ (ΔE, ΔT) never matches!
_NMR_DELTA_START_RE = re.compile(
    r"(?:^|(?<=[\s(\[（［:;,.]))"
    r"\u03b4(?:_?[HCFP])?"
    r"(?=\s*(?:[:=＝(\[（［]|ppm(?![A-Za-z])|[+-−]?\d+[.,]\d+))"
)

# Unmistakable NMR chemical-shift + multiplicity/integration tuple (for table cells in an NMR column that omit 'NMR' and 'δ')
_NMR_SHIFT_TUPLE_RE = re.compile(
    r"[+-−]?\d+[.,]\d+"
    r"(?:\s*[-\u2010-\u2015~～]\s*[+-−]?\d+[.,]\d+)?"
    r"\s*(?:ppm\s*)?"
    r"[\(（]\s*(?:"
    r"\d+\s*[HCFP]\b[^)）]{0,60}"
    r"|(?:br\.?\s*[sdtqm]|singlet|doublet|triplet|quartet|quintet|sextet|septet|multiplet|dd|dt|td|ddd|ABq|[sdtqm])\b"
    r"[^)）]{0,60}\b(?:\d+\s*[HCFP]|J\d*\s*[=＝]?|Hz)\b"
    r")[^)）]*[\)）]",
    re.IGNORECASE,
)

# Hard stop headers after a newline or punctuation indicating another analytical method or experimental prose began
_NMR_HARD_STOP_AFTER_SEP_RE = re.compile(
    r"^\s*(?:"
    r"\[\d{1,5}\]|\(\d{3,5}\)"
    r"|(?:LC[-\s]?MS|LRMS|HRMS|FD[-\s]?MS|FAB[-\s]?MS|ESI[-\s]?MS|MALDI(?:[-\s]?TOF)?|APCI|MS|m/z|"
    r"FT[-\s]?IR|IR|UV(?:[-\s]?Vis)?|HPLC|UPLC|GC[-\s]?MS|GC|GPC|DSC|TGA|XRD|Rf|m\.p\.|mp|b\.p\.|"
    r"Yield|Anal\.|Elemental|Step\s+\d|Example\s+\d|Intermediate\s+\d|Compound\s+\d|Preparation|"
    r"To\s+a\s+|A\s+mixture|A\s+solution|The\s+crude|The\s+title|The\s+residue|The\s+reaction|Following\s+)(?![A-Za-z])"
    r"|(?:수율|원소\s*분석|실시예|합성예|비교예|제조예|실험예|단계\s*\d)"
    r")",
    re.IGNORECASE,
)

# Chemical shift number or shift range, with optional attached/spaced ppm/MHz/Hz or integration count (2H, 3F, 2C)
# Note: units (ppm, MHz, Hz) are matched BEFORE single-letter nuclei, and nuclei require (?![A-Za-z]) so '3.36ppm' never truncates at 'p'!
_NMR_NUMBER_RE = re.compile(
    r"^[+-−]?\d+(?:[.,]\d+)?"
    r"(?:\s*(?:[-\u2010-\u2015~～]|to(?![A-Za-z]))\s*[+-−]?\d+(?:[.,]\d+)?)?"
    r"(?:\s*(?:ppm|MHz|kHz|Hz)(?![A-Za-z])|\s*(?:\d+\s*)?[HCFPNB](?![A-Za-z]))?",
    re.IGNORECASE,
)

# Deuterated solvents (supporting standard digits, HTML-spaced subscripts like 'CDCl 3' / 'D 2 O', and Unicode subscripts ₀-₉)
_NMR_SOLVENT_RE = re.compile(
    r"^(?:"
    r"DMSO(?:\s*[-\u2010-\u2015]\s*d\s*[6₆])?|d\s*[6₆]\s*[-\u2010-\u2015]\s*DMSO"
    r"|CDCl\s*[3₃]|CD\s*[2₂]\s*Cl\s*[2₂]|CD\s*[3₃]\s*OD|MeOD(?:\s*[-\u2010-\u2015]\s*d\s*[4₄])?"
    r"|CD\s*[3₃]\s*CN|D\s*[2₂]\s*O|C\s*[6₆]\s*D\s*[6₆]|C\s*[5₅]\s*D\s*[5₅]\s*N"
    r"|THF(?:\s*[-\u2010-\u2015]\s*d\s*[8₈])?|(?:acetone|toluene|pyridine)\s*[-\u2010-\u2015]\s*d\s*[568₅₆₈]"
    r"|TFA\s*[-\u2010-\u2015]\s*d|CF\s*[3₃]\s*COOD|C\s*[2₂]\s*D\s*[2₂]\s*Cl\s*[4₄]"
    r"|클로로포름\s*[-\u2010-\u2015]\s*d|중수소화\s*클로로포름|아세톤\s*[-\u2010-\u2015]\s*d\s*[6₆]"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)

# NMR vocabulary, multiplicities, coupling symbols, descriptors, and inline peak assignment labels
_NMR_TOKEN_RE = re.compile(
    r"^(?:"
    r"delta|ppm|MHz|kHz|Hz|TMS|J\d*"
    r"|chemical\s+shifts?|shifts?|spectra|spectrum|data"
    r"|스펙트럼|측정값|측정결과|분석결과|결과|데이터"
    r"|singlet|doublet|triplet|quartet|quintet|pentet|sextet|septet|heptet|octet|nonet|multiplet"
    r"|broad|brs|bs|bd|bt|bq|bm|br\.?"
    r"|dddd|ddd|ddt|dtd|tdd|dtq|dtt|ddq|dqd|ABq|abq|dd|dt|td|tt|dq|qd|qq"
    r"|quint\.?|pent\.?|sext\.?|sept\.?|hept\.?|oct\.?|non\.?"
    r"|apparent|app\.?|overlapping|overlapped|ov\.?|complex|rotamers?|major|minor|isomers?|approx\.?|ca\.?"
    r"|Ar[-\u2010-\u2015]?H|Ph[-\u2010-\u2015]?H|OCH\s*[3₃]|OMe|OEt|COOH|CHO|NH\s*[2₂]?|OH|SH|CH\s*[23₂₃]?"
    r"|[sdtqpm]\.?"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)

# Lowercase Greek delta (case-sensitive!)
_LOWER_DELTA_RE = re.compile(r"^\u03b4(?:_?[HCFP])?(?![A-Za-z])")

# Separators allowed between NMR shift items
_NMR_SEP_RE = re.compile(r"^[,;:=＝/\\+&~～\u2010-\u2015，、：；]+")

# Validation helpers
_DECIMAL_SHIFT_RE = re.compile(r"\d+[.,]\d+")
_NMR_EVIDENCE_RE = re.compile(
    r"(?:\u03b4|delta|ppm|MHz|Hz|DMSO|CDCl|CD2Cl2|CD3OD|MeOD|D2O|THF|CD3CN|C6D6|"
    r"[\(（][^)）]{0,60}\b(?:\d+\s*[HCFP]|s|d|t|q|m|dd|dt|td|ddd|br|singlet|doublet|triplet|quartet|quintet|sextet|septet|multiplet)\b)",
    re.IGNORECASE,
)


def _match_balanced_group(text: str, pos: int, limit: int, max_group_len: int = 220) -> int:
    """Match (...) / （...） / [...] / ［...］ supporting nested brackets up to max_group_len.
    Returns end index or 0 if not a valid NMR bracketed group."""
    ch = text[pos]
    pairs = {"(": ")", "（": "）", "[": "]", "［": "］"}
    close_ch = pairs.get(ch)
    if not close_ch:
        return 0

    end_bound = min(limit, pos + max_group_len)
    depth = 1
    i = pos + 1
    while i < end_bound and depth > 0:
        c = text[i]
        if c == "\n" and i + 1 < end_bound and text[i + 1] == "\n":
            return 0
        if c == ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
        i += 1

    if depth != 0:
        return 0

    inner = text[pos + 1: i - 1].strip()
    if not inner:
        return 0
    # Do not treat standalone patent paragraph numbers like [96] or [0096] as NMR groups
    if ch in "[［" and inner.isdigit():
        return 0
    return i


def _consume_nmr_tail(text: str, start_pos: int, max_scan: int = 4500) -> int:
    """Consume all spectroscopic header & shift tokens following an NMR start marker."""
    limit = min(len(text), start_pos + max_scan)
    cursor = start_pos
    valid_end = start_pos

    while cursor < limit:
        # 1. Horizontal whitespace
        if text[cursor] in " \t\f\v\u00a0":
            cursor += 1
            continue

        # 2. Line breaks: allow single line-wrap inside multi-line NMR spectra ONLY if next line is neither
        #    blank nor a paragraph tag / non-NMR section header
        if text[cursor] in "\r\n":
            nl_end = cursor + (2 if text[cursor: cursor + 2] == "\r\n" else 1)
            # Stop on blank line (paragraph break)
            ws_probe = nl_end
            while ws_probe < limit and text[ws_probe] in " \t":
                ws_probe += 1
            if ws_probe >= limit or text[ws_probe] in "\r\n":
                break
            if _NMR_HARD_STOP_AFTER_SEP_RE.match(text[ws_probe: min(limit, ws_probe + 60)]):
                break
            cursor = ws_probe
            continue

        # 3. Balanced (...) / [...] group (solvent/MHz header or (2H, d, J = 8.4 Hz, Ar-H) peak multiplicity)
        grp_end = _match_balanced_group(text, cursor, limit)
        if grp_end > cursor:
            cursor = grp_end
            valid_end = cursor
            continue

        window = text[cursor: min(limit, cursor + 80)]

        # 4. Separators (; , : = - ~) — stop if followed by MS / IR / Yield / next Example
        m_sep = _NMR_SEP_RE.match(window)
        if m_sep:
            after_sep = cursor + m_sep.end()
            if ";" in m_sep.group(0) and _NMR_HARD_STOP_AFTER_SEP_RE.match(text[after_sep: min(limit, after_sep + 60)]):
                valid_end = cursor + 1
                break
            cursor = after_sep
            continue

        # 5. Period / fullwidth stop: continue only if OCR/typo placed a period directly before another shift tuple;
        #    otherwise include trailing period of NMR sentence and stop
        if text[cursor] in ".。":
            after_dot = cursor + 1
            while after_dot < limit and text[after_dot] in " \t":
                after_dot += 1
            if _NMR_SHIFT_TUPLE_RE.match(text[after_dot: min(limit, after_dot + 90)]):
                cursor = after_dot
                continue
            if valid_end > start_pos:
                valid_end = cursor + 1
            break

        # 6. Lowercase Greek δ / δH / δC
        m_delta = _LOWER_DELTA_RE.match(window)
        if m_delta:
            cursor += m_delta.end()
            valid_end = cursor
            continue

        # 7. Deuterated solvents (must check before _NMR_TOKEN_RE / _NMR_NUMBER_RE)
        m_solv = _NMR_SOLVENT_RE.match(window)
        if m_solv:
            cursor += m_solv.end()
            valid_end = cursor
            continue

        # 8. Chemical shifts, ranges, frequencies, and attached units (3.85-3.36ppm, 400MHz, 8.4Hz, 2H)
        m_num = _NMR_NUMBER_RE.match(window)
        if m_num:
            cursor += m_num.end()
            valid_end = cursor
            continue

        # 9. Multiplicities, coupling symbols, NMR header descriptors, and peak labels
        m_tok = _NMR_TOKEN_RE.match(window)
        if m_tok:
            cursor += m_tok.end()
            valid_end = cursor
            continue

        # 10. Connector 'and' ONLY when joining two shift items (e.g., "8.12 (1H, s) and 7.45 (2H, d)")
        m_and = re.match(r"^and(?=\s+[+-−]?\d+[.,]\d+)", window, re.IGNORECASE)
        if m_and:
            cursor += m_and.end()
            continue

        break

    return valid_end


def _is_valid_nmr_span(span_str: str, tail_str: str, started_with_nmr: bool) -> bool:
    """Ensure candidate span contains genuine chemical shift data and is not a casual prose mention."""
    if count_ms_word(span_str) < 3:
        return False

    dec_shifts = _DECIMAL_SHIFT_RE.findall(tail_str)
    if started_with_nmr:
        # Must have at least one decimal chemical shift (or signed ppm shift) plus NMR spectroscopic context
        has_signed_ppm = bool(re.search(r"[+-−]\d+\s*ppm\b", tail_str, re.IGNORECASE))
        if not dec_shifts and not has_signed_ppm:
            return False
        if len(dec_shifts) >= 2:
            return True
        return bool(_NMR_EVIDENCE_RE.search(span_str))

    # Started with lowercase δ (no 'NMR' keyword): require strong NMR evidence so 'tan δ = 0.02' never matches
    if not dec_shifts:
        return False
    if len(dec_shifts) >= 3:
        return True
    if re.search(r"^\u03b4_?[HCFP]\b", span_str):
        return bool(_NMR_EVIDENCE_RE.search(span_str)) or len(dec_shifts) >= 2
    if _NMR_SOLVENT_RE.search(span_str) or re.search(r"\bMHz\b", span_str, re.IGNORECASE):
        return True
    return len(_NMR_SHIFT_TUPLE_RE.findall(span_str)) >= 2


def extract_nmr_spans(text: str, *, in_table_cell: bool = False) -> list[str]:
    """Identify authentic NMR spectroscopic characterization blocks (header + full shift list)."""
    if not text:
        return []

    has_nmr_kw = "nmr" in text.lower()
    has_delta = "\u03b4" in text
    if not has_nmr_kw and not has_delta and not in_table_cell:
        return []

    # Collect candidate start matches (explicit NMR headers + standalone δ shift blocks)
    candidates: list[tuple[int, int, bool]] = []
    if has_nmr_kw:
        for m in _NMR_EXPLICIT_START_RE.finditer(text):
            candidates.append((m.start(), m.end(), True))
    if has_delta:
        for m in _NMR_DELTA_START_RE.finditer(text):
            candidates.append((m.start(), m.end(), False))

    candidates.sort(key=lambda x: (x[0], -int(x[2])))

    spans: list[str] = []
    occupied_ranges: list[tuple[int, int]] = []
    last_end = 0

    for start, hdr_end, started_with_nmr in candidates:
        if start < last_end:
            continue
        end = _consume_nmr_tail(text, hdr_end)
        if end <= hdr_end:
            continue
        span_str = text[start:end].strip()
        tail_str = text[hdr_end:end]
        if _is_valid_nmr_span(span_str, tail_str, started_with_nmr):
            spans.append(span_str)
            occupied_ranges.append((start, end))
            last_end = end

    # Table-cell fallback: in OLED/pharma characterization tables where '1H-NMR' is only in the column header
    # and data cells contain pure shift lists like "8.55 (1H, d), 8.12 (2H, d), 7.94-7.32 (15H, m)"
    if in_table_cell and not spans and len(_NMR_SHIFT_TUPLE_RE.findall(text)) >= 2:
        first_num = re.search(r"[+-−]?\d+[.,]\d+", text)
        if first_num:
            s_idx = first_num.start()
            e_idx = _consume_nmr_tail(text, s_idx)
            if e_idx > s_idx:
                cell_span = text[s_idx:e_idx].strip()
                if count_ms_word(cell_span) >= 4:
                    spans.append(cell_span)

    return spans


def count_whole_numbers_in_cells(cells: list[str]) -> int:
    """Count whitespace-delimited whole-number tokens inside table cells."""
    total = 0
    for cell in cells:
        if not cell:
            continue
        for tok in cell.split():
            if _WHOLE_NUMBER_TOKEN_RE.match(tok):
                total += 1
    return total


def _extract_plain_text_table_cells(text: str) -> tuple[list[str], int]:
    """Fallback extraction of cells from pipe- or tab-delimited tables in plain text."""
    cells: list[str] = []
    tables_count = 0
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if _PLAIN_TABLE_LINE_RE.match(stripped):
            if not in_table:
                tables_count += 1
                in_table = True
            if "|" in stripped:
                parts = [p.strip() for p in stripped.strip("|").split("|")]
            else:
                parts = [p.strip() for p in stripped.split("\t")]
            cells.extend(p for p in parts if p)
        else:
            in_table = False
    return cells, tables_count


def analyze_patent_rte(doc: PatentDocument, total_words_word: int | None = None) -> RTEBreakdown:
    """Compute Removable RTE breakdown: strictly whole numbers in tables + full NMR data blocks (prose & tables)."""
    full_prose = "\n".join(
        part for part in (doc.title, doc.abstract_text, doc.description_text, doc.claims_text) if part
    )

    if doc.table_cells:
        cells = doc.table_cells
        tables_count = max(1, len(cells) // 6)
    else:
        cells, tables_count = _extract_plain_text_table_cells(full_prose)

    # 1. NMR spectroscopic data in prose
    nmr_spans: list[str] = extract_nmr_spans(full_prose, in_table_cell=False)

    # 2. Table cells: extract any NMR characterization blocks first, then count standalone whole numbers
    #    ONLY in the remaining non-NMR portion of each cell so zero words are ever double-counted!
    non_nmr_cell_fragments: list[str] = []
    for cell in cells:
        if not cell:
            continue
        cell_nmr = extract_nmr_spans(cell, in_table_cell=True)
        if cell_nmr:
            nmr_spans.extend(cell_nmr)
            remainder = cell
            for sp in cell_nmr:
                remainder = remainder.replace(sp, " ", 1)
            if remainder.strip():
                non_nmr_cell_fragments.append(remainder)
        else:
            non_nmr_cell_fragments.append(cell)

    table_whole_numbers = count_whole_numbers_in_cells(non_nmr_cell_fragments)
    nmr_words = sum(count_ms_word(span) for span in nmr_spans)

    total_rte = table_whole_numbers + nmr_words
    if total_words_word is not None and total_words_word > 1:
        total_rte = min(total_rte, total_words_word - 1)

    return RTEBreakdown(
        table_whole_numbers=table_whole_numbers,
        nmr_words=nmr_words,
        total_rte_words=total_rte,
        nmr_spans_count=len(nmr_spans),
        tables_count=tables_count,
    )
