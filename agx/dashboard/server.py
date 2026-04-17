"""
AG-X Community Edition — Local Dashboard Server

FastAPI + Jinja2 server-rendered dashboard.
No Node.js / Vite required. Alpine.js + TailwindCSS loaded from CDN.

Routes:
  GET  /                        Runs list (last 100, filterable)
  GET  /runs/<run_id>           Trace detail
  GET  /vaccines                Active vaccines (editable inline)
  GET  /scanner                 Upload logs → scan results
  POST /scanner                 Handle log file upload
  GET  /stream                  SSE stream for live run updates
  GET  /api/runs                JSON: recent runs
  GET  /api/runs/<run_id>       JSON: single run
  GET  /api/vaccines            JSON: all vaccines
  POST /api/vaccines/<agent>    JSON: save vaccine manifest
  POST /api/scanner             JSON: analyze uploaded log file

Local only — binds to 127.0.0.1 by default.
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Optional

log = logging.getLogger(__name__)


def _check_deps() -> None:
    try:
        import fastapi  # noqa: F401
        import jinja2  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        raise ImportError(
            "Dashboard dependencies not installed. "
            "Run: pip install agx-community[dashboard]"
        )


def create_app():
    """Create and return the FastAPI application."""
    _check_deps()

    from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    from agx._config import settings
    from agx._models import VaccineManifest
    from agx.scanner.analyzer import analyze
    from agx.scanner.yaml_exporter import export_yaml, import_yaml
    from agx.store import get_store

    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.filters["tojson"] = lambda v: json.dumps(v, default=str)

    app = FastAPI(
        title="AG-X Community Dashboard",
        description="Local agent safety observability — AG-X Community Edition",
        docs_url=None,
        redoc_url=None,
    )

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # SSE subscriber queues
    _sse_queues: list[asyncio.Queue] = []

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _broadcast_new_run(run: dict) -> None:
        """Push a new run dict to all active SSE subscribers."""
        payload = json.dumps(run, default=str)
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)

    def _cloud_mode_banner() -> Optional[str]:
        if settings.cloud_mode:
            return f"Connected to AG-X Cloud: {settings.endpoint}"
        return None

    # -----------------------------------------------------------------------
    # HTML pages
    # -----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        agent: Optional[str] = None,
        outcome: Optional[str] = None,
    ):
        store = get_store()
        runs = await store.list_runs(agent_name=agent, outcome=outcome, limit=100)
        agents = sorted({r["agent_name"] for r in runs})
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "runs": runs,
                "agents": agents,
                "filter_agent": agent or "",
                "filter_outcome": outcome or "",
                "cloud_banner": _cloud_mode_banner(),
                "agx_endpoint": settings.endpoint or "",
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: str):
        store = get_store()
        run = await store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        # Parse JSON fields
        try:
            run["vaccines_fired"] = json.loads(run.get("vaccines_fired") or "[]")
        except Exception:
            run["vaccines_fired"] = []
        try:
            run["metadata"] = json.loads(run.get("metadata") or "{}")
        except Exception:
            run["metadata"] = {}

        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {
                "run": run,
                "cloud_banner": _cloud_mode_banner(),
            },
        )

    @app.get("/vaccines", response_class=HTMLResponse)
    async def vaccines_page(request: Request):
        store = get_store()
        manifests = store.list_all_vaccines()
        return templates.TemplateResponse(
            request,
            "vaccines.html",
            {
                "manifests": [json.loads(m.model_dump_json()) for m in manifests],
                "cloud_banner": _cloud_mode_banner(),
            },
        )

    @app.get("/scanner", response_class=HTMLResponse)
    async def scanner_page(request: Request):
        return templates.TemplateResponse(
            request,
            "scanner.html",
            {
                "cloud_banner": _cloud_mode_banner(),
                "result": None,
            },
        )

    @app.post("/scanner", response_class=HTMLResponse)
    async def scanner_upload(
        request: Request,
        log_file: UploadFile = File(...),
        agent_name: str = Form(default=""),
    ):
        content = await log_file.read()
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            report = analyze(tmp_path, agent_name=agent_name or None)
            result = json.loads(report.model_dump_json())
        except Exception as exc:
            result = {"error": str(exc)}
        finally:
            os.unlink(tmp_path)

        return templates.TemplateResponse(
            request,
            "scanner.html",
            {
                "cloud_banner": _cloud_mode_banner(),
                "result": result,
            },
        )

    # -----------------------------------------------------------------------
    # JSON API
    # -----------------------------------------------------------------------

    @app.get("/api/runs")
    async def api_runs(
        agent: Optional[str] = None,
        outcome: Optional[str] = None,
        limit: int = 100,
    ):
        store = get_store()
        runs = await store.list_runs(agent_name=agent, outcome=outcome, limit=limit)
        return JSONResponse(runs)

    @app.get("/api/runs/{run_id}")
    async def api_run_detail(run_id: str):
        store = get_store()
        run = await store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return JSONResponse(run)

    @app.get("/api/vaccines")
    async def api_vaccines():
        store = get_store()
        manifests = store.list_all_vaccines()
        return JSONResponse([json.loads(m.model_dump_json()) for m in manifests])

    @app.post("/api/vaccines/{agent_name}")
    async def api_save_vaccine(agent_name: str, request: Request):
        body = await request.json()
        manifest = VaccineManifest.model_validate(body)
        store = get_store()
        store.save_vaccines(manifest)
        return JSONResponse({"status": "saved"})

    @app.post("/api/scanner")
    async def api_scanner(log_file: UploadFile = File(...), agent_name: str = Form(default="")):
        content = await log_file.read()
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            report = analyze(tmp_path, agent_name=agent_name or None)
            return JSONResponse(json.loads(report.model_dump_json()))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            os.unlink(tmp_path)

    # -----------------------------------------------------------------------
    # SSE stream
    # -----------------------------------------------------------------------

    @app.get("/stream")
    async def sse_stream(request: Request):
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        _sse_queues.append(queue)

        async def generator() -> AsyncGenerator[str, None]:
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield f"data: {data}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                if queue in _sse_queues:
                    _sse_queues.remove(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def run(host: str = "127.0.0.1", port: int = 7000) -> None:
    """Start the dashboard server (blocking)."""
    _check_deps()
    import uvicorn

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="warning")
