"""EventAlpha FastAPI application."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes, stocks
from app.config import get_settings
from app.services.analysis import AnalysisError
from app.services.http import ProviderError

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("eventalpha.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    if settings.embedded_worker:
        from app.worker.main import loop

        threading.Thread(target=loop, args=(stop,), daemon=True, name="embedded-worker").start()
        log.info("embedded worker started (EMBEDDED_WORKER=true)")
    yield
    stop.set()


app = FastAPI(
    title="EventAlpha API",
    version=settings.api_version,
    description=(
        "AI-assisted stock research: market data (Massive), SEC EDGAR financials, news/event radar, "
        "OpenAI-assisted theses and quantitative bear/base/bull valuation. Every financial value is labeled "
        "as observed data, external estimate, or model forecast. Not investment advice."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    start = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = rid
    response.headers["X-API-Version"] = settings.api_version
    if request.url.path.startswith("/api"):
        log.info("%s %s %s %.0fms rid=%s", request.method, request.url.path, response.status_code, ms, rid)
    return response


@app.exception_handler(HTTPException)
async def http_exc(_: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(AnalysisError)
async def analysis_exc(_: Request, exc: AnalysisError):
    return JSONResponse(status_code=400, content={"error": {"code": exc.code, "message": str(exc)}})


@app.exception_handler(ProviderError)
async def provider_exc(_: Request, exc: ProviderError):
    return JSONResponse(status_code=502, content={"error": {"code": "provider_error", "provider": exc.provider,
                                                            "message": exc.message}})


app.include_router(routes.router)
app.include_router(stocks.router)
