"""Patent-aware SRX sentence segmentation and MS Word vs. Trados Studio word counting."""
from __future__ import annotations

import re

# Abbreviations common in EP/US/WO patent specifications (EN, DE, FR) that do not end a sentence
_ABBREV_SET = {
    "fig", "figs", "no", "nos", "pat", "publ", "ser", "appl", "vol", "pp", "ch",
    "sec", "eq", "eqs", "equiv", "wt", "mol", "approx", "ca", "cf", "vs", "al",
    "etc", "inc", "corp", "ltd", "co", "llc", "gmbh", "ag", "sa", "bv", "nv",
    "dr", "prof", "mr", "ms", "st", "ex", "comp", "ref", "refs", "tab", "prep",
    "aq", "sat", "conc", "dil", "anhyd", "min", "hr", "hrs", "temp", "mp", "bp",
    "lit", "calc", "calcd", "obsd", "rel", "max", "avg", "std", "dev",
    # Multi-dot / European abbreviations
    "e.g", "i.e", "u.s", "u.s.a", "u.k", "e.p", "p.c.t", "m.p", "b.p",
    "z.b", "d.h", "u.a", "bzw", "usw", "ggf", "abs", "nr", "vgl", "sog",
}

# Pre-split on hard structural breaks (newlines, paragraph tags, tabs)
_PARA_SPLIT_RE = re.compile(r"[\r\n]+|\t+")

# Candidate sentence boundary:
# 1) Western/Korean punctuation (. ! ?) followed by whitespace and uppercase/digit/Hangul/CJK/quote/bracket
# 2) Fullwidth CJK punctuation (。 ！ ？) with optional whitespace
_SENT_BOUNDARY_RE = re.compile(
    r"([.!?]+)(\s+)(?=[\"'(\[\u201c\u2018]*[A-ZÄÖÜÉÈÀ0-9\uac00-\ud7af\u3040-\u9fff])"
    r"|([。！？]+)(\s*)"
)

# Bracketed patent paragraph or claim numbers at start of paragraph, e.g. [1], [0001], (0015)
_PARA_NUM_PREFIX_RE = re.compile(r"^\s*(\[\d{1,5}\]|\(\d{1,5}\))\s*")

# MS Word word counter: non-whitespace sequences
_MS_WORD_TOKEN_RE = re.compile(r"\S+")

# Unspaced CJK scripts ONLY (Chinese Hanzi & Japanese Kana — NOT Korean Hangul, which is space-delimited in Word & Trados!)
_UNSPACED_CJK_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Trados Studio word splitter: splits on whitespace, slashes, em/en dashes, and hyphens between Unicode letters
_STUDIO_SPLIT_RE = re.compile(r"[\s/\u2013\u2014]+|(?<=[^\W\d_])-(?=[^\W\d_])", re.UNICODE)

# Token has at least one Unicode letter or digit (Studio ignores pure punctuation tokens; supports Latin, Hangul, Cyrillic, etc.)
_HAS_ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)


def count_ms_word(text: str) -> int:
    """Count words matching Microsoft Word's behavior (whitespace-delimited for Western & Korean; char-counted for ZH/JA)."""
    if not text:
        return 0
    cjk_count = len(_UNSPACED_CJK_CHAR_RE.findall(text))
    if cjk_count > 0:
        non_cjk = _UNSPACED_CJK_CHAR_RE.sub(" ", text)
        return cjk_count + len(_MS_WORD_TOKEN_RE.findall(non_cjk))
    return len(_MS_WORD_TOKEN_RE.findall(text))


def count_trados_studio(text: str) -> int:
    """Count words matching RWS Trados Studio 2022 word recognition rules across EN/DE/FR/KO/JA/ZH."""
    if not text:
        return 0
    cjk_count = len(_UNSPACED_CJK_CHAR_RE.findall(text))
    working = _UNSPACED_CJK_CHAR_RE.sub(" ", text) if cjk_count > 0 else text

    count = cjk_count
    for raw_tok in _STUDIO_SPLIT_RE.split(working):
        if not raw_tok:
            continue
        if _HAS_ALNUM_RE.search(raw_tok):
            count += 1
    return count


def _ends_with_abbreviation(prefix: str) -> bool:
    """Return True if text immediately preceding a period is a known patent abbreviation or list enumerator."""
    stripped = prefix.rstrip()
    if not stripped:
        return False
    tokens = stripped.split()
    if not tokens:
        return False
    last = tokens[-1].lstrip("([\"'\u201c\u2018")
    low = last.lower().rstrip(".")
    if low in _ABBREV_SET:
        return True
    # Single-letter or numeric list enumerator at very start of segment ("1.", "A.")
    if len(tokens) == 1 and (low.isdigit() or (len(low) == 1 and low.isalpha())):
        return True
    # Dotted initials like U.S. or e.g.
    if "." in low and all(len(part) <= 2 for part in low.split(".") if part):
        return True
    return False


def segment_paragraph(paragraph: str) -> list[str]:
    """Split a single paragraph into Trados-style SRX sentence segments."""
    text = re.sub(r"[ \f\v]+", " ", paragraph).strip()
    if not text:
        return []

    segments: list[str] = []
    # Separate leading [1] / [0001] paragraph tag if present
    m_para = _PARA_NUM_PREFIX_RE.match(text)
    if m_para:
        para_tag = m_para.group(1)
        segments.append(para_tag)
        text = text[m_para.end():].strip()
        if not text:
            return segments

    start = 0
    for match in _SENT_BOUNDARY_RE.finditer(text):
        if match.group(1) is not None:
            punct = match.group(1)
            punct_start = match.start(1)
            end_punct_idx = match.end(1)
            next_start_idx = match.end(2)
        else:
            punct = match.group(3)
            punct_start = match.start(3)
            end_punct_idx = match.end(3)
            next_start_idx = match.end(4)

        if punct == ".":
            prefix = text[start:punct_start]
            if _ends_with_abbreviation(prefix):
                continue
            if prefix and prefix[-1].isdigit() and next_start_idx < len(text) and text[next_start_idx].isdigit():
                continue

        seg = text[start:end_punct_idx].strip()
        if seg:
            segments.append(seg)
        start = next_start_idx

    tail = text[start:].strip()
    if tail:
        segments.append(tail)

    return segments


def segment_patent_sections(*sections: str, table_cells: list[str] | None = None) -> list[str]:
    """Segment full patent sections (Title, Abstract, Description, Claims) into ordered segments."""
    all_segments: list[str] = []
    for section in sections:
        if not section:
            continue
        for para in _PARA_SPLIT_RE.split(section):
            para = para.strip()
            if not para:
                continue
            all_segments.extend(segment_paragraph(para))

    if table_cells:
        for cell in table_cells:
            cell_clean = cell.strip()
            if cell_clean:
                all_segments.extend(segment_paragraph(cell_clean))

    return [s for s in all_segments if count_trados_studio(s) > 0]
