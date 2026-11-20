"""Persistent SQLite cache for fetched patent full-text documents and recent applicant searches.

Minimizes redundant HTTP requests to Google Patents across repeat runs, weight adjustments,
applicant entity checks, and expanded sample sizes.
"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import time
from typing import Optional

from app.config import DATA_DIR
from app.models import PatentDocument

CACHE_DB_PATH = DATA_DIR / "patent_cache.db"

# Default TTLs
PATENT_DOC_TTL_SEC = 60 * 60 * 24 * 45  # 45 days (published patent specifications are immutable)
SEARCH_TTL_SEC = 60 * 60 * 12           # 12 hours for search result lists


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS patent_docs (
            cache_key TEXT PRIMARY KEY,
            fetched_at REAL NOT NULL,
            doc_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_queries (
            query_key TEXT PRIMARY KEY,
            fetched_at REAL NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    return conn


def get_cached_patent(pub_number: str, language: str) -> Optional[PatentDocument]:
    key = f"{pub_number.strip().upper()}:{language.strip().upper()}"
    now = time.time()
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                "SELECT fetched_at, doc_json FROM patent_docs WHERE cache_key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            fetched_at, doc_json = row
            if now - float(fetched_at) > PATENT_DOC_TTL_SEC:
                return None
            return PatentDocument.model_validate_json(doc_json)
    except Exception:
        return None


def put_cached_patent(doc: PatentDocument) -> None:
    if not doc or not doc.pub_number:
        return
    key = f"{doc.pub_number.strip().upper()}:{(doc.language or 'EN').strip().upper()}"
    try:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO patent_docs (cache_key, fetched_at, doc_json) VALUES (?, ?, ?)",
                (key, time.time(), doc.model_dump_json()),
            )
            conn.commit()
    except Exception:
        pass


def get_cached_search(query_key: str, max_age_sec: float = SEARCH_TTL_SEC) -> Optional[dict]:
    now = time.time()
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                "SELECT fetched_at, payload_json FROM search_queries WHERE query_key = ?",
                (query_key,),
            ).fetchone()
            if not row:
                return None
            fetched_at, payload_json = row
            if now - float(fetched_at) > max_age_sec:
                return None
            return json.loads(payload_json)
    except Exception:
        return None


def put_cached_search(query_key: str, payload: dict) -> None:
    try:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO search_queries (query_key, fetched_at, payload_json) VALUES (?, ?, ?)",
                (query_key, time.time(), json.dumps(payload)),
            )
            conn.commit()
    except Exception:
        pass


def get_cache_stats() -> dict[str, int]:
    try:
        with closing(_connect()) as conn:
            p_cnt = conn.execute("SELECT COUNT(*) FROM patent_docs").fetchone()[0]
            s_cnt = conn.execute("SELECT COUNT(*) FROM search_queries").fetchone()[0]
            return {"cached_patents": int(p_cnt or 0), "cached_searches": int(s_cnt or 0)}
    except Exception:
        return {"cached_patents": 0, "cached_searches": 0}


def clear_patent_cache() -> None:
    try:
        with closing(_connect()) as conn:
            conn.execute("DELETE FROM patent_docs")
            conn.execute("DELETE FROM search_queries")
            conn.commit()
    except Exception:
        pass
