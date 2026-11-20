"""Generates desktop release assets:
1. Downloads and vendors Chart.js locally into static/vendor/chart.umd.min.js (offline-safe)
2. Renders the RWS 2025 Brand Icon (assets/rws_icon.ico) using Pillow
3. Generates the RWS-branded Word User Guide (release/RWS_Patent_TM_Estimator_User_Guide.docx)
"""
from __future__ import annotations

from pathlib import Path
import httpx
from PIL import Image, ImageDraw
import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "static" / "vendor"
ASSETS_DIR = ROOT / "assets"
RELEASE_DIR = ROOT / "release"

# Official RWS 2025 Brand Palette
RWS_GALAXY = RGBColor(0x38, 0x00, 0x6B)
RWS_PINK = RGBColor(0xE6, 0x00, 0x50)
RWS_BLUE = RGBColor(0x7B, 0x5C, 0xF5)
RWS_INK = RGBColor(0x0B, 0x0B, 0x0F)
RWS_MUTED = RGBColor(0x5B, 0x58, 0x66)


def vendor_chartjs() -> None:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    target = VENDOR_DIR / "chart.umd.min.js"
    if target.exists() and target.stat().st_size > 50_000:
        print("Chart.js already vendored:", target)
        return
    resp = httpx.get("https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js", timeout=25.0)
    resp.raise_for_status()
    target.write_bytes(resp.content)
    print("Vendored Chart.js:", target, len(resp.content), "bytes")


def generate_rws_ico() -> Path:
    from PIL import ImageChops, ImageFilter

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    S = 1024  # 4x supersampling for ultra-clean anti-aliased curves and edges

    # 1. Base Galaxy Purple tile with subtle RWS Brand Book ambient glow
    tile = Image.new("RGBA", (S, S), (36, 0, 71, 255))
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([(-120, 420), (620, 1140)], fill=(123, 92, 245, 95))   # Hidcote Blue bottom-left glow
    gd.ellipse([(420, -140), (1140, 580)], fill=(230, 0, 80, 110))    # Hillier Pink top-right glow
    glow = glow.filter(ImageFilter.GaussianBlur(radius=95))
    tile = Image.alpha_composite(tile, glow)

    # Apply rounded Windows 11 squircle mask to tile
    tile_mask = Image.new("L", (S, S), 0)
    tmd = ImageDraw.Draw(tile_mask)
    tmd.rounded_rectangle([(44, 44), (980, 980)], radius=216, fill=255)

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(tile, (0, 0), mask=tile_mask)

    # Subtle inner border highlight on the Galaxy Purple tile
    rim_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rim_layer)
    rd.rounded_rectangle([(46, 46), (978, 978)], radius=214, outline=(255, 255, 255, 42), width=5)
    img = Image.alpha_composite(img, rim_layer)

    # 2. Patent Document Sheet (with folded corner + drop shadow)
    doc_x1, doc_y1, doc_x2, doc_y2 = 175, 140, 690, 815
    fold = 125

    doc_poly = [
        (doc_x1 + 42, doc_y1),
        (doc_x2 - fold, doc_y1),
        (doc_x2, doc_y1 + fold),
        (doc_x2, doc_y2 - 42),
        (doc_x1, doc_y2 - 42),
    ]

    def _draw_doc_shape(drw: ImageDraw.ImageDraw, fill_rgba: tuple[int, int, int, int], offset: tuple[int, int] = (0, 0)) -> None:
        ox, oy = offset
        # Main rounded body minus top-right corner cut
        body = Image.new("L", (S, S), 0)
        bd = ImageDraw.Draw(body)
        bd.rounded_rectangle([(doc_x1 + ox, doc_y1 + oy), (doc_x2 + ox, doc_y2 + oy)], radius=44, fill=255)
        # Cut top-right triangle for dog-ear fold
        bd.polygon(
            [(doc_x2 - fold + ox, doc_y1 - 10 + oy), (doc_x2 + 10 + ox, doc_y1 - 10 + oy), (doc_x2 + 10 + ox, doc_y1 + fold + oy)],
            fill=0,
        )
        solid = Image.new("RGBA", (S, S), fill_rgba)
        return solid, body

    # Document drop shadow
    sh_solid, sh_mask = _draw_doc_shape(None, (14, 0, 32, 145), offset=(14, 24))
    sh_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh_layer.paste(sh_solid, (0, 0), mask=sh_mask)
    sh_layer = sh_layer.filter(ImageFilter.GaussianBlur(radius=26))
    img = Image.alpha_composite(img, sh_layer)

    # Crisp white document body
    doc_solid, doc_mask = _draw_doc_shape(None, (255, 255, 255, 255))
    doc_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    doc_layer.paste(doc_solid, (0, 0), mask=doc_mask)

    dd = ImageDraw.Draw(doc_layer)
    # Folded dog-ear flap shadow & flap
    dd.polygon(
        [(doc_x2 - fold, doc_y1 + fold), (doc_x2, doc_y1 + fold), (doc_x2 - fold, doc_y1 + fold + 26)],
        fill=(200, 192, 224, 150),
    )
    dd.polygon(
        [(doc_x2 - fold, doc_y1), (doc_x2 - fold, doc_y1 + fold), (doc_x2, doc_y1 + fold)],
        fill=(228, 222, 246, 255),
    )

    # Stylized patent specification header lines at top of document
    dd.ellipse([(232, 206), (264, 238)], fill=(230, 0, 80, 255))  # RWS Pink header bullet
    dd.rounded_rectangle([(284, 210), (505, 234)], radius=12, fill=(56, 0, 107, 225))
    dd.rounded_rectangle([(232, 266), (595, 288)], radius=11, fill=(208, 202, 228, 255))
    dd.rounded_rectangle([(232, 310), (535, 332)], radius=11, fill=(208, 202, 228, 255))

    # 3. Ascending Bar & Line Graph on the Document (positioned so the peak sits squarely inside the magnifier lens!)
    dd.rounded_rectangle([(226, 728), (642, 740)], radius=6, fill=(214, 208, 232, 255))

    bars = [
        (246, 646, 310, 728, (168, 148, 252, 255)),
        (344, 590, 408, 728, (123, 92, 245, 255)),
        (442, 526, 506, 728, (123, 92, 245, 255)),
        (540, 452, 604, 728, (56, 0, 107, 255)),
    ]
    for bx1, by1, bx2, by2, bcol in bars:
        dd.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=14, fill=bcol)

    # Ascending Hillier Pink TM growth curve + nodes (peak node at (572, 412) centered inside lens!)
    pts = [(278, 610), (376, 550), (474, 484), (572, 410)]
    for i in range(len(pts) - 1):
        dd.line([pts[i], pts[i + 1]], fill=(230, 0, 80, 255), width=22)
    for px, py in pts:
        dd.ellipse([(px - 22, py - 22), (px + 22, py + 22)], fill=(230, 0, 80, 255))
        dd.ellipse([(px - 10, py - 10), (px + 10, py + 10)], fill=(255, 255, 255, 255))

    img = Image.alpha_composite(img, doc_layer)

    # 4. Foreground Magnifying Glass Focusing Over the Graph Peak
    cx, cy, r_lens = 568, 540, 196

    # Magnifier drop shadow (cast ONLY outside the lens interior so the white page inside stays crisp & bright!)
    mag_sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    msd = ImageDraw.Draw(mag_sh)
    msd.line([(cx + 132, cy + 132), (866, 830)], fill=(14, 0, 32, 175), width=76)
    msd.ellipse([(828, 792), (904, 868)], fill=(14, 0, 32, 175))
    msd.ellipse([(cx - r_lens + 8, cy - r_lens + 20), (cx + r_lens + 8, cy + r_lens + 20)], outline=(14, 0, 32, 155), width=44)
    mag_sh = mag_sh.filter(ImageFilter.GaussianBlur(radius=20))
    img = Image.alpha_composite(img, mag_sh)

    # Clean 1.15x optical magnification of the sharp un-shadowed document + graph inside the lens!
    clean_canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    clean_canvas.paste(tile, (0, 0), mask=tile_mask)
    clean_canvas = Image.alpha_composite(clean_canvas, doc_layer)

    crop_r = int(r_lens / 1.15)
    zoomed_patch = clean_canvas.crop((cx - crop_r, cy - crop_r, cx + crop_r, cy + crop_r)).resize(
        (r_lens * 2, r_lens * 2), Image.Resampling.LANCZOS
    )
    lens_mask = Image.new("L", (r_lens * 2, r_lens * 2), 0)
    ImageDraw.Draw(lens_mask).ellipse([(0, 0), (r_lens * 2, r_lens * 2)], fill=255)
    img.paste(zoomed_patch, (cx - r_lens, cy - r_lens), mask=lens_mask)

    # Glass tint + glossy specular arc + handle + Hillier Pink & White precision bezel
    mag_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mld = ImageDraw.Draw(mag_layer)

    # Subtle cool glass sheen + specular highlight inside lens
    mld.ellipse([(cx - r_lens, cy - r_lens), (cx + r_lens, cy + r_lens)], fill=(235, 242, 255, 24))
    mld.arc(
        [(cx - r_lens + 34, cy - r_lens + 34), (cx + r_lens - 34, cy + r_lens - 34)],
        start=198,
        end=258,
        fill=(255, 255, 255, 195),
        width=18,
    )

    # Flush handle stem + ergonomic Hillier Pink grip with rounded cap
    hx1, hy1 = cx + 148, cy + 148
    hx2, hy2 = 856, 828
    mld.line([(hx1 - 14, hy1 - 14), (hx1 + 26, hy1 + 26)], fill=(255, 255, 255, 255), width=42)
    mld.line([(hx1 + 28, hy1 + 28), (hx2, hy2)], fill=(230, 0, 80, 255), width=74)
    mld.ellipse([(hx2 - 37, hy2 - 37), (hx2 + 37, hy2 + 37)], fill=(230, 0, 80, 255))
    mld.ellipse([(hx1 + 28 - 37, hy1 + 28 - 37), (hx1 + 28 + 37, hy1 + 28 + 37)], fill=(230, 0, 80, 255))

    # Outer Hillier Pink rim + crisp inner White ring
    mld.ellipse(
        [(cx - r_lens - 18, cy - r_lens - 18), (cx + r_lens + 18, cy + r_lens + 18)],
        outline=(230, 0, 80, 255),
        width=44,
    )
    mld.ellipse(
        [(cx - r_lens + 8, cy - r_lens + 8), (cx + r_lens - 8, cy + r_lens - 8)],
        outline=(255, 255, 255, 255),
        width=14,
    )

    img = Image.alpha_composite(img, mag_layer)

    # Ensure outer squircle boundary stays clean
    final_alpha = ImageChops.multiply(img.getchannel("A"), tile_mask)
    # Also allow magnifier handle tip if inside tile (already well inside [44..980] since max is 893!)
    img.putalpha(final_alpha)

    ico_path = ASSETS_DIR / "rws_icon.ico"
    img_256 = img.resize((256, 256), Image.Resampling.LANCZOS)
    img_256.save(ico_path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])

    png_path = ASSETS_DIR / "rws_icon.png"
    img.resize((512, 512), Image.Resampling.LANCZOS).save(png_path, format="PNG")

    static_icon = ROOT / "static" / "icon.png"
    img.resize((128, 128), Image.Resampling.LANCZOS).save(static_icon, format="PNG")
    print("Generated RWS custom icon:", ico_path, "and", static_icon)
    return png_path


def _set_cell_shading(cell, hex_color: str) -> None:
    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{hex_color}"/>')
    cell._tc.get_or_add_tcPr().append(shading)


def _set_cell_margins(cell, top=120, bottom=120, left=160, right=160) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = OxmlElement("w:tcMar")
    for m, val in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        node = OxmlElement(f"w:{m}")
        node.set(qn("w:w"), str(val))
        node.set(qn("w:type"), "dxa")
        tcMar.append(node)
    tcPr.append(tcMar)


def _add_rws_heading(doc: docx.Document, num_str: str, title: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(16)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.keep_with_next = True

    r_num = p.add_run(f"{num_str}  ")
    r_num.bold = True
    r_num.font.name = "Segoe UI"
    r_num.font.size = Pt(14)
    r_num.font.color.rgb = RWS_BLUE

    r_txt = p.add_run(title)
    r_txt.bold = True
    r_txt.font.name = "Segoe UI"
    r_txt.font.size = Pt(14)
    r_txt.font.color.rgb = RWS_GALAXY

    r_dot = p.add_run(".")
    r_dot.bold = True
    r_dot.font.name = "Segoe UI"
    r_dot.font.size = Pt(15)
    r_dot.font.color.rgb = RWS_PINK


def _add_callout_box(doc: docx.Document, title: str, body: str, bg_hex: str = "F4F0FA") -> None:
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = tbl.cell(0, 0)
    _set_cell_shading(cell, bg_hex)
    _set_cell_margins(cell, top=140, bottom=140, left=200, right=200)

    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(3)
    rt = p.add_run(title)
    rt.bold = True
    rt.font.name = "Segoe UI"
    rt.font.size = Pt(10.5)
    rt.font.color.rgb = RWS_GALAXY

    p2 = cell.add_paragraph()
    p2.paragraph_format.space_after = Pt(0)
    rb = p2.add_run(body)
    rb.font.name = "Segoe UI"
    rb.font.size = Pt(9.5)
    rb.font.color.rgb = RWS_INK

    sp = doc.add_paragraph()
    sp.paragraph_format.space_before = Pt(0)
    sp.paragraph_format.space_after = Pt(6)


def _add_bullet(doc: docx.Document, lead_bold: str, text: str) -> None:
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(4)
    r1 = p.add_run(lead_bold)
    r1.bold = True
    r1.font.name = "Segoe UI"
    r1.font.size = Pt(10)
    r1.font.color.rgb = RWS_INK

    r2 = p.add_run(text)
    r2.font.name = "Segoe UI"
    r2.font.size = Pt(10)
    r2.font.color.rgb = RWS_INK


def generate_word_user_guide(icon_png: Path) -> Path:
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    doc = docx.Document()

    # Configure page margins & base Normal font
    for section in doc.sections:
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin = Inches(0.8)
        section.right_margin = Inches(0.8)

    normal_style = doc.styles["Normal"]
    normal_style.font.name = "Segoe UI"
    normal_style.font.size = Pt(10)
    normal_style.font.color.rgb = RWS_INK

    # Top Galaxy Purple RWS Hero Banner Table
    banner = doc.add_table(rows=1, cols=1)
    banner.alignment = WD_TABLE_ALIGNMENT.CENTER
    bcell = banner.cell(0, 0)
    _set_cell_shading(bcell, "38006B")
    _set_cell_margins(bcell, top=240, bottom=240, left=260, right=260)

    bp0 = bcell.paragraphs[0]
    bp0.paragraph_format.space_after = Pt(4)
    r_eye = bp0.add_run("RWS IP SALES  •  USER GUIDELINES")
    r_eye.bold = True
    r_eye.font.name = "Segoe UI"
    r_eye.font.size = Pt(9)
    r_eye.font.color.rgb = RGBColor(0xFF, 0x85, 0xB0)

    bp1 = bcell.add_paragraph()
    bp1.paragraph_format.space_after = Pt(6)
    r_h1 = bp1.add_run("Patent TM Suitability & RTE Savings Estimator")
    r_h1.bold = True
    r_h1.font.name = "Segoe UI"
    r_h1.font.size = Pt(20)
    r_h1.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    r_hdot = bp1.add_run(".")
    r_hdot.bold = True
    r_hdot.font.size = Pt(22)
    r_hdot.font.color.rgb = RWS_PINK

    bp2 = bcell.add_paragraph()
    bp2.paragraph_format.space_after = Pt(0)
    r_sub = bp2.add_run(
        "Quick-start desktop guide for generating client Translation Memory (TM) growth projections "
        "and client-ready RWS Branded PDF Suitability Reports."
    )
    r_sub.font.name = "Segoe UI"
    r_sub.font.size = Pt(10)
    r_sub.font.color.rgb = RGBColor(0xE6, 0xDF, 0xF5)

    # -------------------------------------------------------------------------
    # 01 Overview
    # -------------------------------------------------------------------------
    _add_rws_heading(doc, "01", "Overview & Purpose")
    p = doc.add_paragraph(
        "When prospective clients ask how much discount they could achieve by building a Translation Memory (TM) "
        "with RWS, this desktop application automates the entire historical suitability simulation. Instead of "
        "manually downloading 25–50 patents and sequentially running Analyze and Update cycles in Trados Studio, "
        "the application performs the end-to-end assessment automatically in the background."
    )
    p.paragraph_format.space_after = Pt(8)

    _add_callout_box(
        doc,
        "Governance & Data Security Note — Zero AI Used at Runtime",
        "This tool is 100% deterministic, rule-based software. It does NOT use Artificial Intelligence, Large "
        "Language Models (LLMs), or external ML services. All segmentation, fuzzy-match scoring, and word-count "
        "calculations run locally on your Windows PC, producing consistent, repeatable figures every time.",
        bg_hex="FDF2F6",
    )

    # -------------------------------------------------------------------------
    # 02 Getting Started
    # -------------------------------------------------------------------------
    _add_rws_heading(doc, "02", "Launching & One-Time EPO OPS Key Setup")
    _add_bullet(
        doc,
        "1. Double-click RWS_Patent_TM_Estimator.exe: ",
        "No Python installation or local Trados Studio license is required on your laptop. The application "
        "opens directly in its own dedicated RWS desktop window.",
    )
    _add_bullet(
        doc,
        "2. Enter Your EPO OPS Consumer Key & Secret (One-Time Setup): ",
        "Click the gear icon (⚙) in the left Galaxy Purple sidebar, paste your team's EPO Open Patent Services "
        "(OPS v3.2) Consumer Key and Consumer Secret, click 'Test EPO Connection' to verify OAuth2 access, and "
        "click 'Save Settings'. Your keys are saved locally and persist across sessions.",
    )
    _add_bullet(
        doc,
        "3. Automatic 45-Day Local Patent Cache: ",
        "Fetched full-text specifications are cached locally on your PC for 45 days to conserve EPO OPS quota "
        "and make repeat client runs, larger samples, and custom rate-card checks instantaneous.",
    )

    # -------------------------------------------------------------------------
    # 03 Running an Assessment
    # -------------------------------------------------------------------------
    _add_rws_heading(doc, "03", "Running a Client Assessment")
    p_opt = doc.add_paragraph("You can assess a client portfolio using live EPO OPS v3.2 retrieval (with hybrid full-text fallback), explicit patent numbers, or uploaded files:")
    p_opt.paragraph_format.space_after = Pt(6)

    _add_bullet(
        doc,
        "Option A — Search Patent Database (EPO OPS v3.2 + Smart Cache): ",
        "Enter the Client / Applicant Name (e.g., AstraZeneca, Procter & Gamble, Siemens) and select the Source Language "
        "(EN, DE, FR, ES, JA, ZH, or KO). Click 'Verify Entities' to preview matching INPADOC legal entity names and "
        "tick the subsidiaries you want included (or paste explicit publication numbers like EP4200317A1, WO2025110491A1). "
        "Choose the sample size (typically 25–50; strictly 1 publication per INPADOC patent family is selected) and click "
        "'Run Rolling TM Simulation'. Non-specification anomalies—such as Industrial Designs / Design Patents (e.g., CA...S, "
        "USD...S1, CN...S), Plant Patents, search-report-only filings (A3/A4), and title/abstract-only stubs (< 250 words)—are "
        "automatically excluded so every row is a genuine full-text utility specification.",
    )
    _add_bullet(
        doc,
        "Option B — Upload Patent Files (.zip / .docx / .xml / .txt): ",
        "Switch to the 'Upload Patent Files' tab if you already have a folder or .zip bundle of client patents "
        "(such as exports from WIPO PATENTSCOPE or PatBase). Select the .zip archive or multiple .docx/.xml/.txt "
        "files and click 'Upload & Analyze Batch'.",
    )

    # -------------------------------------------------------------------------
    # 04 Interpreting Results & Match Bands
    # -------------------------------------------------------------------------
    _add_rws_heading(doc, "04", "Understanding the Results & Rate Weights")
    p_res = doc.add_paragraph(
        "Retrieved patents are sorted chronologically from oldest (#1) to newest (#N). Patent #1 is analyzed "
        "against a Blank TM, its segments are committed to the TM, Patent #2 is analyzed against that updated TM, "
        "and so on—mirroring how the client's TM develops over time."
    )
    p_res.paragraph_format.space_after = Pt(8)

    # Match band table
    tbl = doc.add_table(rows=6, cols=3)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    headers = ["Trados Studio 22 Band", "Payable Weight (RWS Standard)", "Effective Client Discount"]
    hdr_cells = tbl.rows[0].cells
    for idx, text in enumerate(headers):
        _set_cell_shading(hdr_cells[idx], "38006B")
        _set_cell_margins(hdr_cells[idx], top=100, bottom=100, left=140, right=140)
        hp = hdr_cells[idx].paragraphs[0]
        hr = hp.add_run(text)
        hr.bold = True
        hr.font.size = Pt(9.5)
        hr.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    band_rows = [
        ("Repetitions (Internal)", "0.30 (30%)", "70% Saving"),
        ("100% TM Match (incl. Context)", "0.30 (30%)", "70% Saving"),
        ("95% – 99% Fuzzy Match", "0.30 (30%)", "70% Saving"),
        ("85% – 94% Fuzzy Match", "0.50 (50%)", "50% Saving"),
        ("75% – 84%, 50% – 74%, No Match", "1.00 (100%)", "0% Saving (Full Rate)"),
    ]
    for r_idx, row_vals in enumerate(band_rows, start=1):
        rcells = tbl.rows[r_idx].cells
        bg = "F8F7FB" if r_idx % 2 == 1 else "FFFFFF"
        for c_idx, val in enumerate(row_vals):
            _set_cell_shading(rcells[c_idx], bg)
            _set_cell_margins(rcells[c_idx], top=80, bottom=80, left=140, right=140)
            cp = rcells[c_idx].paragraphs[0]
            cr = cp.add_run(val)
            cr.font.size = Pt(9.5)
            if c_idx == 0:
                cr.bold = True

    doc.add_paragraph().paragraph_format.space_after = Pt(4)

    _add_bullet(
        doc,
        "Projected Cumulative TM Saving (Card 01): ",
        "Your headline portfolio discount across the entire assessed batch from Patent #1 through #N.",
    )
    _add_bullet(
        doc,
        "Mature TM Saving — 2nd Half Avg (Card 02): ",
        "Shows the average TM saving across the second half of the chronological sample once the client's TM has warmed up.",
    )

    # -------------------------------------------------------------------------
    # 05 Removable RTE Toggle & Branded PDF Report Export
    # -------------------------------------------------------------------------
    _add_rws_heading(doc, "05", "Using the Removable RTE Toggle & RWS PDF Report")
    _add_bullet(
        doc,
        "Include RTE Switch (Live Toggle): ",
        "By default, Removable RTE is OFF (0.00%) and all RTE references are completely hidden from the exported PDF "
        "report so clients see a clean, pure-TM assessment. Toggling 'Include RTE' ON (before running or live on the "
        "results screen) adds the Removable RTE card, methodology breakdown, and per-patent RTE column to the report.",
    )
    _add_bullet(
        doc,
        "Download PDF Report: ",
        "Click the pink 'Download PDF Report' button to generate a client-ready RWS Branded PDF Suitability Report "
        "containing the Executive Dashboard, KPI cards, TM growth & match-band vector charts, portfolio payable rate "
        "summary, and the complete chronological patent-by-patent breakdown with clickable publication links.",
    )

    # -------------------------------------------------------------------------
    # 06 Troubleshooting
    # -------------------------------------------------------------------------
    _add_rws_heading(doc, "06", "Quick Troubleshooting")
    _add_bullet(
        doc,
        "Temporary Google Patents Rate-Limit (HTTP 429 / 503): ",
        "The application uses single-call family search, polite rate-paced downloads, and a 45-day local patent cache "
        "to minimize public search traffic. If a shared corporate VPN triggers a temporary Google Patents pause, any "
        "patents already downloaded are preserved in your local cache—simply wait 2–3 minutes and re-run, paste "
        "publication numbers directly, or upload a .zip/.docx export.",
    )
    _add_bullet(
        doc,
        "Windows SmartScreen Prompt on First Launch: ",
        "If Windows displays 'Windows protected your PC' the first time an internally shared .exe is opened, "
        "click 'More info' -> 'Run anyway'.",
    )

    out_path = RELEASE_DIR / "RWS_Patent_TM_Estimator_User_Guide.docx"
    doc.save(out_path)
    print("Generated RWS Word User Guide:", out_path)
    return out_path


if __name__ == "__main__":
    vendor_chartjs()
    png_icon = generate_rws_ico()
    generate_word_user_guide(png_icon)
