"""Rolling Translation Memory (TM) Simulator matching RWS Trados Studio 2022 match bands."""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Callable, Optional

from rapidfuzz import fuzz

from app.config import AppSettings
from app.models import JobSummary, PatentAnalysisRow, PatentDocument
from app.rte_detector import analyze_patent_rte
from app.segmentation import count_ms_word, count_trados_studio, segment_patent_sections

_WHITESPACE_RE = re.compile(r"\s+")
_NUM_PLACEABLE_RE = re.compile(r"\b\d+(?:[.,]\d+)*\b")
_WORD_TOK_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)


def _normalize_exact(seg: str) -> str:
    return _WHITESPACE_RE.sub(" ", seg.strip())


def _normalize_skeleton(seg: str) -> str:
    """Lowercase + collapse numbers to placeable token to catch Trados 98-99% auto-substitution matches."""
    s = _WHITESPACE_RE.sub(" ", seg.strip().lower())
    return _NUM_PLACEABLE_RE.sub("#", s)


def _extract_index_features(norm_lower: str) -> set[str]:
    """Extract distinctive word tokens and character 4-grams for fast candidate filtering."""
    feats = set(_WORD_TOK_RE.findall(norm_lower))
    if len(feats) < 3 and len(norm_lower) >= 4:
        step = max(1, len(norm_lower) // 24)
        for i in range(0, len(norm_lower) - 3, step):
            feats.add(norm_lower[i: i + 4])
    return feats


class RollingTMEngine:
    """Sequential TM simulator: analyzes each patent against prior TM, then commits its segments."""

    def __init__(self, settings: Optional[AppSettings] = None) -> None:
        self.settings = settings or AppSettings()
        self.exact_tm: set[str] = set()
        # Context store: (prev_exact, curr_exact) for 101% Context Match tracking (merged into 100% TM Match band)
        self.context_pairs: set[tuple[str, str]] = set()
        # Skeleton -> list of exact strings (for instant 98-99% placeable/case match check)
        self.skeleton_map: dict[str, str] = {}
        # Corpus list for fuzzy matching
        self.tm_segments: list[str] = []
        self.tm_lower: list[str] = []
        self.tm_lengths: list[int] = []
        # Inverted feature index: feature -> list of segment indices
        self.inv_index: dict[str, list[int]] = defaultdict(list)

    @property
    def size(self) -> int:
        return len(self.tm_segments)

    def _best_fuzzy_score(self, exact_seg: str, skel_seg: str) -> int:
        """Compute Trados-equivalent fuzzy similarity score (0..100) against current TM."""
        if not self.tm_segments:
            return 0

        # 1. O(1) exact TM match
        if exact_seg in self.exact_tm:
            return 100

        # 2. O(1) number/case placeable skeleton match -> Trados applies 1-2% auto-substitution/formatting penalty -> 98%
        if skel_seg in self.skeleton_map:
            return 98

        q_low = exact_seg.lower()
        q_len = len(q_low)
        if q_len == 0:
            return 0

        # Length bounds required for >= 50% normalized Indel/Levenshtein similarity:
        # ratio = 2*M / (L1 + L2) >= 0.50  ==>  L1/3 <= L2 <= 3*L1
        min_len = max(1, q_len // 3)
        max_len = q_len * 3

        feats = _extract_index_features(q_low)
        cand_hits: dict[int, int] = defaultdict(int)
        for f in feats:
            postings = self.inv_index.get(f)
            if postings:
                # Skip overly ubiquitous stop-features if TM is huge, unless query is short
                if len(postings) > 600 and len(feats) > 4:
                    continue
                for idx in postings:
                    cand_hits[idx] += 1

        if cand_hits:
            # Rank top 80 candidates by shared features
            top_indices = sorted(cand_hits.keys(), key=lambda i: cand_hits[i], reverse=True)[:80]
        else:
            # For very short segments without 3+ char word tokens, scan length-compatible subset (capped)
            top_indices = [
                i for i, ln in enumerate(self.tm_lengths) if min_len <= ln <= max_len
            ][:80]

        best = 0.0
        for idx in top_indices:
            ln = self.tm_lengths[idx]
            if ln < min_len or ln > max_len:
                continue
            # RapidFuzz fuzz.ratio computes 100 * (1 - IndelDistance / (L1 + L2)), closely matching Trados character/token edit similarity
            score = fuzz.ratio(q_low, self.tm_lower[idx], score_cutoff=max(50.0, best))
            if score > best:
                best = score
                if best >= 99.0:
                    break

        int_score = int(best)
        # If case-insensitive matched 100% while exact didn't, apply Trados 1% case/formatting penalty -> 99%
        if int_score >= 100:
            int_score = 99
        return int_score

    def _add_segment_to_tm(self, exact_seg: str, prev_exact: str = "") -> None:
        if prev_exact:
            self.context_pairs.add((prev_exact, exact_seg))
        if exact_seg in self.exact_tm:
            return

        idx = len(self.tm_segments)
        low = exact_seg.lower()
        skel = _normalize_skeleton(exact_seg)

        self.exact_tm.add(exact_seg)
        self.skeleton_map.setdefault(skel, exact_seg)
        self.tm_segments.append(exact_seg)
        self.tm_lower.append(low)
        self.tm_lengths.append(len(low))

        for feat in _extract_index_features(low):
            self.inv_index[feat].append(idx)

    def analyze_and_update(self, doc: PatentDocument, sequence_index: int) -> PatentAnalysisRow:
        """Analyze one patent against the current TM, then update the TM with its segments."""
        # Full text for MS Word count (Title + Abstract + Description + Claims + Table Cells)
        prose_parts = [p for p in (doc.title, doc.abstract_text, doc.description_text, doc.claims_text) if p]
        all_text_parts = prose_parts + (doc.table_cells or [])
        words_word = sum(count_ms_word(p) for p in all_text_parts)

        segments = segment_patent_sections(
            doc.title,
            doc.abstract_text,
            doc.description_text,
            doc.claims_text,
            table_cells=doc.table_cells,
        )

        repetitions = 0
        match_100 = 0
        match_95_99 = 0
        match_85_94 = 0
        match_75_84 = 0
        match_50_74 = 0
        no_match = 0

        # Track segments seen earlier WITHIN this same patent for internal Repetitions (Col E)
        seen_in_doc_exact: set[str] = set()
        seen_in_doc_skel: set[str] = set()

        normalized_doc_segments: list[str] = []

        for raw_seg in segments:
            seg_words = count_trados_studio(raw_seg)
            if seg_words <= 0:
                continue

            exact = _normalize_exact(raw_seg)
            skel = _normalize_skeleton(raw_seg)
            normalized_doc_segments.append(exact)

            # Trados Studio precedence:
            # 1) 100% / Context Match in existing TM -> Col F (100% Saving)
            # 2) Repeated segment within the current file -> Col E (Repetitions)
            # 3) Fuzzy match against TM (or internal fuzzy if higher) -> Cols G..K
            if exact in self.exact_tm:
                match_100 += seg_words
            elif exact in seen_in_doc_exact:
                repetitions += seg_words
            else:
                score = self._best_fuzzy_score(exact, skel)
                # Internal skeleton match (e.g. "The compound of claim 1, wherein..." vs "The compound of claim 2, wherein...")
                if score < 98 and skel in seen_in_doc_skel and seg_words >= 4:
                    score = 98

                if score >= 100:
                    match_100 += seg_words
                elif score >= 95:
                    match_95_99 += seg_words
                elif score >= 85:
                    match_85_94 += seg_words
                elif score >= 75:
                    match_75_84 += seg_words
                elif score >= 50:
                    match_50_74 += seg_words
                else:
                    no_match += seg_words

            seen_in_doc_exact.add(exact)
            seen_in_doc_skel.add(skel)

        words_studio = repetitions + match_100 + match_95_99 + match_85_94 + match_75_84 + match_50_74 + no_match
        if words_word == 0 and words_studio > 0:
            words_word = words_studio

        wc_pct_diff = (-1.0 * (words_word - words_studio) / words_word) if words_word > 0 else 0.0

        s = self.settings
        tm_words = (
            (repetitions * s.weight_repetitions)
            + (match_100 * s.weight_100)
            + (match_95_99 * s.weight_95_99)
            + (match_85_94 * s.weight_85_94)
            + (match_75_84 * s.weight_75_84)
            + (match_50_74 * s.weight_50_74)
            + (no_match * s.weight_no_match)
        )
        saving_words = words_studio - tm_words
        pct_saving = (saving_words / words_studio) if words_studio > 0 else 0.0

        # Detect Removable RTE (whole numbers in tables + NMR data ONLY)
        rte_breakdown = analyze_patent_rte(doc, total_words_word=words_word)

        # Commit patent's segments to the rolling TM for subsequent patents
        prev = ""
        for exact in normalized_doc_segments:
            self._add_segment_to_tm(exact, prev_exact=prev)
            prev = exact

        return PatentAnalysisRow(
            sequence_index=sequence_index,
            file_ref=doc.pub_number,
            pub_date=doc.pub_date,
            title=doc.title,
            applicant=doc.applicant,
            source_url=doc.source_url,
            words_word=words_word,
            words_studio=words_studio,
            wc_pct_diff=round(wc_pct_diff, 6),
            repetitions=repetitions,
            match_100=match_100,
            match_95_99=match_95_99,
            match_85_94=match_85_94,
            match_75_84=match_75_84,
            match_50_74=match_50_74,
            no_match=no_match,
            tm_words=round(tm_words, 2),
            saving_words=round(saving_words, 2),
            pct_saving=round(pct_saving, 6),
            tm_size_segments_after=self.size,
            rte=rte_breakdown,
        )


def compute_job_summary(rows: list[PatentAnalysisRow], include_rte: bool = False) -> JobSummary:
    """Compute portfolio-wide Trados TM & Removable RTE summary across all chronological patents."""
    if not rows:
        return JobSummary()

    total_words_word = sum(r.words_word for r in rows)
    total_words_studio = sum(r.words_studio for r in rows)
    total_rep = sum(r.repetitions for r in rows)
    total_100 = sum(r.match_100 for r in rows)
    total_95_99 = sum(r.match_95_99 for r in rows)
    total_85_94 = sum(r.match_85_94 for r in rows)
    total_75_84 = sum(r.match_75_84 for r in rows)
    total_50_74 = sum(r.match_50_74 for r in rows)
    total_no_match = sum(r.no_match for r in rows)

    reduced_tm_total = sum(r.tm_words for r in rows)
    projected_tm_saving = ((total_words_studio - reduced_tm_total) / total_words_studio) if total_words_studio > 0 else 0.0
    wc_diff = (-1.0 * (total_words_word - total_words_studio) / total_words_word) if total_words_word > 0 else 0.0

    # Mature TM saving: average over second half of chronological sequence (once TM is seeded)
    half_idx = len(rows) // 2
    second_half = rows[half_idx:] if half_idx > 0 else rows
    sh_studio = sum(r.words_studio for r in second_half)
    sh_tm = sum(r.tm_words for r in second_half)
    mature_saving = ((sh_studio - sh_tm) / sh_studio) if sh_studio > 0 else projected_tm_saving

    total_rte_words = sum(r.rte.total_rte_words for r in rows)
    total_rte_tables = sum(r.rte.table_whole_numbers for r in rows)
    total_rte_nmr = sum(r.rte.nmr_words for r in rows)
    potential_rte_pct = (total_rte_words / total_words_word) if total_words_word > 0 else 0.0

    return JobSummary(
        total_patents=len(rows),
        total_words_word=total_words_word,
        total_words_studio=total_words_studio,
        wc_diff_pct=round(wc_diff, 6),
        total_repetitions=total_rep,
        total_match_100=total_100,
        total_match_95_99=total_95_99,
        total_match_85_94=total_85_94,
        total_match_75_84=total_75_84,
        total_match_50_74=total_50_74,
        total_no_match=total_no_match,
        reduced_tm_total=round(reduced_tm_total, 2),
        projected_cumulative_tm_saving_pct=round(projected_tm_saving, 6),
        mature_tm_saving_pct=round(mature_saving, 6),
        first_patent_saving_pct=rows[0].pct_saving,
        last_patent_saving_pct=rows[-1].pct_saving,
        total_rte_words=total_rte_words,
        total_rte_table_numbers=total_rte_tables,
        total_rte_nmr_words=total_rte_nmr,
        potential_rte_saving_pct=round(potential_rte_pct, 6),
        projected_cumulative_rte_saving_pct=round(potential_rte_pct if include_rte else 0.0, 6),
    )


def run_rolling_simulation(
    patents: list[PatentDocument],
    settings: Optional[AppSettings] = None,
    include_rte: bool = False,
    on_progress: Optional[Callable[[int, int, PatentAnalysisRow], None]] = None,
) -> tuple[list[PatentAnalysisRow], JobSummary]:
    """Sort patents chronologically (oldest -> newest) and execute sequential TM building."""
    # Sort oldest -> newest so TM develops over time naturally
    ordered = sorted(patents, key=lambda d: (d.pub_date or "00000000", d.pub_number))

    engine = RollingTMEngine(settings=settings)
    rows: list[PatentAnalysisRow] = []
    cum_studio = 0
    cum_saving = 0.0
    total = len(ordered)

    for idx, doc in enumerate(ordered, start=1):
        row = engine.analyze_and_update(doc, sequence_index=idx)
        cum_studio += row.words_studio
        cum_saving += row.saving_words
        row.cumulative_pct_saving = round((cum_saving / cum_studio) if cum_studio > 0 else 0.0, 6)
        rows.append(row)
        if on_progress:
            on_progress(idx, total, row)

    summary = compute_job_summary(rows, include_rte=include_rte)
    return rows, summary

