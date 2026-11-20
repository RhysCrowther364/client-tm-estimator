"""Patent retrieval providers:
1. EPO Open Patent Services (OPS v3.2) API — OAuth2, CQL applicant search, 1-per-INPADOC-family deduplication, XML fulltext + <tables>
2. Google Patents Public Live Provider — Zero-API-key live search & fulltext/HTML <table> extraction (standalone or fallback)
3. Uploaded File Parser (.docx, .xml, .txt) — Direct document batch support
4. Realistic Demo Portfolio Generator — Instant offline demonstration (Pharma w/ NMR & numeric tables, MedTech, Automotive)
"""
from __future__ import annotations

import asyncio
from collections import Counter
import html
import io
import re
from typing import Callable, Optional
import urllib.parse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx

from app.config import AppSettings
from app.models import ApplicantSearchResponse, ApplicantVariant, PatentDocument

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")

# Language code mappings (expanded office coverage per language)
_GP_LANG_MAP = {
    "EN": ("ENGLISH", "WO,EP,US,GB,CA,AU"),
    "DE": ("GERMAN", "DE,EP,WO,AT,CH"),
    "FR": ("FRENCH", "FR,EP,WO,CA,CH,BE"),
    "ES": ("SPANISH", "ES,WO,EP,MX"),
    "JA": ("JAPANESE", "JP,WO"),
    "ZH": ("CHINESE", "CN,WO,TW"),
    "KO": ("KOREAN", "KR,WO"),
}

_LEGAL_SUFFIX_RE = re.compile(
    r"(?:^|\s+)(?:주식회사|\(주\)|㈜|株式会社|有限会社)"
    r"|(?:[,\s]+(?:Inc\.?|Incorporated|Corp\.?|Corporation|Co\.?,?\s*Ltd\.?|Ltd\.?|Limited|"
    r"L\.?L\.?C\.?|P\.?L\.?C\.?|GmbH(?:\s*&\s*Co\.?\s*KG)?|Aktiengesellschaft|AG|S\.?A\.?(?:S\.?|R\.?L\.?)?|"
    r"B\.?V\.?|N\.?V\.?|A/S|AB|Oyj|Oy|K\.?K\.?|有限公司|股份有限公司|\(주\)|㈜|주식회사))+\s*$",
    re.IGNORECASE,
)

_PUB_NUM_TOKEN_RE = re.compile(
    r"\b(?:WO|EP|US|KR|JP|CN|DE|FR|GB|ES|CA|AU|TW|CH|AT|BE|NL|SE)\s*\d{4,}[A-Z0-9]*\b",
    re.IGNORECASE,
)


def _strip_legal_suffix(name: str) -> str:
    cleaned = _LEGAL_SUFFIX_RE.sub("", (name or "").strip()).strip(" ,.;:-")
    return cleaned if len(cleaned) >= 2 else (name or "").strip()


def _parse_direct_pub_numbers(raw: str) -> list[str]:
    """Detect if the user pasted explicit patent publication numbers (e.g. 'WO2025110491A1, EP4200317A1')."""
    tokens = [re.sub(r"\s+", "", m.group(0)).upper() for m in _PUB_NUM_TOKEN_RE.finditer(raw or "")]
    if not tokens:
        return []
    # Treat as direct pub-number list if >=2 numbers pasted, or if the entire input is 1 publication number
    stripped = re.sub(r"[\s,;]+", "", raw or "").upper()
    if len(tokens) >= 2 or (len(tokens) == 1 and stripped == tokens[0]):
        seen: set[str] = set()
        out: list[str] = []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out
    return []


def _extract_gp_family_id(pat_obj: dict) -> str:
    """Extract patent family ID from Google Patents XHR 'family_metadata' if available."""
    fm = pat_obj.get("family_metadata")
    if isinstance(fm, dict):
        agg = fm.get("aggregated")
        if isinstance(agg, dict) and agg.get("family_id"):
            return str(agg["family_id"]).strip()
        if fm.get("family_id"):
            return str(fm["family_id"]).strip()
    return ""


def _clean_html_snippet(raw: str) -> str:
    if not raw:
        return ""
    return _MULTI_SPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub("", raw))).strip()


def _unwrap_inline_formatting(root_el) -> None:
    """Unwrap inline HTML/XML tags (sup, sub, i, b, span, etc.) and merge adjacent text nodes
    so tokens like <sup>1</sup>H-NMR and CDCl<sub>3</sub> stay intact as '1H-NMR' and 'CDCl3'
    instead of splitting across lines or spaces."""
    for inline in root_el.find_all(["sup", "sub", "i", "b", "em", "strong", "u", "small", "font", "span"]):
        inline.unwrap()
    if hasattr(root_el, "smooth"):
        root_el.smooth()


def _extract_bs4_section_with_tables(section_el) -> tuple[str, list[str]]:
    """Extract clean prose text (excluding tables) and a separate list of table cell strings from an HTML/XML element."""
    if section_el is None:
        return "", []

    # Remove hidden metadata,original-language overlays, or line-number asides first
    for junk in section_el.find_all(["aside", "script", "style"]):
        junk.decompose()
    for hidden_src in section_el.find_all("span", class_="google-src-text"):
        hidden_src.decompose()

    table_cells: list[str] = []
    for tbl in section_el.find_all(["table", "tables"]):
        _unwrap_inline_formatting(tbl)
        cells = tbl.find_all(["td", "th", "entry"])
        if cells:
            for c in cells:
                txt = _MULTI_SPACE_RE.sub(" ", c.get_text(" ", strip=True)).strip()
                if txt:
                    table_cells.append(txt)
        tbl.decompose()

    # Unwrap inline tags in prose so <sup>1</sup>H-NMR and CDCl<sub>3</sub> are never shattered by newline separators
    _unwrap_inline_formatting(section_el)

    # Insert newlines around block elements so paragraph boundaries remain intact for SRX segmentation
    raw_text = section_el.get_text("\n", strip=True)

    cleaned = "\n".join(
        _MULTI_SPACE_RE.sub(" ", ln).strip()
        for ln in raw_text.splitlines()
        if ln.strip()
    )
    return _MULTI_NL_RE.sub("\n\n", cleaned), table_cells


# ==============================================================================
# 1. EPO Open Patent Services (OPS v3.2) Provider
# ==============================================================================

class EPOOPSClient:
    """Official EPO OPS v3.2 REST client with OAuth2 client-credentials authentication,
    1-per-INPADOC-family deduplication, local SQLite full-text caching, and hybrid full-text fallback."""

    AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
    BASE_URL = "https://ops.epo.org/3.2/rest-services"

    _EPO_OFFICE_CQL = {
        "EN": "(pn=EP OR pn=WO OR pn=US OR pn=GB OR pn=CA OR pn=AU)",
        "DE": "(pn=DE OR pn=EP OR pn=WO OR pn=AT OR pn=CH)",
        "FR": "(pn=FR OR pn=EP OR pn=WO OR pn=CA OR pn=CH OR pn=BE)",
        "ES": "(pn=ES OR pn=WO OR pn=EP)",
        "JA": "(pn=JP OR pn=WO)",
        "ZH": "(pn=CN OR pn=WO)",
        "KO": "(pn=KR OR pn=WO)",
    }

    def __init__(self, consumer_key: str, consumer_secret: str) -> None:
        self.consumer_key = consumer_key.strip()
        self.consumer_secret = consumer_secret.strip()
        self._token: Optional[str] = None

    @staticmethod
    def _clean_epo_applicant_name(raw: str) -> str:
        """Normalize EPO applicant names (strip trailing '[SE]' country suffixes and extra whitespace)."""
        cleaned = re.sub(r"[\u2000-\u200b\s]+", " ", raw or "").strip()
        cleaned = re.sub(r"\s*\[[A-Z]{2}\]\s*$", "", cleaned).strip(" ,;")
        return cleaned

    @staticmethod
    def _sanitize_cql_term(term: str) -> str:
        cleaned = EPOOPSClient._clean_epo_applicant_name((term or "").replace('"', " "))
        return cleaned.strip()

    async def test_connection(self) -> dict:
        """Verify EPO OPS v3.2 Consumer Key & Secret via OAuth2 token endpoint."""
        async with httpx.AsyncClient() as client:
            token = await self._authenticate(client)
            return {"ok": bool(token), "provider": "EPO OPS v3.2"}

    async def _authenticate(self, client: httpx.AsyncClient) -> str:
        if self._token:
            return self._token
        resp = await client.post(
            self.AUTH_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.consumer_key, self.consumer_secret),
            headers={"Accept": "application/json"},
            timeout=20.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"EPO OPS authentication failed (HTTP {resp.status_code}): {resp.text[:200]}")
        self._token = resp.json()["access_token"]
        return self._token

    async def _get_xml(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> Optional[ET.Element]:
        token = await self._authenticate(client)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/xml"}
        resp = await client.get(f"{self.BASE_URL}{path}", params=params, headers=headers, timeout=25.0)
        if resp.status_code == 401:
            self._token = None
            token = await self._authenticate(client)
            headers["Authorization"] = f"Bearer {token}"
            resp = await client.get(f"{self.BASE_URL}{path}", params=params, headers=headers, timeout=25.0)
        if resp.status_code in (404, 400):
            # Note: EPO OPS returns HTTP 404 when a CQL search matches 0 publications or fulltext is absent
            if resp.status_code == 404 or "SERVER.EntityNotFound" in resp.text:
                return None
        if resp.status_code >= 400:
            raise RuntimeError(f"EPO OPS request {path} failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return ET.fromstring(resp.text)

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    async def search_applicants(self, query: str, language: str = "EN") -> ApplicantSearchResponse:
        clean_q = self._sanitize_cql_term(query)
        stripped_q = self._sanitize_cql_term(_strip_legal_suffix(clean_q))
        office_cql = self._EPO_OFFICE_CQL.get(language.upper(), self._EPO_OFFICE_CQL["EN"])

        async with httpx.AsyncClient() as client:
            root = await self._get_xml(
                client,
                "/published-data/search/biblio",
                params={"q": f'pa all "{clean_q}" AND {office_cql}', "Range": "1-60"},
            )
            if root is None and stripped_q and stripped_q.lower() != clean_q.lower():
                root = await self._get_xml(
                    client,
                    "/published-data/search/biblio",
                    params={"q": f'pa all "{stripped_q}" AND {office_cql}', "Range": "1-60"},
                )

        if root is None:
            return ApplicantSearchResponse(query=query, source_used="EPO OPS v3.2", total_found=0, variants=[])

        total_found = 0
        for el in root.iter():
            if self._local_name(el.tag) == "biblio-search":
                total_found = int(el.attrib.get("total-result-count", "0") or 0)
                break

        base_tokens = [t.lower() for t in re.split(r"\W+", stripped_q or clean_q) if len(t) >= 2]
        is_cjk_lang = language.upper() in ("KO", "JA", "ZH")
        counts: Counter[str] = Counter()

        for exch in root.iter():
            if self._local_name(exch.tag) != "exchange-document":
                continue
            # Deduplicate epodoc/original applicant tags within the same exchange-document
            doc_names: set[str] = set()
            for el in exch.iter():
                if self._local_name(el.tag) == "applicant-name":
                    name_txt = self._clean_epo_applicant_name("".join(el.itertext()))
                    if not name_txt:
                        continue
                    has_non_ascii = any(ord(ch) > 127 for ch in name_txt)
                    if is_cjk_lang and has_non_ascii:
                        doc_names.add(name_txt)
                    elif not base_tokens or any(tok in name_txt.lower() for tok in base_tokens):
                        doc_names.add(name_txt)
            for dn in doc_names:
                counts[dn] += 1

        variants = [ApplicantVariant(name=k, hit_count=v) for k, v in counts.most_common(12)]
        return ApplicantSearchResponse(
            query=query,
            source_used="EPO OPS v3.2",
            total_found=total_found,
            variants=variants,
        )

    async def fetch_patents(
        self,
        client_name: str,
        applicant_variants: list[str],
        sample_size: int,
        language: str = "EN",
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[PatentDocument]:
        import datetime as dt
        from app.patent_cache import get_cached_patent, put_cached_patent

        lang_up = language.upper()
        lang_low = language.lower()

        # Support direct patent publication number lists via EPO OPS + Hybrid Fallback too
        direct_pubs = _parse_direct_pub_numbers(client_name)
        if direct_pubs:
            or_pubs = " OR ".join(f"pn={p}" for p in direct_pubs[:50])
            cql_tiers = [or_pubs]
        else:
            raw_names = applicant_variants if applicant_variants else [client_name]
            clean_names = [self._sanitize_cql_term(n) for n in raw_names if self._sanitize_cql_term(n)]
            if not applicant_variants:
                stripped = self._sanitize_cql_term(_strip_legal_suffix(client_name))
                if stripped and all(stripped.lower() != n.lower() for n in clean_names):
                    clean_names.append(stripped)

            or_clauses = " OR ".join(f'pa all "{n}"' for n in clean_names[:5])
            office_cql = self._EPO_OFFICE_CQL.get(lang_up, self._EPO_OFFICE_CQL["EN"])
            base_cql = f"({or_clauses}) AND {office_cql}"
            current_year = dt.date.today().year
            cql_tiers = [
                f"{base_cql} AND pd>={current_year - 2}",
                f"{base_cql} AND pd>={current_year - 6}",
                base_cql,
            ]

        candidates: list[dict] = []
        seen_families: set[str] = set()

        async with httpx.AsyncClient() as client:
            for tier_idx, cql in enumerate(cql_tiers):
                for start in range(1, 201, 50):
                    end = start + 49
                    try:
                        root = await self._get_xml(
                            client,
                            "/published-data/search/biblio",
                            params={"q": cql, "Range": f"{start}-{end}"},
                        )
                    except Exception:
                        if tier_idx == len(cql_tiers) - 1 and start == 1 and not candidates:
                            raise
                        break
                    if root is None:
                        break

                    docs_in_batch = 0
                    for exch in root.iter():
                        if self._local_name(exch.tag) != "exchange-document":
                            continue
                        docs_in_batch += 1
                        fam_id = exch.attrib.get("family-id", "")
                        country = exch.attrib.get("country", "")
                        doc_num = exch.attrib.get("doc-number", "")
                        kind = exch.attrib.get("kind", "")
                        pub_ref = f"{country}{doc_num}{kind}"
                        if not fam_id:
                            fam_id = pub_ref

                        if fam_id in seen_families:
                            continue

                        # Extract publication date, title, abstract, applicant
                        pub_date = ""
                        title = ""
                        abstract_txt = ""
                        app_name = ""

                        for sub in exch.iter():
                            lname = self._local_name(sub.tag)
                            if lname == "publication-reference" and not pub_date:
                                for d_el in sub.iter():
                                    if self._local_name(d_el.tag) == "date" and d_el.text:
                                        pub_date = d_el.text.strip()
                            elif lname == "invention-title":
                                t_lang = (sub.attrib.get("lang") or "").lower()
                                txt = "".join(sub.itertext()).strip()
                                if txt and (t_lang == lang_low or not title):
                                    title = txt
                            elif lname == "abstract":
                                a_lang = (sub.attrib.get("lang") or "").lower()
                                txt = " ".join(s.strip() for s in sub.itertext() if s.strip())
                                if txt and (a_lang == lang_low or not abstract_txt):
                                    abstract_txt = txt
                            elif lname == "applicant-name" and not app_name:
                                app_name = self._clean_epo_applicant_name("".join(sub.itertext()))

                        seen_families.add(fam_id)
                        candidates.append({
                            "pub_number": pub_ref,
                            "epodoc": f"{country}{doc_num}.{kind}" if kind else f"{country}{doc_num}",
                            "family_id": fam_id,
                            "pub_date": pub_date,
                            "title": title,
                            "abstract": abstract_txt,
                            "applicant": app_name or client_name,
                        })
                        if len(candidates) >= sample_size * 2:
                            break

                    if docs_in_batch < 50 or len(candidates) >= sample_size * 2:
                        break
                if len(candidates) >= sample_size:
                    break

            # Sort newest first to select the N most recent families
            candidates.sort(key=lambda x: x["pub_date"], reverse=True)
            selected = candidates[:sample_size]

            gp_fallback = GooglePatentsClient()
            gp_http: Optional[httpx.AsyncClient] = None
            results: list[PatentDocument] = []
            total = len(selected)

            try:
                for idx, item in enumerate(selected, start=1):
                    pub = item["pub_number"]

                    # 1) Check 45-day local SQLite full-text cache first (saves EPO OPS quota & time)
                    cached_doc = get_cached_patent(pub, language)
                    if cached_doc is not None and (cached_doc.description_text or cached_doc.claims_text):
                        if on_progress:
                            on_progress(idx, total, f"Loaded {pub} from local cache ({idx}/{total})...")
                        results.append(cached_doc)
                        continue

                    if on_progress:
                        on_progress(idx, total, f"Downloading {pub} ({idx}/{total}) via EPO OPS v3.2...")

                    desc_txt, claims_txt, table_cells = await self._fetch_fulltext(
                        client, item["epodoc"], lang_low
                    )

                    # 2) Hybrid fallback: if EPO OPS lacks fulltext XML in requested language (e.g. certain US/KR/JP/WO records), fetch fulltext from Google Patents
                    if not desc_txt and not claims_txt:
                        if gp_http is None:
                            gp_http = httpx.AsyncClient(
                                headers=GooglePatentsClient.HEADERS, follow_redirects=True, timeout=25.0
                            )
                        try:
                            gp_doc = await gp_fallback.fetch_single_patent_html(
                                gp_http,
                                pub_number=pub,
                                family_id=item["family_id"],
                                pub_date=item["pub_date"],
                                applicant=item["applicant"],
                                language=language,
                                fallback_title=item["title"],
                            )
                        except Exception:
                            gp_doc = None
                        if gp_doc and (gp_doc.description_text or gp_doc.claims_text):
                            put_cached_patent(gp_doc)
                            results.append(gp_doc)
                            await asyncio.sleep(0.25)
                            continue

                    built_doc = PatentDocument(
                        pub_number=pub,
                        family_id=item["family_id"],
                        pub_date=item["pub_date"],
                        title=item["title"],
                        applicant=item["applicant"],
                        language=language,
                        abstract_text=item["abstract"],
                        description_text=desc_txt,
                        claims_text=claims_txt,
                        table_cells=table_cells,
                        source_url=f"https://worldwide.espacenet.com/patent/search?q=pn%3D{pub}",
                    )
                    if desc_txt or claims_txt:
                        put_cached_patent(built_doc)
                    results.append(built_doc)
                    await asyncio.sleep(0.15)  # Polite OPS rate pacing
            finally:
                if gp_http is not None:
                    await gp_http.aclose()

        return results

    async def _fetch_fulltext(
        self, client: httpx.AsyncClient, epodoc_id: str, lang_low: str
    ) -> tuple[str, str, list[str]]:
        desc_txt = ""
        claims_txt = ""
        table_cells: list[str] = []

        for endpoint in ("description", "claims"):
            try:
                root = await self._get_xml(
                    client, f"/published-data/publication/epodoc/{epodoc_id}/{endpoint}"
                )
            except Exception:
                root = None
            if root is None:
                continue

            xml_bytes = ET.tostring(root, encoding="unicode")
            soup = BeautifulSoup(xml_bytes, "xml")
            target_tag = soup.find(endpoint, attrs={"lang": lang_low.upper()}) or soup.find(
                endpoint, attrs={"lang": lang_low}
            ) or soup.find(endpoint)
            if target_tag:
                txt, cells = _extract_bs4_section_with_tables(target_tag)
                if endpoint == "description":
                    desc_txt = txt
                else:
                    claims_txt = txt
                table_cells.extend(cells)

        return desc_txt, claims_txt, table_cells


# ==============================================================================
# 2. Google Patents Zero-Key Live Provider (with Local SQLite Cache & Pacing)
# ==============================================================================

class GooglePatentsClient:
    """Zero-API-key live patent fetcher using Google Patents family-grouped XHR,
    proper URL encoding, multi-entity variant support, polite rate pacing, and persistent local caching."""

    XHR_URL = "https://patents.google.com/xhr/query"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    @staticmethod
    def _build_search_url_param(
        assignees: list[str], countries: str, gp_lang: str, num: int = 100, page: int = 0
    ) -> str:
        # Properly URL-encode each assignee so '&' (e.g. 'Procter & Gamble', 'Johnson & Johnson') never breaks inner_q
        encoded_assignees = ",".join(urllib.parse.quote_plus(a.strip()) for a in assignees if a.strip())
        parts = [
            f"assignee={encoded_assignees}",
            f"country={countries}",
            f"language={gp_lang}",
            f"num={min(max(num, 20), 100)}",
            "sort=new",
        ]
        if page > 0:
            parts.append(f"page={page}")
        # Note: omitting dups=language uses Google Patents' native 1-per-Simple-Patent-Family server-side grouping
        return "&".join(parts)

    async def _query_xhr_cached(self, client: Optional[httpx.AsyncClient], inner_q: str) -> tuple[int, dict]:
        from app.patent_cache import get_cached_search, put_cached_search

        cache_key = f"gp_xhr:{inner_q}"
        cached = get_cached_search(cache_key)
        if cached is not None:
            return 200, cached

        if client is None:
            async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=25.0) as tmp_client:
                resp = await tmp_client.get(self.XHR_URL, params={"url": inner_q})
        else:
            resp = await client.get(self.XHR_URL, params={"url": inner_q})

        if resp.status_code == 200:
            data = resp.json()
            put_cached_search(cache_key, data)
            return 200, data
        return resp.status_code, {}

    async def search_applicants(self, query: str, language: str = "EN") -> ApplicantSearchResponse:
        gp_lang, countries = _GP_LANG_MAP.get(language.upper(), ("ENGLISH", "WO,EP,US,GB,CA,AU"))
        clean_q = query.strip()
        stripped_q = _strip_legal_suffix(clean_q)

        async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=22.0) as client:
            inner_q = self._build_search_url_param([clean_q], countries, gp_lang, num=100, page=0)
            status, data = await self._query_xhr_cached(client, inner_q)
            if status in (429, 503):
                raise RuntimeError(
                    f"Google Patents temporarily throttled automated search (HTTP {status}). "
                    "Please wait a few minutes, paste publication numbers directly, or use the Upload Patent Files (.zip / .docx) tab."
                )
            if status != 200:
                raise RuntimeError(f"Google Patents search returned HTTP {status}.")

            results_obj = data.get("results", {})
            total_found = int(results_obj.get("total_num_results", 0) or 0)
            clusters = results_obj.get("cluster", [])
            items = clusters[0].get("result", []) if clusters else []

            # Automatic corporate-suffix fallback (e.g. 'AstraZeneca PLC' -> 'AstraZeneca')
            if not items and stripped_q and stripped_q.lower() != clean_q.lower():
                await asyncio.sleep(0.35)
                inner_q2 = self._build_search_url_param([stripped_q], countries, gp_lang, num=100, page=0)
                status2, data2 = await self._query_xhr_cached(client, inner_q2)
                if status2 == 200:
                    results_obj = data2.get("results", {})
                    total_found = int(results_obj.get("total_num_results", 0) or 0)
                    clusters = results_obj.get("cluster", [])
                    items = clusters[0].get("result", []) if clusters else []

        base_for_tokens = stripped_q or clean_q
        q_tokens = [t.lower() for t in re.split(r"\W+", base_for_tokens) if len(t) >= 2]
        is_cjk_lang = language.upper() in ("KO", "JA", "ZH")
        counts: Counter[str] = Counter()

        for item in items:
            pat = item.get("patent", {})
            raw_ass = _clean_html_snippet(pat.get("assignee", ""))
            if not raw_ass:
                continue
            for part in [p.strip() for p in raw_ass.split(";") if p.strip()]:
                has_non_ascii = any(ord(ch) > 127 for ch in part)
                if is_cjk_lang and has_non_ascii:
                    counts[part] += 1
                elif not q_tokens or any(tok in part.lower() for tok in q_tokens):
                    counts[part] += 1

        variants = [ApplicantVariant(name=k, hit_count=v) for k, v in counts.most_common(12)]
        return ApplicantSearchResponse(
            query=query,
            source_used="Google Patents (1 per Family • Smart Cache)",
            total_found=total_found,
            variants=variants,
            note="Grouped 1 per patent family. Select matching legal entities to include in your simulation.",
        )

    async def fetch_patents(
        self,
        client_name: str,
        applicant_variants: list[str],
        sample_size: int,
        language: str = "EN",
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[PatentDocument]:
        import random
        from app.patent_cache import get_cached_patent, put_cached_patent

        # Fast-path: check if user pasted explicit patent publication numbers (e.g. 'WO2025110491A1, EP4200317A1')
        direct_pubs = _parse_direct_pub_numbers(client_name)
        if direct_pubs:
            candidates = [
                {"pub_number": p, "family_id": "", "pub_date": "", "title": "", "assignee": "Selected Portfolio"}
                for p in direct_pubs[: max(sample_size, len(direct_pubs))]
            ]
            target_goal = min(sample_size, len(candidates)) if len(direct_pubs) > sample_size else len(candidates)
            return await self._download_candidates_paced(
                candidates=candidates,
                target_count=target_goal,
                client_name="Patent List Portfolio",
                language=language,
                on_progress=on_progress,
            )

        gp_lang, countries = _GP_LANG_MAP.get(language.upper(), ("ENGLISH", "WO,EP,US,GB,CA,AU"))
        primary_query = client_name.strip()
        stripped_query = _strip_legal_suffix(primary_query)

        # Build prioritized search query groups:
        # 1) Explicitly checked applicant variants (if any) — up to 4 per XHR call
        # 2) Exact client name
        # 3) Stripped legal-suffix core name (if needed to reach sample_size)
        query_batches: list[list[str]] = []
        if applicant_variants:
            clean_vars = [v.strip() for v in applicant_variants if v.strip()]
            for i in range(0, len(clean_vars), 4):
                query_batches.append(clean_vars[i: i + 4])
        else:
            query_batches.append([primary_query])
            if stripped_query and stripped_query.lower() != primary_query.lower():
                query_batches.append([stripped_query])

        match_lowers = [v.lower() for v in (applicant_variants or [primary_query, stripped_query]) if v]
        q_tokens = [t.lower() for t in re.split(r"\W+", stripped_query or primary_query) if len(t) >= 2]
        is_cjk_lang = language.upper() in ("KO", "JA", "ZH")

        candidates: list[dict] = []
        seen_pubs: set[str] = set()
        seen_search_families: set[str] = set()
        seen_title_keys: set[str] = set()
        desired_pool = min(140, max(sample_size + 15, int(sample_size * 1.4)))

        http_client: Optional[httpx.AsyncClient] = None
        try:
            for b_idx, assignees_batch in enumerate(query_batches):
                if len(candidates) >= desired_pool:
                    break
                max_pages = 2 if sample_size > 60 else 1
                for page in range(0, max_pages):
                    inner_q = self._build_search_url_param(assignees_batch, countries, gp_lang, num=100, page=page)
                    from app.patent_cache import get_cached_search
                    cached_search = get_cached_search(f"gp_xhr:{inner_q}")
                    if cached_search is not None:
                        status, data = 200, cached_search
                    else:
                        if b_idx > 0 or page > 0:
                            await asyncio.sleep(random.uniform(0.35, 0.55))
                        if http_client is None:
                            http_client = httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=25.0)
                        status, data = await self._query_xhr_cached(http_client, inner_q)
                    if status in (429, 503):
                        if not candidates:
                            raise RuntimeError(
                                f"Google Patents temporarily rate-limited search requests (HTTP {status}). "
                                "Wait a few minutes and try again, paste publication numbers directly, or use the "
                                "'Upload Patent Files (.zip / .docx)' tab."
                            )
                        break
                    if status != 200:
                        if not candidates and b_idx == len(query_batches) - 1:
                            raise RuntimeError(f"Patent search returned HTTP {status}.")
                        break

                    clusters = data.get("results", {}).get("cluster", [])
                    items = clusters[0].get("result", []) if clusters else []
                    if not items:
                        break

                    for item in items:
                        pat = item.get("patent", {})
                        pub_num = (pat.get("publication_number") or "").strip()
                        if not pub_num or pub_num in seen_pubs:
                            continue

                        fam_id = _extract_gp_family_id(pat)
                        if fam_id and fam_id in seen_search_families:
                            continue

                        clean_ass = _clean_html_snippet(pat.get("assignee", ""))
                        ass_low = clean_ass.lower()
                        has_non_ascii_ass = any(ord(ch) > 127 for ch in clean_ass)

                        if applicant_variants:
                            if clean_ass and not any(v in ass_low for v in match_lowers):
                                continue
                        elif q_tokens and clean_ass and not (is_cjk_lang and has_non_ascii_ass):
                            if not any(tok in ass_low for tok in q_tokens):
                                continue

                        clean_title = _clean_html_snippet(pat.get("title", ""))
                        filing_yr = (pat.get("filing_date") or "")[:7]
                        norm_title = re.sub(r"\W+", "", clean_title.lower(), flags=re.UNICODE)[:60]
                        title_key = f"{norm_title}|{filing_yr}"
                        if norm_title and title_key in seen_title_keys:
                            continue

                        seen_pubs.add(pub_num)
                        if fam_id:
                            seen_search_families.add(fam_id)
                        if norm_title:
                            seen_title_keys.add(title_key)

                        candidates.append({
                            "pub_number": pub_num,
                            "family_id": fam_id,
                            "pub_date": pat.get("publication_date") or pat.get("filing_date") or "",
                            "title": clean_title,
                            "assignee": clean_ass or client_name,
                        })
                        if len(candidates) >= desired_pool:
                            break

                    if len(candidates) >= sample_size + 10 or len(items) < 50:
                        break
        finally:
            if http_client is not None:
                await http_client.aclose()

        if not candidates:
            raise RuntimeError(
                f"No published {language} patents found for applicant '{client_name}'. "
                "Try clicking 'Verify Entities', checking source language, or removing subsidiary suffixes."
            )

        # Sort newest first so we pull the most recent N patent families
        candidates.sort(key=lambda c: c.get("pub_date") or "", reverse=True)

        return await self._download_candidates_paced(
            candidates=candidates,
            target_count=sample_size,
            client_name=client_name,
            language=language,
            on_progress=on_progress,
        )

    async def _download_candidates_paced(
        self,
        candidates: list[dict],
        target_count: int,
        client_name: str,
        language: str,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[PatentDocument]:
        """Fetch full-text specifications using local SQLite cache first, then polite rate-paced waves of 2,
        stopping immediately once target_count distinct full-text families are collected."""
        import random
        from app.patent_cache import get_cached_patent, put_cached_patent

        fetched_docs: list[PatentDocument] = []
        abstract_only_backups: list[PatentDocument] = []
        seen_family_ids: set[str] = set()
        rate_limited = False
        client: Optional[httpx.AsyncClient] = None

        try:
            idx = 0
            total_cands = len(candidates)

            while idx < total_cands and len(fetched_docs) < target_count and not rate_limited:
                # 1) Drain any immediately consecutive cached candidates at zero network cost
                cand = candidates[idx]
                cached_doc = get_cached_patent(cand["pub_number"], language)
                if cached_doc is not None:
                    idx += 1
                    fid = cached_doc.family_id or cand.get("family_id") or cached_doc.pub_number
                    if fid in seen_family_ids:
                        continue
                    if cached_doc.description_text or cached_doc.claims_text:
                        seen_family_ids.add(fid)
                        fetched_docs.append(cached_doc)
                        if on_progress:
                            on_progress(
                                len(fetched_docs),
                                target_count,
                                f"Loaded {cached_doc.pub_number} from local cache ({len(fetched_docs)}/{target_count})...",
                            )
                    elif cached_doc.abstract_text:
                        abstract_only_backups.append(cached_doc)
                    continue

                # 2) Assemble a polite wave of up to 2 uncached patents (never exceeding remaining needed count)
                needed = target_count - len(fetched_docs)
                wave_size = min(2, needed)
                wave: list[dict] = []
                while idx < total_cands and len(wave) < wave_size:
                    c_item = candidates[idx]
                    idx += 1
                    # Check cache in case second item in wave IS cached
                    c_cached = get_cached_patent(c_item["pub_number"], language)
                    if c_cached is not None:
                        fid = c_cached.family_id or c_item.get("family_id") or c_cached.pub_number
                        if fid not in seen_family_ids and (c_cached.description_text or c_cached.claims_text):
                            seen_family_ids.add(fid)
                            fetched_docs.append(c_cached)
                            if on_progress:
                                on_progress(
                                    len(fetched_docs),
                                    target_count,
                                    f"Loaded {c_cached.pub_number} from local cache ({len(fetched_docs)}/{target_count})...",
                                )
                            if len(fetched_docs) >= target_count:
                                break
                        continue
                    wave.append(c_item)

                if not wave or len(fetched_docs) >= target_count:
                    continue

                if client is None:
                    client = httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=25.0)

                wave_results = await asyncio.gather(
                    *(
                        self.fetch_single_patent_html(
                            client,
                            pub_number=w["pub_number"],
                            family_id=w.get("family_id", ""),
                            pub_date=w.get("pub_date", ""),
                            applicant=w.get("assignee") or client_name,
                            language=language,
                            fallback_title=w.get("title", ""),
                        )
                        for w in wave
                    ),
                    return_exceptions=True,
                )

                for res in wave_results:
                    if isinstance(res, PermissionError):
                        rate_limited = True
                        continue
                    if isinstance(res, Exception) or res is None:
                        continue

                    fid = res.family_id or res.pub_number
                    if fid in seen_family_ids:
                        continue

                    if res.description_text or res.claims_text:
                        put_cached_patent(res)
                        seen_family_ids.add(fid)
                        fetched_docs.append(res)
                        if on_progress:
                            on_progress(
                                min(len(fetched_docs), target_count),
                                target_count,
                                f"Downloaded {res.pub_number} ({min(len(fetched_docs), target_count)}/{target_count})...",
                            )
                        if len(fetched_docs) >= target_count:
                            break
                    elif res.abstract_text:
                        abstract_only_backups.append(res)

                # Polite jittered pacing between live waves to protect corporate IP reputation
                if len(fetched_docs) < target_count and idx < total_cands and not rate_limited:
                    await asyncio.sleep(random.uniform(0.35, 0.60))
        finally:
            if client is not None:
                await client.aclose()

        # Only fall back to abstract-only records if fewer full-text specifications existed globally
        if len(fetched_docs) < target_count and abstract_only_backups and not rate_limited:
            for bdoc in abstract_only_backups:
                fid = bdoc.family_id or bdoc.pub_number
                if fid not in seen_family_ids:
                    seen_family_ids.add(fid)
                    fetched_docs.append(bdoc)
                    if len(fetched_docs) >= target_count:
                        break

        if rate_limited and len(fetched_docs) < max(5, target_count // 3):
            raise RuntimeError(
                "Google Patents temporarily throttled full-text downloads (HTTP 429/503). "
                f"{len(fetched_docs)} patents were saved to your local cache so far — wait a few minutes and run "
                "again to resume from cache automatically, or upload .zip/.docx files."
            )

        return fetched_docs[:target_count]

    async def fetch_single_patent_html(
        self,
        client: httpx.AsyncClient,
        pub_number: str,
        family_id: str,
        pub_date: str,
        applicant: str,
        language: str = "EN",
        fallback_title: str = "",
    ) -> Optional[PatentDocument]:
        lang_slug = language.lower()[:2]
        url = f"https://patents.google.com/patent/{pub_number}/{lang_slug}"

        resp = None
        for attempt in range(2):
            try:
                resp = await client.get(url)
                if resp.status_code in (429, 503):
                    raise PermissionError(f"HTTP {resp.status_code}")
                if resp.status_code in (500, 502, 504) and attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                if resp.status_code != 200 and lang_slug == "en":
                    resp = await client.get(f"https://patents.google.com/patent/{pub_number}")
                break
            except PermissionError:
                raise
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                return None

        if resp is None or resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        title_meta = soup.find("meta", attrs={"name": "DC.title"})
        title = title_meta["content"].strip() if title_meta and title_meta.get("content") else fallback_title

        date_meta = soup.find("meta", attrs={"name": "DC.date", "scheme": "issue"}) or soup.find(
            "meta", attrs={"name": "DC.date"}
        )
        if not pub_date and date_meta and date_meta.get("content"):
            pub_date = date_meta["content"].strip()

        if not family_id:
            fam_sec = soup.find(attrs={"itemprop": "family"})
            if fam_sec:
                fam_id_el = fam_sec.find(attrs={"itemprop": "id"})
                if fam_id_el:
                    family_id = fam_id_el.get_text(strip=True)
        if not family_id:
            family_id = pub_number

        abst_el = soup.find("section", attrs={"itemprop": "abstract"})
        desc_el = soup.find("section", attrs={"itemprop": "description"})
        claims_el = soup.find("section", attrs={"itemprop": "claims"})

        # Strip boilerplate section headers ("Abstract", "Description", "Claims") injected by Google Patents HTML
        for sec in (abst_el, desc_el, claims_el):
            if sec:
                h2 = sec.find("h2")
                if h2 and h2.get_text(strip=True).lower() in ("abstract", "description", "claims"):
                    h2.decompose()

        abst_txt, abst_cells = _extract_bs4_section_with_tables(abst_el)
        desc_txt, desc_cells = _extract_bs4_section_with_tables(desc_el)
        claims_txt, claims_cells = _extract_bs4_section_with_tables(claims_el)

        return PatentDocument(
            pub_number=pub_number,
            family_id=family_id,
            pub_date=pub_date,
            title=title,
            applicant=applicant,
            language=language,
            abstract_text=abst_txt,
            description_text=desc_txt,
            claims_text=claims_txt,
            table_cells=abst_cells + desc_cells + claims_cells,
            source_url=f"https://patents.google.com/patent/{pub_number}/{lang_slug}",
        )


# ==============================================================================
# 3. Local File & ZIP Archive Upload Parser (.zip, .docx, .xml, .txt)
# ==============================================================================

def _normalize_pub_date(raw_date: str, fallback_idx: int) -> str:
    """Normalize DD.MM.YYYY, DD/MM/YYYY, YYYYMMDD, or YYYY-MM-DD into sortable ISO YYYY-MM-DD."""
    s = (raw_date or "").strip()
    if not s:
        return f"2026-01-{min(fallback_idx, 28):02d}"
    # DD.MM.YYYY or DD/MM/YYYY
    m_dmy = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", s)
    if m_dmy:
        d, m, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        return f"{y:04d}-{m:02d}-{d:02d}"
    # YYYYMMDD
    m_compact = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m_compact:
        return f"{m_compact.group(1)}-{m_compact.group(2)}-{m_compact.group(3)}"
    # YYYY-MM-DD
    m_iso = re.match(r"^(\d{4})[./-](\d{1,2})[./-](\d{1,2})$", s)
    if m_iso:
        return f"{int(m_iso.group(1)):04d}-{int(m_iso.group(2)):02d}-{int(m_iso.group(3)):02d}"
    return s


def _detect_script_language(text: str, default: str = "EN") -> str:
    if not text:
        return default
    sample = text[:2000]
    if re.search(r"[\uac00-\ud7af]", sample):
        return "KO"
    if re.search(r"[\u3040-\u30ff]", sample):
        return "JA"
    if re.search(r"[\u4e00-\u9fff]", sample):
        return "ZH"
    return default


def parse_uploaded_files(
    files: list[tuple[str, bytes]],
    client_name: str,
    default_language: str = "EN",
) -> list[PatentDocument]:
    """Parse uploaded .zip bundles (including optional index.csv), .docx, .xml/.html, or .txt patent files."""
    import csv
    import zipfile

    expanded_files: list[tuple[str, bytes]] = []
    index_meta: dict[str, dict[str, str]] = {}

    for filename, raw_bytes in files:
        base_name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        ext = base_name.rsplit(".", 1)[-1].lower() if "." in base_name else ""

        if ext == "zip":
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                # First pass: check for index.csv metadata manifest
                for zname in zf.namelist():
                    if zname.endswith("/") or "__MACOSX" in zname:
                        continue
                    zbase = zname.replace("\\", "/").rsplit("/", 1)[-1]
                    if zbase.lower() == "index.csv":
                        csv_txt = zf.read(zname).decode("utf-8-sig", errors="replace")
                        for row in csv.DictReader(io.StringIO(csv_txt)):
                            fname_key = (row.get("file") or row.get("filename") or "").strip()
                            if fname_key:
                                index_meta[fname_key] = row
                                index_meta[fname_key.rsplit(".", 1)[0]] = row

                # Second pass: extract supported patent documents
                for zname in zf.namelist():
                    if zname.endswith("/") or "__MACOSX" in zname:
                        continue
                    zbase = zname.replace("\\", "/").rsplit("/", 1)[-1]
                    if zbase.startswith(".") or zbase.startswith("~$"):
                        continue
                    zext = zbase.rsplit(".", 1)[-1].lower() if "." in zbase else ""
                    if zext in ("docx", "xml", "html", "htm", "txt"):
                        expanded_files.append((zbase, zf.read(zname)))
        elif ext == "csv" and base_name.lower() == "index.csv":
            csv_txt = raw_bytes.decode("utf-8-sig", errors="replace")
            for row in csv.DictReader(io.StringIO(csv_txt)):
                fname_key = (row.get("file") or row.get("filename") or "").strip()
                if fname_key:
                    index_meta[fname_key] = row
                    index_meta[fname_key.rsplit(".", 1)[0]] = row
        else:
            expanded_files.append((base_name, raw_bytes))

    docs: list[PatentDocument] = []
    for idx, (filename, raw_bytes) in enumerate(expanded_files, start=1):
        stem = filename.rsplit(".", 1)[0]
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
        desc_text = ""
        title_text = stem
        table_cells: list[str] = []

        if ext == "docx":
            import docx

            d = docx.Document(io.BytesIO(raw_bytes))
            paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
            if paras:
                first_p = paras[0]
                for prefix in ("발명의 명칭:", "Title of Invention:", "Title:"):
                    if first_p.startswith(prefix):
                        title_text = first_p[len(prefix):].strip() or stem
                        break
            desc_text = "\n".join(paras)

            # Deduplicate underlying XML _tc elements so merged Word table cells are counted once
            seen_tcs: set[object] = set()
            for tbl in d.tables:
                for row in tbl.rows:
                    for cell in row.cells:
                        tc = getattr(cell, "_tc", None)
                        if tc is not None:
                            if tc in seen_tcs:
                                continue
                            seen_tcs.add(tc)
                        c_txt = cell.text.strip()
                        if c_txt:
                            table_cells.append(c_txt)
        elif ext in ("xml", "html", "htm"):
            soup = BeautifulSoup(raw_bytes.decode("utf-8", errors="replace"), "lxml")
            desc_text, table_cells = _extract_bs4_section_with_tables(soup)
        else:
            desc_text = raw_bytes.decode("utf-8", errors="replace")

        meta = index_meta.get(filename) or index_meta.get(stem) or {}
        pub_date = _normalize_pub_date(meta.get("publication_date") or meta.get("pub_date") or "", idx)
        raw_lang = (meta.get("language") or "").strip().upper()
        lang = raw_lang if raw_lang else _detect_script_language(desc_text, default=default_language)
        src_url = (meta.get("source_url") or meta.get("url") or "").strip()
        if not src_url and re.match(r"^(?:WO|EP|US|KR|JP|CN)\d+", stem, re.IGNORECASE):
            src_url = f"https://worldwide.espacenet.com/patent/search?q=pn%3D{stem}"

        docs.append(
            PatentDocument(
                pub_number=stem,
                family_id=f"UPLOAD-{stem}",
                pub_date=pub_date,
                title=title_text,
                applicant=client_name,
                language=lang,
                description_text=desc_text,
                table_cells=table_cells,
                source_url=src_url,
            )
        )
    return docs


# ==============================================================================
# 4. Realistic Demo Portfolio Generator (Offline Instant Simulation)
# ==============================================================================

def generate_demo_portfolio(
    client_name: str,
    sample_size: int = 30,
    profile: str = "pharma",
) -> list[PatentDocument]:
    """Generate a realistic chronological patent portfolio with authentic boilerplate, claims, NMR & numeric tables."""
    import random

    rng = random.Random(hash(client_name + profile) & 0xFFFFFFFF)
    docs: list[PatentDocument] = []

    targets = ["KCC2", "EGFR", "KRAS-G12D", "JAK1", "TYK2", "BTK", "NLRP3", "GLP-1R"]
    scaffolds = [
        "fused pyrimidine", "pyrrolopyrimidine", "azolopyridine",
        "quinazoline-4-one", "dihydroisoquinolinone", "pyrazolopyridazine",
    ]
    solvents = ["DMSO-d6", "CDCl3", "CD3OD"]
    indications = [
        "neurological disorders", "autoimmune conditions", "metabolic syndromes",
        "solid tumors", "inflammatory bowel disease", "fibrotic diseases",
    ]

    for i in range(1, sample_size + 1):
        year = 2024 + (i * 28) // 365
        month = ((i - 1) % 12) + 1
        day = ((i * 3) % 27) + 1
        pub_date = f"{year}-{month:02d}-{day:02d}"
        pub_num = f"EP{4200000 + i * 317}A1"

        target = targets[i % len(targets)] if i % 4 == 0 else targets[0]
        scaffold = scaffolds[i % len(scaffolds)] if i % 3 == 0 else scaffolds[0]
        indication = indications[i % len(indications)]

        title = f"Substituted {scaffold} derivatives as selective {target} modulators"
        abstract = (
            f"The present invention relates to compounds of Formula (I), or pharmaceutically acceptable salts, "
            f"solvates, or prodrugs thereof, which act as modulators of {target}. "
            f"The invention further relates to pharmaceutical compositions comprising said compounds and "
            f"their use in the manufacture of a medicament for treating {indication}."
        )

        # Shared client house-style clauses mixed with patent-specific paragraphs (creates realistic progressive TM curve)
        desc_paras = [
            f"[0001] FIELD OF THE INVENTION. The present disclosure relates generally to {scaffold} compounds that modulate {target} activity, pharmaceutical compositions thereof, and therapeutic uses in {indication}.",
            f"[0002] BACKGROUND OF THE INVENTION. Dysregulation of {target} signaling has been implicated in the pathogenesis of {indication} (see Smith et al., J. Med. Chem., Vol. {58 + (i % 5)}, No. 4, pp. 1120-1135).",
            "[0003] Despite recent advances, there remains a significant unmet medical need for potent, orally bioavailable small-molecule modulators exhibiting improved metabolic stability and reduced off-target hERG inhibition.",
            f"[0004] SUMMARY OF THE INVENTION. According to a first aspect of the invention, there is provided a compound of Formula (I) wherein R1 is selected from C1-C6 alkyl, C3-C6 cycloalkyl, haloalkyl, and 5- to 6-membered heteroaryl.",
            "[0005] In certain embodiments, the pharmaceutically acceptable salt is a hydrochloride, mesylate, tosylate, fumarate, succinate, or maleate salt.",
            "[0006] Pharmaceutical compositions of the present disclosure may be formulated for oral, parenteral, intravenous, subcutaneous, or topical administration in admixture with a pharmaceutically acceptable excipient, diluent, or carrier.",
            f"[0007] Referring to Fig. {(i % 3) + 1}, the crystalline polymorph Form A exhibits characteristic X-ray powder diffraction peaks at 2-theta angles of {8 + (i % 4)}.2, 14.6, and 21.8 degrees.",
        ]

        # Add unique chemical synthesis & NMR characterization paragraphs in pharma mode (triggers RTE NMR detector!)
        table_cells: list[str] = []
        if profile == "pharma":
            for ex_num in range(1, 5):
                cmpd_id = (i * 10) + ex_num
                solv = solvents[(i + ex_num) % len(solvents)]
                s1 = round(8.10 + rng.uniform(0.05, 0.85), 2)
                s2 = round(7.20 + rng.uniform(0.05, 0.75), 2)
                s3 = round(3.75 + rng.uniform(0.05, 0.65), 2)
                s4 = round(1.20 + rng.uniform(0.05, 0.45), 2)
                desc_paras.append(
                    f"[00{10 + ex_num}] Example {ex_num}: Preparation of Compound {cmpd_id}. "
                    f"To a stirred solution of Intermediate {ex_num} ({150 + ex_num * 25} mg, 0.{45 + ex_num} mmol) in anhydrous dioxane (5.0 mL) "
                    f"under nitrogen at {80 + (ex_num * 5)} C was added Pd(dppf)Cl2 (18 mg) and cesium carbonate (325 mg). "
                    f"The reaction mixture was cooled to ambient temperature, concentrated under reduced pressure, and purified by flash silica chromatography to afford Compound {cmpd_id} as an off-white solid. "
                    f"1H NMR (400 MHz, {solv}) δ {s1} (d, J = 8.4 Hz, 1H), {s2} (dd, J = 8.4, 2.1 Hz, 2H), "
                    f"6.94 (s, 1H), {s3} (q, J = 7.1 Hz, 2H), 2.48 (m, 3H), {s4} (t, J = 7.1 Hz, 3H). "
                    f"13C NMR (101 MHz, {solv}) δ 166.4, 158.2, 149.7, 136.5, 128.1, 119.4, 54.8, 31.2, 14.6. "
                    f"LCMS (ESI) m/z calcd: {380 + cmpd_id}.2."
                )

            # Biological assay table with whole numbers (triggers RTE whole-numbers-in-tables detector!)
            table_cells.extend(["Example No.", "Compound ID", "IC50 (nM)", "Selectivity Fold", "Microsomal Cl", "Oral F (%)"])
            for row_i in range(1, 16):
                table_cells.extend([
                    str(row_i),
                    str(i * 100 + row_i),
                    str(rng.randint(2, 450)),
                    str(rng.randint(50, 1200)),
                    str(rng.randint(4, 65)),
                    str(rng.randint(35, 92)),
                ])
        else:
            # MedTech / Automotive engineering paragraphs + numeric dimension table
            for sec_i in range(1, 5):
                desc_paras.append(
                    f"[00{10 + sec_i}] In accordance with embodiment {sec_i} of {client_name}, the control unit {100 + sec_i} "
                    f"receives sensor telemetry from actuator assembly {200 + (i % 6)} at a sampling interval of {10 * sec_i} ms "
                    f"and adjusts torque command signals to maintain closed-loop stability across operating modes."
                )
            table_cells.extend(["Parameter ID", "Test Cycle", "Peak Load (N)", "Cycles Completed", "Status"])
            for row_i in range(1, 10):
                table_cells.extend([str(row_i), str(1000 + row_i * 50), str(rng.randint(120, 980)), str(rng.randint(10000, 50000)), "PASS"])

        claims = "\n".join([
            f"1. A compound of Formula (I), or a pharmaceutically acceptable salt or solvate thereof, for modulating {target}, wherein R1 is C1-C6 alkyl or C3-C6 cycloalkyl and R2 is halo or cyano.",
            "2. The compound according to claim 1, wherein R1 is methyl, ethyl, isopropyl, or cyclopropyl.",
            "3. The compound according to claim 1 or claim 2, wherein R2 is fluoro or chloro.",
            f"4. A pharmaceutical composition comprising a therapeutically effective amount of a compound according to any one of claims 1 to 3 and at least one pharmaceutically acceptable excipient.",
            f"5. The compound according to any one of claims 1 to 3, or a pharmaceutically acceptable salt thereof, for use in therapy.",
            f"6. The compound according to any one of claims 1 to 3 for use in the treatment or prophylaxis of {indication}.",
        ])

        docs.append(
            PatentDocument(
                pub_number=pub_num,
                family_id=f"DEMO-FAM-{i:03d}",
                pub_date=pub_date,
                title=title,
                applicant=client_name,
                language="EN",
                abstract_text=abstract,
                description_text="\n\n".join(desc_paras),
                claims_text=claims,
                table_cells=table_cells,
                source_url=f"https://worldwide.espacenet.com/patent/search?q=pn%3D{pub_num}",
            )
        )

    return docs

