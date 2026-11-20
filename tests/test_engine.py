"""Automated verification suite for segmentation, strict RTE detection, rolling TM simulation, and RWS PDF report export."""
from __future__ import annotations

import asyncio
import io
import unittest

from starlette.testclient import TestClient

from app.main import app, job_manager
from app.models import AnalysisJob, PatentDocument
from app.patent_sources import generate_demo_portfolio
from app.pdf_exporter import export_job_to_pdf
from app.rte_detector import analyze_patent_rte, count_whole_numbers_in_cells, extract_nmr_spans
from app.segmentation import count_ms_word, count_trados_studio, segment_paragraph
from app.tm_engine import run_rolling_simulation


class PatentTMEstimatorTests(unittest.TestCase):
    def test_patent_segmentation_preserves_abbreviations(self) -> None:
        para = (
            "[0012] Referring to Fig. 1 and U.S. Pat. No. 9,123,456 (Smith et al.), "
            "approx. 5.5 wt.% of catalyst is added at 25.0 C. The mixture is stirred for 2 hrs."
        )
        segs = segment_paragraph(para)
        # Expect: ['[0012]', 'Referring to Fig. 1 ... at 25.0 C.', 'The mixture is stirred for 2 hrs.']
        self.assertEqual(len(segs), 3, f"Unexpected segments: {segs}")
        self.assertEqual(segs[0], "[0012]")
        self.assertIn("Fig. 1", segs[1])
        self.assertIn("25.0 C.", segs[1])
        self.assertEqual(segs[2], "The mixture is stirred for 2 hrs.")

    def test_word_counters_word_vs_studio(self) -> None:
        sample = "A water-soluble cis/trans isomer (100 mg)"
        # MS Word: A | water-soluble | cis/trans | isomer | (100 | mg) -> 6 tokens
        self.assertEqual(count_ms_word(sample), 6)
        # Trados Studio splits 'water-soluble' (2) and 'cis/trans' (2) -> 8 tokens
        self.assertEqual(count_trados_studio(sample), 8)

    def test_rte_whole_numbers_in_tables_only(self) -> None:
        cells = [
            "Example 1",      # 'Example' (no), '1' (YES -> 1)
            "42",             # YES -> 1
            "1,250",          # YES -> 1
            "-15",            # YES -> 1
            "3.14",           # Decimal -> NO
            "50%",            # Percentage -> NO
            "100mg",          # Alphanumeric -> NO
            "Compound A-2",   # Text -> NO
        ]
        self.assertEqual(count_whole_numbers_in_cells(cells), 4)

    def test_rte_nmr_detector_strictness(self) -> None:
        text = (
            "The structure was confirmed by 1H NMR spectroscopy prior to assay testing. "
            "Compound 12 was isolated as a white solid. "
            "1H NMR (400 MHz, DMSO-d6) δ 8.64 (d, J = 2.1 Hz, 1H), 7.91 (dd, J = 8.4, 2.1 Hz, 2H), "
            "3.88 (s, 3H), 1.25 (t, J = 7.0 Hz, 3H). "
            "13C NMR (101 MHz, DMSO-d6) δ 166.2, 154.1, 139.8, 122.4, 55.1, 14.3. "
            "LCMS (ESI) m/z: 342.1 [M+H]+. To a solution of Intermediate 2 in THF was added NaH."
        )
        spans = extract_nmr_spans(text)
        self.assertEqual(len(spans), 2, f"Expected 2 NMR spans (1H and 13C), got: {spans}")
        self.assertTrue(spans[0].startswith("1H NMR"))
        self.assertNotIn("LCMS", spans[0])
        self.assertTrue(spans[1].startswith("13C NMR"))
        self.assertNotIn("LCMS", spans[1])
        self.assertNotIn("Intermediate", spans[1])

        doc = PatentDocument(
            pub_number="EP1000001A1",
            family_id="F1",
            description_text=text,
            table_cells=["Header", "10", "250", "3.5", "Active"],
        )
        rte = analyze_patent_rte(doc)
        self.assertEqual(rte.table_whole_numbers, 2)
        self.assertGreater(rte.nmr_words, 20)
        self.assertEqual(rte.total_rte_words, rte.table_whole_numbers + rte.nmr_words)

    def test_rolling_tm_simulation_and_pdf_report_export(self) -> None:
        docs = generate_demo_portfolio("Novartis AG", sample_size=12, profile="pharma")
        rows, summary = run_rolling_simulation(docs, include_rte=True)

        self.assertEqual(len(rows), 12)
        # Patent #1 starts against a Blank TM -> 0 words in 100% TM match!
        self.assertEqual(rows[0].match_100, 0)
        # Later patents build substantial 100% and fuzzy TM matches!
        self.assertGreater(rows[-1].match_100, 50)
        self.assertGreater(rows[-1].pct_saving, rows[0].pct_saving)

        # Every row's Studio words must equal exact sum of match bands E..K
        for r in rows:
            band_sum = (
                r.repetitions + r.match_100 + r.match_95_99 + r.match_85_94
                + r.match_75_84 + r.match_50_74 + r.no_match
            )
            self.assertEqual(r.words_studio, band_sum)

        job = AnalysisJob(
            id="testjob01",
            client_name="Novartis AG",
            sample_size=12,
            include_rte=True,
            status="completed",
            created_at="2026-11-18T10:00:00+00:00",
            rows=rows,
            summary=summary,
        )

        # Verify RWS Branded PDF Report generation with RTE OFF vs ON + Korean Unicode client names
        pdf_off = export_job_to_pdf(job, include_rte=False)
        self.assertTrue(pdf_off.startswith(b"%PDF-1."))
        self.assertGreater(len(pdf_off), 15_000)

        pdf_on = export_job_to_pdf(job, include_rte=True)
        self.assertTrue(pdf_on.startswith(b"%PDF-1."))
        self.assertGreater(len(pdf_on), 15_000)

        # Verify Korean/Unicode client rendering in PDF + HTTP download endpoint
        job_ko = job.model_copy(update={"id": "testjobko", "client_name": "CJ CheilJedang (씨제이제일제당)"})
        job_manager._jobs_cache["testjobko"] = job_ko
        try:
            with TestClient(app) as client:
                resp = client.get("/api/jobs/testjobko/report.pdf?include_rte=true")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers["content-type"], "application/pdf")
                self.assertTrue(resp.content.startswith(b"%PDF-1."))
        finally:
            job_manager._jobs_cache.pop("testjobko", None)

    def test_korean_word_counting_and_segmentation(self) -> None:
        ko_para = "[1] 본 개시는 신규한 프로모터 및 이를 이용한 목적 산물을 생산하는 방법에 관한 것이다. 제6항의 미생물을 배지에서 배양하는 단계를 포함한다."
        segs = segment_paragraph(ko_para)
        self.assertEqual(len(segs), 3, f"Expected [1] + 2 Korean sentences, got: {segs}")
        # Korean is space-delimited in MS Word and Trados Studio (NOT character-counted like Chinese)
        s1_words_word = count_ms_word(segs[1])
        s1_words_studio = count_trados_studio(segs[1])
        self.assertEqual(s1_words_word, 13)
        self.assertEqual(s1_words_studio, 13)

    def test_rte_nmr_full_tail_multiline_html_and_tables(self) -> None:
        from bs4 import BeautifulSoup
        from app.patent_sources import _extract_bs4_section_with_tables

        # 1) Attached 'ppm', 'sextet', spaced 'CDCl 3', and inline Korean particle after closing ')'
        ko_nmr = (
            "[95]\n"
            "1H NMR (D2O, 80 MHz) : 3.85-3.36ppm (2H, m), 2.76-2.33ppm (1H, sextet), 1.13-1.04ppm (3H, d).\n"
            "[117]\n"
            "피크는 1H NMR (CDCl 3, 400 MHz) : 4.28-4.21ppm (2H, m), 2.82-2.74ppm (1H, sextet), 1.23-1.17 (3H, d)으로 확인할 수 있었다."
        )
        spans = extract_nmr_spans(ko_nmr)
        self.assertEqual(len(spans), 2, f"Expected 2 full NMR spans, got: {spans}")
        self.assertTrue(spans[0].endswith("(3H, d)."), f"Truncated span[0]: {spans[0]}")
        self.assertTrue(spans[1].endswith("(3H, d)"), f"Truncated span[1]: {spans[1]}")

        # 2) HTML <sup>1</sup>H-NMR and CDCl<sub>3</sub> + multi-line δ block + OLED characterization table
        html = (
            "<section>"
            "<p>[0095] <sup>1</sup>H-NMR (300 MHz, CDCl<sub>3</sub>)<br/>"
            "<i>δ</i> = 8.55 (1H, d, <i>J</i> = 8.0 Hz), 8.12 (2H, d), 7.94-7.32 (15H, m). FD-MS: m/z = 512.20</p>"
            "<p>[0096] Optical loss tan δ = 0.025 and color shift ΔE = 1.45.</p>"
            "<table>"
            "<tr><th>화합물</th><th><sup>1</sup>H-NMR (CDCl<sub>3</sub>)</th><th>FD-MS</th></tr>"
            "<tr><td>1</td><td>δ = 8.55 (1H, d), 8.12 (2H, d), 7.94-7.32 (15H, m)</td><td>m/z=512.20</td></tr>"
            "<tr><td>2</td><td>8.41 (2H, d), 7.89 (4H, m), 7.60-7.25 (14H, m)</td><td>m/z=588.23</td></tr>"
            "</table>"
            "</section>"
        )
        soup = BeautifulSoup(html, "lxml")
        prose, cells = _extract_bs4_section_with_tables(soup.find("section"))
        doc = PatentDocument(pub_number="KR20250012680A", family_id="F2", description_text=prose, table_cells=cells)
        rte = analyze_patent_rte(doc)
        # Prose span + 2 NMR table cells = 3 NMR spans; 'tan δ = 0.025' and 'ΔE = 1.45' and 'FD-MS' must NOT be tagged!
        self.assertEqual(rte.nmr_spans_count, 3)
        self.assertGreaterEqual(rte.nmr_words, 35)
        self.assertEqual(rte.table_whole_numbers, 2)  # Compound IDs '1' and '2' only

    def test_google_patents_enhancements_and_local_cache(self) -> None:
        from app.patent_cache import (
            clear_patent_cache,
            get_cache_stats,
            get_cached_patent,
            put_cached_patent,
        )
        from app.patent_sources import (
            EPOOPSClient,
            GooglePatentsClient,
            _extract_gp_family_id,
            _parse_direct_pub_numbers,
            _strip_legal_suffix,
        )

        # 0) EPO OPS applicant country-tag normalization & CQL sanitization
        self.assertEqual(EPOOPSClient._clean_epo_applicant_name("ASTRAZENECA AB\u2002[SE]"), "ASTRAZENECA AB")
        self.assertEqual(EPOOPSClient._sanitize_cql_term('Siemens "AG" [DE]'), "Siemens AG")

        # 1) URL-encoding of '&' and multi-entity batching in Google Patents XHR query param
        q_param = GooglePatentsClient._build_search_url_param(
            ["Procter & Gamble", "The Procter & Gamble Company"], "WO,EP,US,GB,CA,AU", "ENGLISH", num=100, page=0
        )
        self.assertIn("assignee=Procter+%26+Gamble,The+Procter+%26+Gamble+Company", q_param)
        self.assertIn("num=100", q_param)
        self.assertNotIn("dups=language", q_param)

        # 2) Corporate legal suffix stripping (English, European, and Korean/Japanese prefixes/suffixes)
        self.assertEqual(_strip_legal_suffix("AstraZeneca PLC"), "AstraZeneca")
        self.assertEqual(_strip_legal_suffix("Siemens Aktiengesellschaft"), "Siemens")
        self.assertEqual(_strip_legal_suffix("주식회사 엘티소재"), "엘티소재")
        self.assertEqual(_strip_legal_suffix("CJ CheilJedang Corp."), "CJ CheilJedang")

        # 3) Direct patent publication number list detection
        self.assertEqual(
            _parse_direct_pub_numbers("WO2025110491A1, ep4200317a1; US20240123456A1"),
            ["WO2025110491A1", "EP4200317A1", "US20240123456A1"],
        )
        self.assertEqual(_parse_direct_pub_numbers("AstraZeneca PLC"), [])

        # 4) Family ID extraction from XHR metadata
        self.assertEqual(
            _extract_gp_family_id({"family_metadata": {"aggregated": {"family_id": "89123456"}}}),
            "89123456",
        )

        # 5) Local SQLite patent cache round-trip & API settings integration
        test_doc = PatentDocument(
            pub_number="WO2099999999A1",
            family_id="FAM999",
            pub_date="20250101",
            title="Test Cached Specification",
            applicant="Test Corp",
            language="EN",
            description_text="A pharmaceutical composition comprising compound A.",
            claims_text="1. A composition comprising compound A.",
        )
        put_cached_patent(test_doc)
        try:
            loaded = get_cached_patent("wo2099999999a1", "en")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.family_id, "FAM999")
            stats = get_cache_stats()
            self.assertGreaterEqual(stats["cached_patents"], 1)

            with TestClient(app) as client:
                s_resp = client.get("/api/settings")
                self.assertEqual(s_resp.status_code, 200)
                self.assertGreaterEqual(s_resp.json()["cached_patents"], 1)
        finally:
            from contextlib import closing
            from app.patent_cache import _connect
            with closing(_connect()) as conn:
                conn.execute("DELETE FROM patent_docs WHERE cache_key = 'WO2099999999A1:EN'")
                conn.commit()


if __name__ == "__main__":
    unittest.main()

