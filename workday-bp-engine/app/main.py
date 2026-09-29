"""Application factory.

Run with ``python -m app`` (see ``app/__main__.py``) or
``uvicorn app.main:create_app --factory --reload``.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm.exc import StaleDataError

from .api.routes import router
from .db import Base, make_engine, make_session_factory
from .engine.definitions import DefinitionRegistry
from .engine.engine import Clock, EngineError, SystemClock
from .seed import seed

BASE_DIR = Path(__file__).resolve().parent.parent
DEFINITIONS_DIR = BASE_DIR / "definitions"
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DATABASE_URL = os.getenv("BP_DATABASE_URL", f"sqlite:///{BASE_DIR / 'bp_engine.db'}")

DESCRIPTION = """
A Workday-style **business process engine** built for learning.

* Business process definitions with ordered and parallel steps (YAML files)
* Condition rules: entry, validation and while-running conditions
* Role-based security on supervisory organizations with inheritance
* Approvals, approval chains, sub-processes, To Dos and integration steps
* Approve, Deny, Send Back, Cancel and Rescind

Identity is simulated with the `X-User-Id` header (for example `W1005`).
Not affiliated with or endorsed by Workday, Inc.
"""


def create_app(
    database_url: str | None = None,
    clock: Clock | None = None,
    definitions_dir: Path | None = None,
) -> FastAPI:
    registry = DefinitionRegistry.from_directory(definitions_dir or DEFINITIONS_DIR)
    db_engine = make_engine(database_url or DEFAULT_DATABASE_URL)
    Base.metadata.create_all(db_engine)
    session_factory = make_session_factory(db_engine)
    with session_factory() as session:
        seed(session)
        session.commit()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        db_engine.dispose()  # close pooled database connections on shutdown

    app = FastAPI(title="BP Engine", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
    app.state.registry = registry
    app.state.db_engine = db_engine
    app.state.session_factory = session_factory
    app.state.clock = clock or SystemClock()

    @app.exception_handler(EngineError)
    async def engine_error_handler(_request: Request, exc: EngineError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "errors": exc.errors})

    @app.exception_handler(StaleDataError)
    async def stale_data_handler(_request: Request, _exc: StaleDataError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "Someone else changed this business process at the same time. Reload and try again.", "errors": []},
        )

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
