from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1 import jobs, platforms, scraping
from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger
from app.storage.database import ensure_indexes

_log = get_logger("app")

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    try:
        await ensure_indexes()
    except Exception as exc:
        _log.warning("event=index_setup_failed error=%s", exc)
    yield

app = FastAPI(title=settings.app_name, lifespan=lifespan)

@app.exception_handler(AppError)
async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )

@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}

for module in (scraping, jobs, platforms):
    app.include_router(module.router, prefix=settings.api_v1_prefix)
