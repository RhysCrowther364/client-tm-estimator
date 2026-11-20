# RWS Patent TM Suitability & RTE Savings Estimator

An internal RWS desktop and web application for automating **Trados Studio 2022 TM Suitability & Removable RTE assessments** for prospective IP clients. Enter a client name (or paste patent publication numbers / upload files), and the deterministic local engine retrieves recent patent families, sequentially builds a rolling Translation Memory (`oldest -> newest`), renders interactive TM growth curves, and exports a client-ready **RWS Branded PDF Suitability Report**.

> **Governance & Zero-AI Guarantee:** This application is 100% deterministic, rule-based Python software. It uses **zero** Artificial Intelligence, Large Language Models (LLMs), or external ML APIs at runtime.

---

## Quick Start

### 1. Run from Source (Desktop Window or Browser)

```powershell
pip install -r requirements.txt

# Launch dedicated desktop application window:
python desktop_app.py

# Or run local web server at http://127.0.0.1:8765:
python run.py
```

### 2. Build Standalone Windows `.exe` & Word User Guide

```powershell
cmd /c build_desktop.bat
```

Outputs are generated in `release/`:
- `release/RWS_Patent_TM_Estimator.exe` (single-file Windows executable with custom RWS icon)
- `release/RWS_Patent_TM_Estimator_User_Guide.docx` (RWS-branded Word quick-start guide)

---

## Key Capabilities

1. **EPO OPS v3.2 Primary Engine + Hybrid Google Patents + 45-Day Smart Cache**
   - **EPO Open Patent Services (OPS v3.2)**: Click the **&#9881; Settings** icon in the sidebar (or configure `.env`) to enter your `EPO OPS Consumer Key` and `Consumer Secret`, and click **Test EPO Connection** to verify OAuth2 access. Performs CQL applicant search with strict **1-per-INPADOC-family** deduplication, XML full-text + `<tables>` extraction, and automatic **Hybrid Google Patents full-text fallback** for non-EP publications lacking OPS full-text XML.
   - **Google Patents Live Engine**: Also works standalone with zero API keys (single-call family-grouped search + polite rate-paced waves), and serves as an automatic backup if EPO OPS quota is ever reached.
   - **45-Day Local SQLite Cache (`data/patent_cache.db`)**: Stores fetched specifications locally to conserve EPO OPS weekly quota and make repeat client runs execute in `< 0.1s`.
   - Also supports **Direct Patent Number Pasting** (`EP4200317A1, WO2025110491A1`), **Direct File Upload** (`.zip`, `.docx`, `.xml`, `.html`, `.txt`), and **Instant Offline Demo Portfolios**.

2. **Sequential Rolling TM Engine (Trados Studio 2022 Match Bands)**
   - Sorts patents **chronologically (`oldest -> newest`)** starting from a Blank TM on Patent #1.
   - Patent-aware SRX sentence segmentation (preserving `Fig. 1`, `U.S. Pat. No.`, `et al.`, `wt.%`, decimals, and `[0001]` tags) with full CJK and Korean space-delimited support.
   - Computes both **MS Word** (`Words (Word)`) and **Trados Studio 2022** (`Words (Studio)`) word counts across `Repetitions`, `100%`, `95%-99%`, `85%-94%`, `75%-84%`, `50%-74%`, and `No Match`.

3. **Live Removable RTE Toggle (Table Whole Numbers & NMR Data Only)**
   - Toggle **Include RTE** ON/OFF live on the results screen without re-running the simulation.
   - Flags standalone whole numbers inside table cells and full single-/multi-line `1H`/`13C`/`19F`/`31P` NMR chemical-shift blocks (in prose and characterization tables). When OFF, all RTE elements are completely omitted from the exported report.

4. **Client-Ready RWS Branded PDF Suitability Report**
   - Exports a multi-page widescreen vector PDF (`RWS_TM_Suitability_Report_<Client>.pdf`) styled per the RWS Brand Book with KPI cards, vector TM growth & match-band charts, payable rate summary, and a chronological patent breakdown with clickable links.

---

## Running Tests

```powershell
python -m unittest discover -s tests -v
```
