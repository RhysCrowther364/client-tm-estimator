"""Starlette + SSE web application for the Patent TM Suitability & Savings Estimator."""
from __future__ import annotations

import re
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app.config import STATIC_DIR, AppSettings, load_settings, save_settings
from app.models import JobCreateRequest
from app.patent_cache import clear_patent_cache, get_cache_stats
from app.patent_sources import EPOOPSClient, GooglePatentsClient
from app.pdf_exporter import export_job_to_pdf
from app.store import JobManager

job_manager = JobManager()


async def index_page(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html")


async def get_settings_api(request: Request) -> JSONResponse:
    s = load_settings()
    data = s.model_dump()
    data["has_epo_credentials"] = bool(s.epo_ops_key and s.epo_ops_secret)
    # Mask secret for display unless empty
    if data["epo_ops_secret"]:
        sec = data["epo_ops_secret"]
        data["epo_ops_secret_masked"] = ("*" * max(4, len(sec) - 4)) + sec[-4:]
    else:
        data["epo_ops_secret_masked"] = ""
    data.pop("epo_ops_secret", None)
    data.update(get_cache_stats())
    return JSONResponse(data)


async def clear_cache_api(request: Request) -> JSONResponse:
    clear_patent_cache()
    return JSONResponse(get_cache_stats())


async def update_settings_api(request: Request) -> JSONResponse:
    body = await request.json()
    current = load_settings()
    merged = current.model_dump()
    for k, v in body.items():
        if k == "epo_ops_secret":
            # Only overwrite secret if non-empty string explicitly provided (or clear_secret flag)
            if v or body.get("clear_credentials"):
                merged["epo_ops_secret"] = v
        elif k in merged and v is not None:
            merged[k] = v

    updated = save_settings(AppSettings(**merged))
    out = updated.model_dump()
    out["has_epo_credentials"] = bool(updated.epo_ops_key and updated.epo_ops_secret)
    out.pop("epo_ops_secret", None)
    return JSONResponse(out)


async def search_applicants_api(request: Request) -> JSONResponse:
    query = (request.query_params.get("q") or "").strip()
    language = (request.query_params.get("language") or "EN").strip().upper()
    if len(query) < 2:
        return JSONResponse({"error": "Please enter at least 2 characters."}, status_code=400)

    settings = load_settings()
    try:
        if settings.epo_ops_key and settings.epo_ops_secret:
            try:
                epo = EPOOPSClient(settings.epo_ops_key, settings.epo_ops_secret)
                res = await epo.search_applicants(query)
                return JSONResponse(res.model_dump())
            except Exception as epo_exc:
                gp = GooglePatentsClient()
                res = await gp.search_applicants(query, language=language)
                res.note = f"EPO OPS lookup failed ({epo_exc}); showing live Google Patents results."
                return JSONResponse(res.model_dump())
        else:
            gp = GooglePatentsClient()
            res = await gp.search_applicants(query, language=language)
            return JSONResponse(res.model_dump())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def list_jobs_api(request: Request) -> JSONResponse:
    return JSONResponse({"jobs": job_manager.list_jobs()})


async def create_job_api(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        req = JobCreateRequest(**body)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if not req.client_name.strip():
        return JSONResponse({"error": "Client name is required."}, status_code=400)

    job = job_manager.start_job(req)
    return JSONResponse(job.model_dump(), status_code=201)


async def create_upload_job_api(request: Request) -> JSONResponse:
    form = await request.form()
    client_name = str(form.get("client_name") or "Uploaded Portfolio").strip()
    include_rte = str(form.get("include_rte") or "false").lower() in ("true", "1", "on", "yes")

    files_list: list[tuple[str, bytes]] = []
    for item in form.getlist("files"):
        if hasattr(item, "filename") and hasattr(item, "read"):
            content = await item.read()
            if content:
                files_list.append((item.filename or "patent.txt", content))

    if not files_list:
        return JSONResponse({"error": "Please upload at least one .docx, .xml, or .txt file."}, status_code=400)

    job = job_manager.start_upload_job(client_name, include_rte, files_list)
    return JSONResponse(job.model_dump(), status_code=201)


async def get_job_api(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    job = job_manager.get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    return JSONResponse(job.model_dump())


async def toggle_job_rte_api(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    body = await request.json()
    include_rte = bool(body.get("include_rte", False))
    job = job_manager.toggle_job_rte(job_id, include_rte=include_rte)
    if not job:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    return JSONResponse(job.model_dump())


async def delete_job_api(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    deleted = job_manager.delete_job(job_id)
    if not deleted:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    return JSONResponse({"deleted": True})


async def job_events_sse(request: Request) -> StreamingResponse:
    job_id = request.path_params["job_id"]

    async def _event_stream():
        async for payload in job_manager.subscribe(job_id):
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def export_pdf_report_api(request: Request) -> Response:
    job_id = request.path_params["job_id"]
    job = job_manager.get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found."}, status_code=404)

    rte_param = request.query_params.get("include_rte")
    include_rte = job.include_rte if rte_param is None else (rte_param.lower() in ("true", "1", "yes"))

    settings = load_settings()
    pdf_bytes = export_job_to_pdf(job, include_rte=include_rte, settings=settings)
    safe_client = re.sub(r"[^A-Za-z0-9_-]+", "_", job.client_name).strip("_") or "Client"
    date_part = (job.created_at or "")[:10] or "Assessment"
    rte_suffix = "_with_RTE" if include_rte else ""
    filename = f"RWS_TM_Suitability_Report_{safe_client}_{date_part}{rte_suffix}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


routes = [
    Route("/", index_page, methods=["GET"]),
    Route("/api/settings", get_settings_api, methods=["GET"]),
    Route("/api/settings", update_settings_api, methods=["PUT"]),
    Route("/api/cache/clear", clear_cache_api, methods=["POST"]),
    Route("/api/applicants/search", search_applicants_api, methods=["GET"]),
    Route("/api/jobs", list_jobs_api, methods=["GET"]),
    Route("/api/jobs", create_job_api, methods=["POST"]),
    Route("/api/jobs/upload", create_upload_job_api, methods=["POST"]),
    Route("/api/jobs/{job_id}", get_job_api, methods=["GET"]),
    Route("/api/jobs/{job_id}/rte", toggle_job_rte_api, methods=["PATCH"]),
    Route("/api/jobs/{job_id}", delete_job_api, methods=["DELETE"]),
    Route("/api/jobs/{job_id}/events", job_events_sse, methods=["GET"]),
    Route("/api/jobs/{job_id}/report.pdf", export_pdf_report_api, methods=["GET"]),
    Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

app = Starlette(debug=False, routes=routes)

