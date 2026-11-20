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

1. **Google Patents Live Engine + 45-Day Local Smart Cache**
   - Works out of the box with **zero API keys**.
   - Single-call family-grouped search (`1 publication per patent family` so `A1`/`B1` duplicates never inflate 100% matches), polite rate-paced full-text & HTML `<table>` extraction, automatic legal-suffix handling (`PLC`, `AG`, `GmbH`, `Inc.`, `주식회사`), multi-entity selection via **Verify Entities**, and direct publication-number list pasting.
   - Persistent local SQLite cache (`data/patent_cache.db`) stores fetched specifications for 45 days so repeat runs and rate-card tests execute in `< 0.1s` with zero redundant downloads.
   - Also supports **Direct File Upload** (`.zip`, `.docx`, `.xml`, `.html`, `.txt`), **Instant Offline Demo Portfolios**, and optional **EPO OPS v3.2** credentials.

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
