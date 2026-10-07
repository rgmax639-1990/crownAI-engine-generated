import os
import sys
from contextlib import asynccontextmanager

# Modules here import each other as top-level names (`from db import ...`).
# Put this directory on sys.path so the app loads the same way whether it is
# started from backend/ (`uvicorn main:app`) or from the repo root
# (`uvicorn backend.main:app`) -- never a half-imported app without routes.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from db import init_db  # noqa: E402
from routers import auth, consent, contact, pricing, projects  # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="CrownAI-website API",
    description=(
        "Backend for Crownwright Technologies Pvt Ltd's corporate site and the "
        "Crown AI SDLC/STLC automation tool."
    ),
    version="1.0.0",
)

# Any localhost origin is always allowed (dev port is not fixed). A deployed
# frontend origin (FRONTEND_URL) is additionally allowlisted so the same
# backend works in production without loosening the localhost regex.
_deployed_frontend_url = os.environ.get("FRONTEND_URL", "")
_extra_origins = (
    [_deployed_frontend_url]
    if _deployed_frontend_url and "localhost" not in _deployed_frontend_url and "127.0.0.1" not in _deployed_frontend_url
    else []
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_origins=_extra_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


# Each router's routes already carry their full path (auth's "/auth" prefix
# is set on its APIRouter), so mount the APIRoute objects on the app directly.
# This keeps every endpoint a plain APIRoute in app.routes -- the same app
# whichever directory it's started from and whichever FastAPI version wraps
# include_router() differently.
for _router_module in (auth, projects, pricing, contact, consent):
    app.router.routes.extend(_router_module.router.routes)
