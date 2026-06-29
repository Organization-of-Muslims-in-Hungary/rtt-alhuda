"""FastAPI application factory and lifespan."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from aiohttp import ClientSession
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from rtt_alhuda import config
from rtt_alhuda.config import validate_auth_config
from rtt_alhuda.database import dispose_engine, get_engine, get_session_factory
from rtt_alhuda.db_models import Base, Organization, Role, User, UserStatus
from rtt_alhuda.openrouter_debug import log_startup_summary
from rtt_alhuda.routers import (
    admin,
    auth,
    browser,
    control,
    devices,
    health,
    network,
    server,
    streams,
)
from rtt_alhuda.security import hash_password
from rtt_alhuda.session_manager import SessionManager


def log(*parts: object) -> None:
    """Print a timestamped log line to stdout."""
    print(f"[{time.strftime('%H:%M:%S')}]", *parts)


async def _seed_defaults() -> None:
    """Create the default org and superadmin user when the DB is empty."""
    factory = get_session_factory()
    async with factory() as db:
        # Default org.
        result = await db.execute(
            select(Organization).where(Organization.slug == config.DEFAULT_ORG_SLUG)
        )
        org = result.scalar_one_or_none()
        if org is None:
            org = Organization(
                name=config.DEFAULT_ORG_SLUG.capitalize(),
                slug=config.DEFAULT_ORG_SLUG,
            )
            db.add(org)
            await db.commit()
            await db.refresh(org)

        # Superadmin user (only if no users exist at all).
        count_result = await db.execute(select(func.count()).select_from(User))
        user_count = int(count_result.scalar() or 0)
        if user_count == 0:
            user = User(
                org_id=org.id,
                email=config.DEFAULT_ADMIN_EMAIL,
                password_hash=hash_password(config.DEFAULT_ADMIN_PASSWORD or "changeme"),
                role=Role.superadmin,
                status=UserStatus.active,
                approved_at=datetime.now(timezone.utc),
            )
            db.add(user)
            await db.commit()
            log(f"Seeded superadmin user '{config.DEFAULT_ADMIN_EMAIL}'")
            log(f"Default organization slug: '{config.DEFAULT_ORG_SLUG}'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    validate_auth_config()

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _seed_defaults()

    app.state.http_client = ClientSession()
    app.state.session_manager = SessionManager()

    log_startup_summary()
    yield

    await app.state.session_manager.stop_all()
    await app.state.http_client.close()
    await dispose_engine()


def create_app() -> FastAPI:
    """Build and wire the FastAPI application."""
    app = FastAPI(title="rtt-alhuda", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(devices.router)
    app.include_router(control.router)
    app.include_router(browser.router)
    app.include_router(server.router)
    app.include_router(network.router)
    app.include_router(streams.router)

    return app
