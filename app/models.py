"""Data models for Patent TM Estimation jobs, patent documents, and analysis results."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class PatentDocument(BaseModel):
    pub_number: str
    family_id: str
    pub_date: str = ""  # YYYY-MM-DD or YYYYMMDD
    title: str = ""
    applicant: str = ""
    language: str = "EN"
    abstract_text: str = ""
    description_text: str = ""
    claims_text: str = ""
    # Preserved table cells (list of cell text strings) when extracted from XML/HTML
    table_cells: list[str] = Field(default_factory=list)
    source_url: str = ""


class RTEBreakdown(BaseModel):
    table_whole_numbers: int = 0
    nmr_words: int = 0
    total_rte_words: int = 0
    nmr_spans_count: int = 0
    tables_count: int = 0


class PatentAnalysisRow(BaseModel):
    sequence_index: int  # 1..N (chronological order: 1 = oldest in sample, N = most recent)
    file_ref: str
    pub_date: str = ""
    title: str = ""
    applicant: str = ""
    source_url: str = ""
    words_word: int = 0
    words_studio: int = 0
    wc_pct_diff: float = 0.0
    repetitions: int = 0
    match_100: int = 0
    match_95_99: int = 0
    match_85_94: int = 0
    match_75_84: int = 0
    match_50_74: int = 0
    no_match: int = 0
    tm_words: float = 0.0
    saving_words: float = 0.0
    pct_saving: float = 0.0
    cumulative_pct_saving: float = 0.0
    tm_size_segments_after: int = 0
    rte: RTEBreakdown = Field(default_factory=RTEBreakdown)


class JobCreateRequest(BaseModel):
    client_name: str
    applicant_variants: list[str] = Field(default_factory=list)
    sample_size: int = Field(default=30, ge=1, le=80)
    language: str = "EN"
    data_source: Literal["auto", "epo_ops", "google_patents", "demo"] = "auto"
    demo_profile: Literal["pharma", "medtech", "automotive"] = "pharma"
    include_rte: bool = False


class ApplicantVariant(BaseModel):
    name: str
    hit_count: int = 1


class ApplicantSearchResponse(BaseModel):
    query: str
    source_used: str
    total_found: int
    variants: list[ApplicantVariant]
    note: str = ""


class JobSummary(BaseModel):
    total_patents: int = 0
    total_words_word: int = 0
    total_words_studio: int = 0
    wc_diff_pct: float = 0.0
    total_repetitions: int = 0
    total_match_100: int = 0
    total_match_95_99: int = 0
    total_match_85_94: int = 0
    total_match_75_84: int = 0
    total_match_50_74: int = 0
    total_no_match: int = 0
    reduced_tm_total: float = 0.0
    projected_cumulative_tm_saving_pct: float = 0.0
    # Mature TM saving (average over second half of sample once TM has warmed up)
    mature_tm_saving_pct: float = 0.0
    first_patent_saving_pct: float = 0.0
    last_patent_saving_pct: float = 0.0
    # RTE metrics (computed for both toggle states; UI can flip include_rte instantaneously)
    total_rte_words: int = 0
    total_rte_table_numbers: int = 0
    total_rte_nmr_words: int = 0
    potential_rte_saving_pct: float = 0.0
    projected_cumulative_rte_saving_pct: float = 0.0


class AnalysisJob(BaseModel):
    id: str
    client_name: str
    applicant_variants: list[str] = Field(default_factory=list)
    language: str = "EN"
    sample_size: int = 30
    data_source_requested: str = "auto"
    data_source_used: str = ""
    include_rte: bool = False
    status: Literal["queued", "fetching", "analyzing", "completed", "failed"] = "queued"
    progress_current: int = 0
    progress_total: int = 0
    progress_message: str = "Queued..."
    error_message: Optional[str] = None
    created_at: str = ""
    completed_at: Optional[str] = None
    rows: list[PatentAnalysisRow] = Field(default_factory=list)
    summary: JobSummary = Field(default_factory=JobSummary)

