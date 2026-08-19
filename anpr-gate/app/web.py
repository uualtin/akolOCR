from __future__ import annotations

import csv
import io
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import psycopg
import redis
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .anpr.normalize import normalize_plate
from .db import Database, DatabaseUnavailable
from .gate import ProductionGate
from .logging_config import configure_logging
from .prod_config import ProductionSettings, read_secret
from .security import SessionManager, client_ip, verify_password

logger = logging.getLogger("anpr.web")
ROOT = Path(__file__).resolve().parent
settings = ProductionSettings.from_env()
database = Database(settings.database_url)
redis_client = redis.Redis.from_url(settings.redis_url)
sessions = SessionManager(
    redis_client,
    settings.session_cookie_name,
    settings.session_ttl_seconds,
    settings.secure_cookies,
)
templates = Jinja2Templates(directory=ROOT / "templates")


def format_local_time(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(ZoneInfo(settings.site_timezone)).strftime("%d.%m.%Y %H:%M:%S")


templates.env.filters["local_time"] = format_local_time


def template_context(request: Request, session: dict | None = None, **extra):
    return {
        "request": request,
        "session": session,
        "csrf": session["csrf"] if session else "",
        "site_timezone": settings.site_timezone,
        **extra,
    }


def page_session(request: Request):
    session = sessions.get(request)
    if session is None:
        return None, RedirectResponse("/login", status_code=303)
    return session, None


def local_date(value: str, end: bool = False) -> datetime | None:
    if not value:
        return None
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(
        tzinfo=ZoneInfo(settings.site_timezone)
    )
    if end:
        parsed += timedelta(days=1)
    return parsed.astimezone(UTC)


def gate_from_environment(gate_id: str) -> ProductionGate:
    prefix = gate_id.upper().replace("-", "_")
    return ProductionGate(
        os.getenv(f"{prefix}_GATE_DRIVER", "disabled").lower(),
        os.getenv(f"{prefix}_GATE_URL", ""),
        read_secret(f"{prefix}_GATE_TOKEN"),
    )


def publish_access_change(plate: str) -> None:
    try:
        redis_client.publish("access-list:changed", plate)
    except redis.RedisError:
        logger.warning("access-list invalidation publish failed; workers will poll")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        database.initialize()
    except DatabaseUnavailable:
        logger.exception("database unavailable at web startup")
    yield


app = FastAPI(title="ANPR Gate", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self' https://unpkg.com; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    if settings.secure_cookies:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if sessions.get(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html", context=template_context(request, error="")
    )


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(), password: str = Form()):
    ip = client_ip(request)
    if sessions.login_rate_limited(username, ip):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=template_context(request, error="Çok fazla deneme. 15 dakika bekleyin."),
            status_code=429,
        )
    try:
        user = database.get_user_by_username(username)
    except DatabaseUnavailable:
        user = None
    locked = bool(user and user["locked_until"] and user["locked_until"] > datetime.now(UTC))
    if not user or not user["is_active"] or locked or not verify_password(user["password_hash"], password):
        sessions.login_failed(username, ip)
        if user:
            database.login_failed(user["id"])
        try:
            database.create_audit(
                user["id"] if user else None,
                "login.failed",
                metadata={"username": username[:64]},
                client_ip=ip,
            )
        except DatabaseUnavailable:
            pass
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=template_context(request, error="Kullanıcı adı veya parola hatalı."),
            status_code=401,
        )
    database.login_succeeded(user["id"])
    sessions.login_succeeded(username, ip)
    database.create_audit(user["id"], "login", client_ip=ip)
    token, _ = sessions.create(user)
    response = RedirectResponse("/", status_code=303)
    sessions.set_cookie(response, token)
    return response


@app.post("/logout")
def logout(request: Request, csrf: str = Form()):
    session = sessions.require(request)
    sessions.require_csrf(session, csrf)
    database.create_audit(session["user_id"], "logout", client_ip=client_ip(request))
    sessions.destroy(request)
    response = RedirectResponse("/login", status_code=303)
    sessions.clear_cookie(response)
    return response


def worker_statuses() -> list[dict]:
    statuses = []
    try:
        cameras = database.list_cameras()
    except DatabaseUnavailable:
        cameras = []
    for camera in cameras:
        try:
            raw = redis_client.get(f"heartbeat:{camera['id']}")
        except redis.RedisError:
            raw = None
        heartbeat = json.loads(raw) if raw else {}
        statuses.append({**camera, **heartbeat, "online": bool(raw)})
    return statuses


def archive_status() -> dict:
    path = settings.archive_root / ".anpr-health.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(payload["updated_at"])
        payload["online"] = datetime.now(UTC) - updated < timedelta(hours=2)
        return payload
    except (OSError, ValueError, KeyError, TypeError):
        return {"online": False}


def spool_usage_bytes() -> int:
    total = 0
    if not settings.local_spool_root.exists():
        return total
    for path in settings.local_spool_root.rglob("*.jpg"):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    session, redirect = page_session(request)
    if redirect:
        return redirect
    try:
        events = database.recent_events(10)
        local_now = datetime.now(ZoneInfo(settings.site_timezone))
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        counts = database.event_counts_today(local_midnight.astimezone(UTC))
        gates = database.list_gates()
        db_online = True
    except DatabaseUnavailable:
        events, gates = [], []
        counts = {"total": 0, "success": 0, "failed": 0, "disabled": 0}
        db_online = False
    archive = archive_status()
    spool_bytes = spool_usage_bytes()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=template_context(
            request,
            session,
            events=events,
            counts=counts,
            gates=gates,
            workers=worker_statuses(),
            db_online=db_online,
            archive_online=archive["online"],
            spool_bytes=spool_bytes,
            spool_limit=settings.spool_limit_bytes,
        ),
    )


@app.get("/stream/{camera_id}.mjpeg")
def stream(request: Request, camera_id: str):
    sessions.require(request)

    def frames():
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(f"preview-stream:{camera_id}")
        try:
            initial = redis_client.get(f"preview:{camera_id}")
            if initial:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + initial + b"\r\n"
            while True:
                message = pubsub.get_message(timeout=2)
                if message and message["type"] == "message":
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + message["data"] + b"\r\n"
        finally:
            pubsub.close()

    return StreamingResponse(
        frames(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/events", response_class=HTMLResponse)
def events_page(
    request: Request,
    plate: str = "",
    camera_id: str = "",
    source: str = "",
    trigger_status: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
):
    session, redirect = page_session(request)
    if redirect:
        return redirect
    page = max(1, page)
    events = database.list_events(
        plate=normalize_plate(plate) if plate else "",
        camera_id=camera_id,
        source=source,
        trigger_status=trigger_status,
        date_from=local_date(date_from),
        date_to=local_date(date_to, end=True),
        limit=50,
        offset=(page - 1) * 50,
    )
    filters = {
        "plate": plate,
        "camera_id": camera_id,
        "source": source,
        "trigger_status": trigger_status,
        "date_from": date_from,
        "date_to": date_to,
    }
    return templates.TemplateResponse(
        request=request,
        name="events.html",
        context=template_context(
            request,
            session,
            events=events,
            cameras=database.list_cameras(),
            filters=filters,
            filter_query=urlencode(filters),
            page=page,
        ),
    )


@app.get("/events.csv")
def events_csv(
    request: Request,
    plate: str = "",
    camera_id: str = "",
    source: str = "",
    trigger_status: str = "",
    date_from: str = "",
    date_to: str = "",
):
    sessions.require(request)
    rows = database.list_events(
        plate=normalize_plate(plate) if plate else "",
        camera_id=camera_id,
        source=source,
        trigger_status=trigger_status,
        date_from=local_date(date_from),
        date_to=local_date(date_to, end=True),
        limit=100000,
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "occurred_at_utc", "source", "camera", "gate", "plate",
        "confidence", "status", "duration_ms", "error", "snapshot_status",
        "requested_by", "manual_reason", "client_ip",
    ])
    for row in rows:
        writer.writerow(
            [row[key] for key in (
                "id", "occurred_at", "source", "camera_id", "gate_id", "plate",
                "confidence", "trigger_status", "trigger_duration_ms", "trigger_error",
                "snapshot_status", "requested_by", "manual_reason", "client_ip",
            )]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=gate-events.csv"},
    )


@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: str):
    session, redirect = page_session(request)
    if redirect:
        return redirect
    event = database.get_event(event_id)
    if not event:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request=request,
        name="event_detail.html",
        context=template_context(request, session, event=event),
    )


def snapshot_file(path_value: str) -> Path:
    if ":" not in path_value:
        raise HTTPException(404)
    location, relative = path_value.split(":", 1)
    if location not in {"local", "archive"}:
        raise HTTPException(404)
    root = settings.local_spool_root if location == "local" else settings.archive_root
    candidate = (root / relative).resolve()
    if root.resolve() not in candidate.parents or not candidate.is_file():
        raise HTTPException(404)
    return candidate


@app.get("/snapshot/{event_id}/{kind}")
def snapshot(request: Request, event_id: str, kind: str):
    sessions.require(request)
    event = database.get_event(event_id)
    if not event or kind not in {"full", "crop"}:
        raise HTTPException(404)
    value = event[f"{kind}_snapshot_path"]
    if not value:
        raise HTTPException(404)
    return FileResponse(snapshot_file(value), media_type="image/jpeg")


@app.get("/access-list", response_class=HTMLResponse)
def access_list(request: Request, q: str = "", error: str = ""):
    session, redirect = page_session(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="access_list.html",
        context=template_context(
            request, session, entries=database.list_access_entries(q), q=q, error=error
        ),
    )


@app.post("/access-list")
def add_access(
    request: Request,
    plate: str = Form(),
    owner_label: str = Form(""),
    note: str = Form(""),
    csrf: str = Form(),
):
    session = sessions.require(request)
    sessions.require_csrf(session, csrf)
    normalized = normalize_plate(plate)
    if len(normalized) < settings.min_plate_length:
        return RedirectResponse("/access-list?error=Geçersiz+plaka", status_code=303)
    try:
        entry_id = database.add_access_entry(
            normalized, owner_label.strip(), note.strip(), session["user_id"]
        )
    except psycopg.errors.UniqueViolation:
        return RedirectResponse("/access-list?error=Plaka+zaten+mevcut", status_code=303)
    database.create_audit(
        session["user_id"], "access.create", "access_entry", str(entry_id), {"plate": normalized}, client_ip(request)
    )
    publish_access_change(normalized)
    return RedirectResponse("/access-list", status_code=303)


@app.post("/access-list/{entry_id}")
def update_access(
    request: Request,
    entry_id: int,
    plate: str = Form(),
    owner_label: str = Form(""),
    note: str = Form(""),
    is_active: str = Form("false"),
    csrf: str = Form(),
):
    session = sessions.require(request)
    sessions.require_csrf(session, csrf)
    normalized = normalize_plate(plate)
    active = is_active.lower() in {"1", "true", "on", "yes"}
    if len(normalized) < settings.min_plate_length:
        return RedirectResponse("/access-list?error=Geçersiz+plaka", status_code=303)
    try:
        updated = database.update_access_entry(
            entry_id, normalized, owner_label.strip(), note.strip(), active
        )
    except psycopg.errors.UniqueViolation:
        return RedirectResponse("/access-list?error=Plaka+zaten+mevcut", status_code=303)
    if not updated:
        raise HTTPException(404)
    database.create_audit(
        session["user_id"], "access.update", "access_entry", str(entry_id), {"plate": normalized, "active": active}, client_ip(request)
    )
    publish_access_change(normalized)
    return RedirectResponse("/access-list", status_code=303)


@app.post("/gates/{gate_id}/open")
def manual_open(request: Request, gate_id: str, reason: str = Form(), confirm: str = Form(), csrf: str = Form()):
    session = sessions.require(request)
    sessions.require_csrf(session, csrf)
    if confirm != "OPEN" or len(reason.strip()) < 3:
        raise HTTPException(422, "Confirmation and reason are required")
    gate_record = database.get_gate(gate_id)
    if not gate_record:
        raise HTTPException(404)
    event_id = str(uuid.uuid4())
    event = {
        "id": event_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        "source": "manual",
        "camera_id": None,
        "gate_id": gate_id,
        "plate": None,
        "confidence": None,
        "trigger_status": "pending",
        "trigger_duration_ms": None,
        "trigger_error": None,
        "full_snapshot_path": None,
        "crop_snapshot_path": None,
        "snapshot_status": "none",
        "requested_by": session["user_id"],
        "manual_reason": reason.strip(),
        "client_ip": client_ip(request),
    }
    database.upsert_event(event)
    result = gate_from_environment(gate_id).open(
        event_id=event_id, gate_id=gate_id, source="manual", reason=reason.strip()
    )
    event.update(trigger_status=result.status, trigger_duration_ms=result.duration_ms, trigger_error=result.error)
    database.upsert_event(event)
    database.create_audit(
        session["user_id"], "gate.manual_open", "gate", gate_id, {"event_id": event_id, "reason": reason.strip(), "status": result.status}, client_ip(request)
    )
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    session, redirect = page_session(request)
    if redirect:
        return redirect
    try:
        redis_online = bool(redis_client.ping())
    except redis.RedisError:
        redis_online = False
    archive = archive_status()
    return templates.TemplateResponse(
        request=request,
        name="system.html",
        context=template_context(
            request,
            session,
            workers=worker_statuses(),
            db_online=database.health(),
            redis_online=redis_online,
            archive_online=archive["online"],
            archive=archive,
            spool_bytes=spool_usage_bytes(),
            spool_limit=settings.spool_limit_bytes,
        ),
    )


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready(response: Response):
    try:
        redis_online = bool(redis_client.ping())
    except redis.RedisError:
        redis_online = False
    ready = database.health() and redis_online
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "not-ready"}


def main() -> None:
    configure_logging()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
    )


if __name__ == "__main__":
    main()
