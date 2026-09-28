"""Self-contained error-telemetry ingest server for StepBot installations.

Single file on purpose: deployed standalone (Docker) on the product team's box,
away from the main repo. Only fastapi / uvicorn / httpx / pydantic + stdlib sqlite3.
"""

# NB: no `from __future__ import annotations` — FastAPI must resolve Depends() closures
# at runtime, and stringified annotations break that for factory-scoped dependencies.

import asyncio
import contextlib
import json
import os
import queue
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from html import escape
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

MAX_EVENTS_PER_REQUEST = 50
MAX_TRACEBACK_CHARS = 16_000
MAX_MESSAGE_CHARS = 1_000
MAX_CONTEXT_CHARS = 4_000
MAX_INSTALLS_PER_ISSUE = 50
MAX_VERSIONS_PER_ISSUE = 10
MAX_EVENT_ROWS_PER_ISSUE = 200
MAX_COUNT_PER_EVENT = 1_000_000  # fits SQLite INTEGER; caps merge-inflation abuse
MAX_ISSUE_ROWS = 20_000  # oldest resolved / least-recent issues evicted past this
RATE_LIMIT_PER_MINUTE = 120
MAX_RATE_KEYS = 10_000  # bound the in-memory rate map (evict stale IPs past this)
MAX_ALERT_QUEUE = 100  # drop Telegram alerts past this so a burst can't flood/grow


@dataclass(frozen=True)
class Config:
    db_path: str
    dash_user: str
    dash_pass: str
    ingest_token: str
    tg_bot_token: str
    tg_chat_id: str
    allow_anonymous: bool  # explicit opt-in to run /ingest WITHOUT a token (not recommended)


def _load_config() -> Config:
    return Config(
        db_path=os.environ.get("TS_DB_PATH", "./telemetry.db"),
        dash_user=os.environ.get("TS_DASH_USER", ""),
        dash_pass=os.environ.get("TS_DASH_PASS", ""),
        ingest_token=os.environ.get("TS_INGEST_TOKEN", ""),
        tg_bot_token=os.environ.get("TS_TG_BOT_TOKEN", ""),
        tg_chat_id=os.environ.get("TS_TG_CHAT_ID", ""),
        allow_anonymous=os.environ.get("TS_ALLOW_ANONYMOUS", "").strip().lower()
        in ("1", "true", "yes"),
    )


class TelemetryEvent(BaseModel):
    error_id: str = ""
    code: int = Field(default=0, ge=0, le=9999)  # стабильный номер класса ошибки (E-код)
    fingerprint: str = Field(min_length=1, max_length=256)
    source: str = ""
    exc_type: str = ""
    message: str = ""
    traceback: str = ""
    context: dict[str, str] = Field(default_factory=dict)
    count: int = Field(default=1, ge=1, le=MAX_COUNT_PER_EVENT)
    ts: str = ""


class IngestPayload(BaseModel):
    install_id: str = Field(min_length=1, max_length=128)
    version: str = Field(default="", max_length=64)
    # Raw dicts, not TelemetryEvent: one malformed event must not 422 the whole batch.
    events: list[Any] = Field(default_factory=list, max_length=MAX_EVENTS_PER_REQUEST)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# Timestamps are stored as UTC ISO strings; the dashboard shows them in Moscow time (UTC+3,
# no DST) formatted for humans — «16.07.2026 20:59» instead of a raw «…T17:59:00+00:00».
_MSK = timezone(timedelta(hours=3))


def _msk(iso: object, *, seconds: bool = False) -> str:
    s = str(iso or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    fmt = "%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M"
    return dt.astimezone(_MSK).strftime(fmt)


def _init_db(db_path: str) -> sqlite3.Connection:
    # autocommit (isolation_level=None) so ingest can drive explicit BEGIN + per-event
    # SAVEPOINT/ROLLBACK — the module's implicit transactions don't wrap SAVEPOINT.
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issues (
            fingerprint TEXT PRIMARY KEY,
            exc_type TEXT,
            message TEXT,
            source TEXT,
            first_seen TEXT,
            last_seen TEXT,
            total INTEGER,
            installs TEXT,
            versions TEXT,
            traceback TEXT,
            context TEXT,
            resolved INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT,
            error_id TEXT,
            install_id TEXT,
            version TEXT,
            ts TEXT,
            count INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_fp ON events(fingerprint, id)")
    # Миграция существующих баз: колонка E-кода появилась после первых деплоев.
    # Явная проверка вместо suppress(OperationalError): «database is locked» должен
    # уронить старт (и перезапуститься), а не оставить процесс без колонки —
    # иначе каждый ingest молча теряет события через per-event savepoint.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(issues)")}
    if "code" not in cols:
        conn.execute("ALTER TABLE issues ADD COLUMN code INTEGER DEFAULT 0")
    conn.commit()
    return conn


def _merge_capped(raw_json: str | None, value: str, cap: int) -> str:
    arr: list[str] = []
    if raw_json:
        with contextlib.suppress(ValueError):
            loaded = json.loads(raw_json)
            if isinstance(loaded, list):
                arr = [str(x) for x in loaded]
    if value and value not in arr:
        arr.append(value)
    return json.dumps(arr[-cap:])


async def _send_telegram(cfg: Config, text: str) -> None:
    url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/sendMessage"
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"chat_id": cfg.tg_chat_id, "text": text})


async def _drain_alerts(cfg: Config, alerts: queue.SimpleQueue[str]) -> None:
    # Ingest endpoints are sync (threadpool), so they only enqueue; this task sends.
    while True:
        await asyncio.sleep(2)
        while True:
            try:
                text = alerts.get_nowait()
            except queue.Empty:
                break
            await _send_telegram(cfg, text)


# --------------------------------------------------------------------------
# Dashboard rendering (server-side HTML, no external assets)
# --------------------------------------------------------------------------

_CSS = """
body{background:#111418;color:#d7dde4;font:14px/1.5 -apple-system,Segoe UI,sans-serif;
 margin:0;padding:24px}
h1{font-size:18px;margin:0 0 4px}
.meta{color:#8b96a3;margin-bottom:16px}
.meta b{color:#d7dde4}
table{border-collapse:collapse;width:100%}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid #232a33;vertical-align:top}
th{color:#8b96a3;font-weight:600;font-size:12px;text-transform:uppercase}
tr.detail td{border-bottom:1px solid #2e3844;background:#151a20}
.badge{background:#26303b;border-radius:4px;padding:1px 7px;font-size:12px;color:#9fb2c5}
.res{color:#5fbf77}
.open{color:#e0b453}
pre{background:#0b0e12;border:1px solid #232a33;border-radius:6px;padding:10px;
 overflow-x:auto;font:12px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap}
button{background:#26303b;color:#d7dde4;border:1px solid #3a4653;border-radius:5px;
 padding:3px 10px;cursor:pointer;font-size:12px}
button:hover{background:#33404f}
details summary{cursor:pointer;color:#8b96a3;font-size:12px}
.evt{color:#8b96a3;font-size:12px}
a{color:#6aa7e8}
"""


def _render_row(issue: sqlite3.Row, events: list[sqlite3.Row], colspan: int) -> str:
    fp = escape(str(issue["fingerprint"]))
    installs = json.loads(issue["installs"] or "[]")
    versions = json.loads(issue["versions"] or "[]")
    resolved = bool(issue["resolved"])
    status = '<span class="res">resolved</span>' if resolved else '<span class="open">open</span>'
    action = "reopen" if resolved else "resolve"
    msg = escape(str(issue["message"] or "")[:120])
    ctx = escape(json.dumps(json.loads(issue["context"] or "{}"), ensure_ascii=False, indent=1))
    evt_lines = "".join(
        f'<div class="evt">{escape(_msk(e["ts"], seconds=True))} · {escape(str(e["error_id"] or ""))}'
        f" · {escape(str(e['install_id'] or ''))} · v{escape(str(e['version'] or ''))}"
        f" · x{e['count']}</div>"
        for e in events
    )
    code = 0
    with contextlib.suppress(IndexError, KeyError, TypeError, ValueError):
        code = int(issue["code"] or 0)
    code_cell = f'<span class="badge">E{code}</span>' if code else "—"
    main = (
        f"<tr><td>{code_cell}</td>"
        f"<td><b>{escape(str(issue['exc_type'] or ''))}</b><br>{msg}</td>"
        f'<td><span class="badge">{escape(str(issue["source"] or ""))}</span></td>'
        f"<td>{issue['total']}</td><td>{len(installs)}</td>"
        f"<td>{escape(', '.join(str(v) for v in versions))}</td>"
        f"<td>{escape(_msk(issue['first_seen']))}<br>"
        f"{escape(_msk(issue['last_seen']))}</td>"
        # Относительный action: дашборд работает и на голом домене, и за префиксом
        # (nginx location /errors/ -> proxy_pass http://127.0.0.1:8088/).
        f'<td>{status}<form method="post" action="issues/{fp}/toggle">'
        f"<button>{action}</button></form></td></tr>"
    )
    detail = (
        f'<tr class="detail"><td colspan="{colspan}"><details>'
        f"<summary>traceback / context / events</summary>"
        f"<pre>{escape(str(issue['traceback'] or ''))}</pre><pre>{ctx}</pre>{evt_lines}"
        f"</details></td></tr>"
    )
    return main + detail


def _render_dashboard(
    issues: list[sqlite3.Row],
    events_by_fp: dict[str, list[sqlite3.Row]],
    open_count: int,
    resolved_count: int,
    install_count: int,
    show_all: bool,
) -> str:
    cols = ["code", "error", "source", "total", "installs", "versions", "first / last seen", "status"]
    head = "".join(f"<th>{c}</th>" for c in cols)
    rows = "".join(
        _render_row(i, events_by_fp.get(str(i["fingerprint"]), []), len(cols)) for i in issues
    )
    toggle = '<a href="./">hide resolved</a>' if show_all else '<a href="./?all=1">show resolved</a>'
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>StepBot telemetry</title><style>{_CSS}</style></head><body>"
        "<h1>StepBot telemetry</h1>"
        f'<div class="meta">open <b>{open_count}</b> · resolved <b>{resolved_count}</b>'
        f" · installs <b>{install_count}</b> · время МСК · {toggle}</div>"
        f"<table><tr>{head}</tr>{rows}</table></body></html>"
    )


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

_basic = HTTPBasic(auto_error=False)


def create_app() -> FastAPI:
    cfg = _load_config()
    conn = _init_db(cfg.db_path)
    conn.row_factory = sqlite3.Row
    db_lock = threading.Lock()
    alerts: queue.SimpleQueue[str] = queue.SimpleQueue()
    rate: dict[str, deque[float]] = {}
    rate_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.get_running_loop().create_task(_drain_alerts(cfg, alerts))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            conn.close()

    app = FastAPI(title="StepBot telemetry", lifespan=lifespan)

    def _alert(text: str) -> None:
        # Bounded, non-blocking: drop alerts past the cap so a new-fingerprint burst
        # can't grow the queue without limit or flood the channel.
        if cfg.tg_bot_token and cfg.tg_chat_id and alerts.qsize() < MAX_ALERT_QUEUE:
            alerts.put(text)

    def _client_ip(request: Request) -> str:
        # Use nginx-set X-Real-IP (see README) — it OVERWRITES any client value, so it can't be
        # spoofed. The X-Forwarded-For FIRST hop is attacker-controlled (nginx APPENDS the real IP
        # to whatever the client sent), so trusting it lets one attacker mint a fresh rate bucket
        # per request → rate-limit bypass + unbounded map growth. Use the LAST XFF hop as fallback.
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            return real[:64]
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()[:64]
        return request.client.host if request.client else "unknown"

    def _check_rate(ip: str) -> None:
        now = time.monotonic()
        with rate_lock:
            if len(rate) > MAX_RATE_KEYS:
                # Evict IPs whose window is fully stale so the map can't grow unbounded.
                for stale in [k for k, d in rate.items() if not d or now - d[-1] > 60]:
                    del rate[stale]
            hits = rate.setdefault(ip, deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= RATE_LIMIT_PER_MINUTE:
                raise HTTPException(status_code=429, detail="rate limit exceeded")
            hits.append(now)

    def _require_dash_auth(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
    ) -> None:
        if not cfg.dash_user or not cfg.dash_pass:
            raise HTTPException(status_code=503, detail="set TS_DASH_USER/TS_DASH_PASS")
        ok = (
            credentials is not None
            and secrets.compare_digest(credentials.username.encode(), cfg.dash_user.encode())
            and secrets.compare_digest(credentials.password.encode(), cfg.dash_pass.encode())
        )
        if not ok:
            raise HTTPException(
                status_code=401,
                detail="unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )

    def _evict_issues() -> None:
        """Bound total issue rows (disk-fill guard): drop resolved first, then oldest.
        Delete the evicted issues' event rows too, else the events table grows unbounded and
        install_count (COUNT(DISTINCT install_id) FROM events) counts orphans."""
        over = conn.execute("SELECT COUNT(*) AS c FROM issues").fetchone()["c"] - MAX_ISSUE_ROWS
        if over > 0:
            victims = [
                r["fingerprint"]
                for r in conn.execute(
                    "SELECT fingerprint FROM issues ORDER BY resolved DESC, last_seen ASC LIMIT ?",
                    (over,),
                ).fetchall()
            ]
            marks = ",".join("?" * len(victims))
            conn.execute(f"DELETE FROM events WHERE fingerprint IN ({marks})", victims)
            conn.execute(f"DELETE FROM issues WHERE fingerprint IN ({marks})", victims)

    def _apply_event(ev: TelemetryEvent, install_id: str, version: str) -> None:
        traceback = ev.traceback[:MAX_TRACEBACK_CHARS]
        message = ev.message[:MAX_MESSAGE_CHARS]
        ts = ev.ts or _now_iso()
        context = json.dumps(ev.context, ensure_ascii=False)[:MAX_CONTEXT_CHARS]
        row = conn.execute(
            "SELECT total, installs, versions, resolved, code FROM issues WHERE fingerprint = ?",
            (ev.fingerprint,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO issues (fingerprint, code, exc_type, message, source, first_seen,"
                " last_seen, total, installs, versions, traceback, context, resolved)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    ev.fingerprint,
                    ev.code,
                    ev.exc_type,
                    message,
                    ev.source,
                    ts,
                    ts,
                    ev.count,
                    json.dumps([install_id]),
                    json.dumps([version] if version else []),
                    traceback,
                    context,
                ),
            )
            _evict_issues()
            # Без "E0" в алертах: у события от старого клиента кода нет.
            label = f"E{ev.code} " if ev.code else ""
            _alert(f"🆕 {label}{ev.exc_type}: {message} ({ev.source}, {version})")
        else:
            conn.execute(
                # NULLIF: событие от старого клиента (code=0) не затирает уже известный код.
                "UPDATE issues SET total = ?, last_seen = ?,"
                " code = COALESCE(NULLIF(?, 0), code), message = ?, traceback = ?,"
                " context = ?, installs = ?, versions = ?, resolved = 0 WHERE fingerprint = ?",
                (
                    row["total"] + ev.count,
                    ts,
                    ev.code,
                    message,
                    traceback,
                    context,
                    _merge_capped(row["installs"], install_id, MAX_INSTALLS_PER_ISSUE),
                    _merge_capped(row["versions"], version, MAX_VERSIONS_PER_ISSUE),
                    ev.fingerprint,
                ),
            )
            if row["resolved"]:
                # Регрессия от legacy-клиента (code=0) алертит сохранённым кодом issue.
                code = ev.code or int(row["code"] or 0)
                label = f"E{code} " if code else ""
                _alert(f"♻️ регрессия: {label}{ev.exc_type}: {message} ({ev.source}, {version})")
        conn.execute(
            "INSERT INTO events (fingerprint, error_id, install_id, version, ts, count)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ev.fingerprint, ev.error_id, install_id, version, ts, ev.count),
        )
        conn.execute(
            "DELETE FROM events WHERE fingerprint = ? AND id NOT IN"
            " (SELECT id FROM events WHERE fingerprint = ? ORDER BY id DESC LIMIT ?)",
            (ev.fingerprint, ev.fingerprint, MAX_EVENT_ROWS_PER_ISSUE),
        )

    @app.post("/ingest")
    def ingest(payload: IngestPayload, request: Request) -> dict[str, Any]:
        _check_rate(_client_ip(request))
        # Fail CLOSED: without a configured token /ingest is a public write endpoint anyone can
        # flood/forge. Require the token unless the operator EXPLICITLY opted into anonymous mode.
        if not cfg.ingest_token:
            if not cfg.allow_anonymous:
                raise HTTPException(status_code=503, detail="telemetry ingest not configured")
        else:
            supplied = request.headers.get("X-Telemetry-Token", "")
            if not secrets.compare_digest(supplied.encode(), cfg.ingest_token.encode()):
                raise HTTPException(status_code=401, detail="bad telemetry token")
        accepted = 0
        with db_lock:
            conn.execute("BEGIN")
            try:
                for i, raw in enumerate(payload.events[:MAX_EVENTS_PER_REQUEST]):
                    # Per-event savepoint: ANY failure (incl. non-sqlite ones like
                    # OverflowError) rolls back only that event, so a good event is never
                    # left half-written on the shared connection and the batch keeps going.
                    conn.execute(f"SAVEPOINT ev{i}")
                    try:
                        ev = TelemetryEvent.model_validate(raw)
                        _apply_event(ev, payload.install_id, payload.version)
                    except Exception:
                        conn.execute(f"ROLLBACK TO ev{i}")
                        conn.execute(f"RELEASE ev{i}")
                    else:
                        conn.execute(f"RELEASE ev{i}")
                        accepted += 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"ok": True, "accepted": accepted}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, _auth: Annotated[None, Depends(_require_dash_auth)]) -> str:
        show_all = request.query_params.get("all") == "1"
        where = "" if show_all else " WHERE resolved = 0"
        with db_lock:
            issues = conn.execute(f"SELECT * FROM issues{where} ORDER BY last_seen DESC").fetchall()
            open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM issues WHERE resolved = 0"
            ).fetchone()["c"]
            resolved_count = conn.execute(
                "SELECT COUNT(*) AS c FROM issues WHERE resolved = 1"
            ).fetchone()["c"]
            install_count = conn.execute(
                "SELECT COUNT(DISTINCT install_id) AS c FROM events"
            ).fetchone()["c"]
            events_by_fp = {
                str(i["fingerprint"]): conn.execute(
                    "SELECT * FROM events WHERE fingerprint = ? ORDER BY id DESC LIMIT 20",
                    (i["fingerprint"],),
                ).fetchall()
                for i in issues
            }
        return _render_dashboard(
            issues, events_by_fp, open_count, resolved_count, install_count, show_all
        )

    @app.post("/issues/{fingerprint}/toggle")
    def toggle_issue(
        fingerprint: str, _auth: Annotated[None, Depends(_require_dash_auth)]
    ) -> RedirectResponse:
        with db_lock:
            cur = conn.execute(
                "UPDATE issues SET resolved = 1 - resolved WHERE fingerprint = ?",
                (fingerprint,),
            )
            conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="unknown fingerprint")
        # Относительный Location: /issues/x/toggle -> ../../ == корень дашборда,
        # что верно и на голом домене, и за префиксом /errors/.
        return RedirectResponse(url="../../", status_code=303)

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    return app


app = create_app()
