"""SQLite persistence and async background job orchestrator with live SSE progress broadcasting."""
from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import uuid
from typing import AsyncGenerator, Optional

from app.config import DATA_DIR, load_settings
from app.models import AnalysisJob, JobCreateRequest, PatentDocument
from app.patent_sources import (
    EPOOPSClient,
    GooglePatentsClient,
    _parse_direct_pub_numbers,
    generate_demo_portfolio,
    parse_uploaded_files,
)
from app.tm_engine import compute_job_summary, run_rolling_simulation

DB_PATH = DATA_DIR / "jobs.db"


class JobManager:
    """Manages SQLite-backed analysis jobs and broadcasts real-time updates to connected browsers."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._jobs_cache: dict[str, AnalysisJob] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._load_existing_jobs()

    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _load_existing_jobs(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("SELECT payload_json FROM jobs ORDER BY created_at DESC LIMIT 100")
            for (payload_json,) in cur.fetchall():
                try:
                    job = AnalysisJob.model_validate_json(payload_json)
                    # Mark any interrupted in-flight jobs from previous server run as failed
                    if job.status in ("queued", "fetching", "analyzing"):
                        job.status = "failed"
                        job.error_message = "Server restarted while job was in progress."
                    self._jobs_cache[job.id] = job
                except Exception:
                    continue

    def _persist(self, job: AnalysisJob) -> None:
        self._jobs_cache[job.id] = job
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (id, created_at, payload_json) VALUES (?, ?, ?)",
                (job.id, job.created_at, job.model_dump_json()),
            )
            conn.commit()

    def _broadcast(self, job: AnalysisJob) -> None:
        queues = self._subscribers.get(job.id)
        if not queues:
            return
        snapshot = job.model_dump_json()
        for q in list(queues):
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass

    def list_jobs(self) -> list[dict]:
        """Return lightweight job metadata sorted newest first for the sidebar history."""
        jobs = sorted(self._jobs_cache.values(), key=lambda j: j.created_at, reverse=True)
        return [
            {
                "id": j.id,
                "client_name": j.client_name,
                "language": j.language,
                "sample_size": j.sample_size,
                "data_source_used": j.data_source_used or j.data_source_requested,
                "include_rte": j.include_rte,
                "status": j.status,
                "progress_current": j.progress_current,
                "progress_total": j.progress_total,
                "created_at": j.created_at,
                "projected_tm_saving_pct": j.summary.projected_cumulative_tm_saving_pct,
                "potential_rte_saving_pct": j.summary.potential_rte_saving_pct,
                "total_patents": j.summary.total_patents,
            }
            for j in jobs
        ]

    def get_job(self, job_id: str) -> Optional[AnalysisJob]:
        return self._jobs_cache.get(job_id)

    def delete_job(self, job_id: str) -> bool:
        if job_id not in self._jobs_cache:
            return False
        del self._jobs_cache[job_id]
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
        return True

    def toggle_job_rte(self, job_id: str, include_rte: bool) -> Optional[AnalysisJob]:
        """Instantaneously toggle Removable RTE on an existing job without re-running TM simulation."""
        job = self._jobs_cache.get(job_id)
        if not job:
            return None
        job.include_rte = include_rte
        job.summary = compute_job_summary(job.rows, include_rte=include_rte)
        self._persist(job)
        return job

    async def subscribe(self, job_id: str) -> AsyncGenerator[str, None]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._subscribers.setdefault(job_id, set()).add(q)
        try:
            job = self.get_job(job_id)
            if job:
                yield job.model_dump_json()
                if job.status in ("completed", "failed"):
                    return
            while True:
                payload = await q.get()
                yield payload
                parsed_status = '"status":"completed"' in payload or '"status":"failed"' in payload
                if parsed_status:
                    break
        finally:
            self._subscribers.get(job_id, set()).discard(q)

    def start_job(self, req: JobCreateRequest) -> AnalysisJob:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        job = AnalysisJob(
            id=uuid.uuid4().hex[:10],
            client_name=req.client_name.strip(),
            applicant_variants=req.applicant_variants,
            language=req.language.upper(),
            sample_size=req.sample_size,
            data_source_requested=req.data_source,
            include_rte=req.include_rte,
            status="queued",
            progress_current=0,
            progress_total=req.sample_size,
            progress_message="Initializing background worker...",
            created_at=now,
        )
        self._persist(job)
        asyncio.create_task(self._execute_job(job, req.demo_profile))
        return job

    def start_upload_job(
        self, client_name: str, include_rte: bool, uploaded_files: list[tuple[str, bytes]]
    ) -> AnalysisJob:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        job = AnalysisJob(
            id=uuid.uuid4().hex[:10],
            client_name=client_name.strip() or "Uploaded Client Portfolio",
            language="EN",
            sample_size=len(uploaded_files),
            data_source_requested="upload",
            data_source_used="Uploaded Files",
            include_rte=include_rte,
            status="queued",
            progress_current=0,
            progress_total=len(uploaded_files),
            progress_message="Parsing uploaded files...",
            created_at=now,
        )
        self._persist(job)
        asyncio.create_task(self._execute_upload_job(job, uploaded_files))
        return job

    async def _execute_upload_job(
        self, job: AnalysisJob, uploaded_files: list[tuple[str, bytes]]
    ) -> None:
        try:
            job.status = "fetching"
            self._broadcast(job)
            docs = await asyncio.to_thread(parse_uploaded_files, uploaded_files, job.client_name)
            if not docs:
                raise RuntimeError("No supported patent documents (.docx, .xml, .html, .txt) found in upload.")
            job.sample_size = len(docs)
            job.language = (docs[0].language or job.language).upper()
            await self._run_tm_stage(job, docs)
        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            job.progress_message = f"Failed: {exc}"
            self._persist(job)
            self._broadcast(job)

    async def _execute_job(self, job: AnalysisJob, demo_profile: str = "pharma") -> None:
        settings = load_settings()
        try:
            job.status = "fetching"
            job.progress_message = f"Searching recent {job.language} patent families for {job.client_name}..."
            self._persist(job)
            self._broadcast(job)

            def _fetch_progress(curr: int, tot: int, msg: str) -> None:
                job.progress_current = curr
                job.progress_total = max(tot, 1)
                job.progress_message = msg
                self._broadcast(job)

            docs: list[PatentDocument] = []
            req_src = job.data_source_requested

            if req_src == "demo":
                job.data_source_used = f"Demo Corpus ({demo_profile.title()})"
                docs = await asyncio.to_thread(
                    generate_demo_portfolio, job.client_name, job.sample_size, demo_profile
                )
            elif req_src == "epo_ops" or (
                req_src == "auto" and settings.epo_ops_key and settings.epo_ops_secret
            ):
                epo = EPOOPSClient(settings.epo_ops_key, settings.epo_ops_secret)
                try:
                    job.data_source_used = "EPO OPS v3.2 (1 per INPADOC Family)"
                    docs = await epo.fetch_patents(
                        client_name=job.client_name,
                        applicant_variants=job.applicant_variants,
                        sample_size=job.sample_size,
                        language=job.language,
                        on_progress=_fetch_progress,
                    )
                except Exception as epo_err:
                    if req_src == "epo_ops":
                        raise
                    # Auto-fallback to Google Patents if EPO OPS credentials fail or quota exceeded
                    gp = GooglePatentsClient()
                    job.data_source_used = "Google Patents Live (EPO Fallback)"
                    job.progress_message = f"EPO OPS unavailable ({epo_err}); switching to live Google Patents..."
                    self._broadcast(job)
                    docs = await gp.fetch_patents(
                        client_name=job.client_name,
                        applicant_variants=job.applicant_variants,
                        sample_size=job.sample_size,
                        language=job.language,
                        on_progress=_fetch_progress,
                    )
            else:
                gp = GooglePatentsClient()
                is_direct_list = bool(_parse_direct_pub_numbers(job.client_name))
                job.data_source_used = (
                    "Direct Patent List (Google Patents)"
                    if is_direct_list
                    else "Google Patents Live (1 per Family)"
                )
                docs = await gp.fetch_patents(
                    client_name=job.client_name,
                    applicant_variants=job.applicant_variants,
                    sample_size=job.sample_size,
                    language=job.language,
                    on_progress=_fetch_progress,
                )

            if not docs:
                raise RuntimeError(f"No full-text patents retrieved for '{job.client_name}'.")

            await self._run_tm_stage(job, docs)

        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            job.progress_message = f"Error: {exc}"
            self._persist(job)
            self._broadcast(job)

    async def _run_tm_stage(self, job: AnalysisJob, docs: list[PatentDocument]) -> None:
        settings = load_settings()
        job.status = "analyzing"
        job.progress_current = 0
        job.progress_total = len(docs)
        job.progress_message = f"Running sequential Trados TM simulation across {len(docs)} chronological patents..."
        job.rows = []
        self._persist(job)
        self._broadcast(job)

        loop = asyncio.get_running_loop()

        def _apply_step_on_loop(idx: int, tot: int, row) -> None:
            job.progress_current = idx
            job.progress_total = tot
            job.progress_message = (
                f"Analyzed & updated TM [{idx}/{tot}]: {row.file_ref} "
                f"({row.pct_saving * 100:.1f}% TM saving)"
            )
            job.rows.append(row)
            job.summary = compute_job_summary(job.rows, include_rte=job.include_rte)
            self._broadcast(job)

        def _sim_worker() -> tuple[list, object]:
            def _on_step(idx: int, tot: int, row) -> None:
                loop.call_soon_threadsafe(_apply_step_on_loop, idx, tot, row)

            return run_rolling_simulation(
                patents=docs,
                settings=settings,
                include_rte=job.include_rte,
                on_progress=_on_step,
            )

        rows, summary = await asyncio.to_thread(_sim_worker)
        job.rows = rows
        job.summary = summary
        job.status = "completed"
        job.completed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        job.progress_current = len(rows)
        job.progress_total = len(rows)
        job.progress_message = (
            f"Completed {len(rows)} patents — Projected Cumulative TM Saving: "
            f"{summary.projected_cumulative_tm_saving_pct * 100:.2f}%"
        )
        self._persist(job)
        self._broadcast(job)
