from __future__ import annotations

import ipaddress
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import psycopg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .db import connect, init_db, log_access
from .service import (
    analyze_complaint,
    approve_draft,
    generate_draft,
    generate_unanswered_review_drafts,
    get_runtime_options,
    publish_draft,
    sync_all,
    update_runtime_options,
)
from .settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
PUBLIC_PATHS = {"/health", "/login"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(settings)
    yield


app = FastAPI(title="WB Review Assistant", version="0.2.0", lifespan=lifespan, docs_url=None)


class RuntimeOptionsInput(BaseModel):
    automation_enabled: bool = False
    sync_interval_seconds: int = 3600


def _credentials(request: Request) -> tuple[str, str]:
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
    if bearer:
        return bearer, "api-token"
    cookie = request.cookies.get("wb_access_token", "")
    return cookie, "web-operator"


@app.middleware("http")
async def security_and_access_audit(request: Request, call_next):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    supplied, actor = _credentials(request)
    authenticated = bool(supplied) and secrets.compare_digest(supplied, settings.app_access_token)
    request.state.request_id = request_id
    request.state.actor = actor if authenticated else "anonymous"

    if request.url.path not in PUBLIC_PATHS and not authenticated:
        response = (
            JSONResponse(
                {"detail": "Unauthorized", "request_id": request_id},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            if request.url.path.startswith("/api/")
            else RedirectResponse("/login", status_code=303)
        )
    elif (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and actor == "web-operator"
        and request.headers.get("x-requested-with") != "wb-review-assistant"
        and request.url.path != "/login"
    ):
        response = JSONResponse(
            {"detail": "CSRF protection failed", "request_id": request_id}, status_code=403
        )
    else:
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000)
            _safe_access_log(request, request_id, actor, authenticated, 500, duration_ms)
            raise

    response.headers["X-Request-ID"] = request_id
    duration_ms = round((time.perf_counter() - started) * 1000)
    _safe_access_log(request, request_id, actor, authenticated, response.status_code, duration_ms)
    return response


def _safe_access_log(
    request: Request,
    request_id: str,
    actor: str,
    authenticated: bool,
    status_code: int,
    duration_ms: int,
) -> None:
    try:
        log_access(
            settings,
            request_id=request_id,
            actor=actor if authenticated else "anonymous",
            authenticated=authenticated,
            method=request.method,
            path=request.url.path,
            query_keys=sorted(request.query_params.keys()),
            status_code=status_code,
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:500],
            duration_ms=duration_ms,
        )
    except psycopg.Error:
        # Access logging must not disclose credentials or replace the original response.
        logger.exception("Access audit write failed", extra={"request_id": request_id})


def _client_ip(request: Request) -> str | None:
    if not request.client:
        return None
    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return None


@app.get("/health")
def health():
    try:
        with connect(settings) as conn:
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        return {"status": "ok", "database": "postgresql", "schema_version": version["value"]}
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc


@app.get("/login", response_class=HTMLResponse)
def login_form():
    return HTMLResponse(
        """<!doctype html><html lang='ru'><meta charset='utf-8'><title>Вход</title>
        <body style='font-family:system-ui;max-width:420px;margin:10vh auto'>
        <h1>WB Review Assistant</h1><form method='post'><label>Токен доступа<br>
        <input type='password' name='token' required style='width:100%;padding:10px;margin:8px 0'></label>
        <button style='padding:10px 18px'>Войти</button></form></body></html>"""
    )


@app.post("/login")
def login(token: str = Form(...)):
    if not secrets.compare_digest(token, settings.app_access_token):
        raise HTTPException(401, "Invalid token")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "wb_access_token",
        token,
        httponly=True,
        samesite="strict",
        secure=settings.app_env == "production",
        max_age=86400,
    )
    return response


def _dashboard() -> dict:
    with connect(settings) as conn:
        summary = dict(
            conn.execute(
                """SELECT (SELECT count(*) FROM products) products,
                (SELECT count(*) FROM interactions) interactions,
                (SELECT count(*) FROM interactions WHERE status='new') new_items,
                (SELECT count(*) FROM interactions WHERE kind='review' AND answered=false
                    AND status <> 'archived') unanswered_reviews,
                (SELECT count(*) FROM drafts) drafts,
                (SELECT count(*) FROM drafts WHERE status='needs_human') needs_human,
                (SELECT count(*) FROM complaints WHERE eligible=true) complaint_candidates,
                (SELECT count(*) FROM audit_log) audit_events,
                (SELECT count(*) FROM access_log) access_events"""
            ).fetchone()
        )
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT i.*,p.name product_name,d.id draft_id,d.goal,d.text draft_text,
                d.status draft_status,d.confidence
                FROM interactions i LEFT JOIN products p ON p.nm_id=i.nm_id
                LEFT JOIN LATERAL (
                    SELECT * FROM drafts WHERE interaction_id=i.id ORDER BY created_at DESC LIMIT 1
                ) d ON true
                ORDER BY COALESCE(i.created_at_source,i.synced_at) DESC LIMIT 200"""
            )
        ]
        sync_row = conn.execute(
            "SELECT value_json,updated_at FROM sync_state WHERE key='last_sync'"
        ).fetchone()
    return {"summary": summary, "items": rows, "last_sync": dict(sync_row) if sync_row else None}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            **_dashboard(),
            "auto_publish": settings.auto_publish,
            "runtime_options": get_runtime_options(settings),
            "wb_connected": bool(settings.wb_api_token),
            "openrouter_connected": bool(settings.openrouter_api_key),
            "llm_mode": settings.llm_mode,
            "openrouter_model": settings.openrouter_model,
        },
    )


@app.get("/api/summary")
def api_summary():
    return _dashboard()["summary"]


@app.get("/api/interactions")
def api_interactions():
    return _dashboard()["items"]


@app.post("/api/sync")
def api_sync(request: Request):
    try:
        return {
            "status": "ok",
            "counts": sync_all(
                settings,
                actor=request.state.actor,
                request_id=request.state.request_id,
            ),
        }
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"WB API returned HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "WB API is temporarily unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/drafts/generate-unanswered-reviews")
def api_generate_unanswered_reviews(request: Request):
    try:
        return {
            "status": "ok",
            **generate_unanswered_review_drafts(
                settings,
                actor=request.state.actor,
                request_id=request.state.request_id,
            ),
        }
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/settings")
def api_settings():
    return {
        **get_runtime_options(settings),
        "wb_connected": bool(settings.wb_api_token),
        "openrouter_connected": bool(settings.openrouter_api_key),
        "llm_mode": settings.llm_mode,
        "openrouter_model": settings.openrouter_model,
        "live_publish_enabled": settings.auto_publish,
    }


@app.post("/api/settings")
def api_update_settings(request: Request, payload: RuntimeOptionsInput):
    try:
        return update_runtime_options(
            settings,
            automation_enabled=payload.automation_enabled,
            sync_interval_seconds=payload.sync_interval_seconds,
            actor=request.state.actor,
            request_id=request.state.request_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/audit")
def api_audit(limit: int = 200):
    with connect(settings) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT %s", (min(limit, 1000),)
            )
        ]


@app.get("/api/access-log")
def api_access_log(limit: int = 200):
    with connect(settings) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM access_log ORDER BY created_at DESC LIMIT %s", (min(limit, 1000),)
            )
        ]


@app.post("/api/interactions/{interaction_id}/generate")
def api_generate(request: Request, interaction_id: str):
    try:
        return {
            "draft_id": generate_draft(
                settings,
                interaction_id,
                actor=request.state.actor,
                request_id=request.state.request_id,
            )
        }
    except KeyError as exc:
        raise HTTPException(404, "Interaction not found") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"OpenRouter returned HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "OpenRouter is temporarily unavailable") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/interactions/{interaction_id}/complaint")
def api_complaint(request: Request, interaction_id: str):
    try:
        return analyze_complaint(
            settings,
            interaction_id,
            actor=request.state.actor,
            request_id=request.state.request_id,
        )
    except KeyError as exc:
        raise HTTPException(404, "Review not found") from exc


@app.post("/api/drafts/{draft_id}/approve")
def api_approve(request: Request, draft_id: int):
    try:
        approve_draft(
            settings, draft_id, actor=request.state.actor, request_id=request.state.request_id
        )
        return {"status": "approved"}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/drafts/{draft_id}/publish")
def api_publish(request: Request, draft_id: int):
    try:
        return {
            "status": "published",
            "run_id": publish_draft(
                settings, draft_id, actor=request.state.actor, request_id=request.state.request_id
            ),
        }
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
