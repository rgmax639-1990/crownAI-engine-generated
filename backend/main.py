"""bank-website API: modular monolith (FastAPI + SQLite for local/dev).

Run: uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import seed_core  # noqa: F401  (registers the core synthetic seed)
from integrations.base import IntegrationError
from integrations.finacle import FinacleBusinessError
from routers import (accounts, admin, assistant, auth, beneficiaries, branches, calculators, cards, cheques, cms, complaints,
                     consents, content, corporate, deposits, fraud, insights, leads, loans, lrs, onboarding, ops, payments,
                     profile, search, status, system, transfers, vcip, wealth)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="bank-website API",
    version="1.0.0",
    description="Digital banking platform API. All external institutions run on MOCK adapters until a live "
    "integration is approved. Synthetic data only outside production.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload")
    path = request.url.path
    if not path.startswith("/public") and path not in ("/health", "/status", "/status/maintenance-notices"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(IntegrationError)
async def integration_error(_request: Request, exc: IntegrationError):
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": {"code": exc.code, "message": exc.message, "provider": exc.provider}})


@app.exception_handler(FinacleBusinessError)
async def finacle_error(_request: Request, exc: FinacleBusinessError):
    return JSONResponse(status_code=422, content={"detail": {"code": exc.code, "message": exc.message, "source": "finacle"}})


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


for module in (auth, system, content, search, cms, branches, calculators, consents, leads, complaints, fraud, status,
               onboarding, vcip, loans, accounts, beneficiaries, transfers, cards, payments, deposits, cheques, profile,
               insights, wealth, lrs, corporate, admin, ops, assistant):
    app.include_router(module.router)
