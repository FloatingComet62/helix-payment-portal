from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import close_db, init_db, ping_db
from app.routers import admin, export, payment, portal, webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — initialising MongoDB connection and indexes")
    await init_db(app)
    yield
    logger.info("Shutting down — closing MongoDB connection")
    await close_db(app)


app = FastAPI(
    title="Central Payment & Fest Accommodation Portal",
    description=(
        "Multi-tenant payment middleware connecting external fest frontends "
        "to the EaseBuzz payment gateway. Handles payment initiation, "
        "S2S webhook processing, and signed callbacks to fest backends."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_origin_regex=settings.CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(portal.router)
app.include_router(payment.router)
app.include_router(webhook.router)
app.include_router(admin.router)
app.include_router(export.router)


@app.get("/health", tags=["Health"])
async def health_check(request: Request):
    db_ok = await ping_db(request.app)
    if not db_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"status": "degraded", "db": "unreachable"})
    return {"status": "ok", "db": "ok"}
