"""Database connection session tagging, timeouts, and report-only transaction watchdog.

Part 4 (2026-11-28 Ledger Lock Fix):
- Applies `<role>:<engine>` `application_name` (`vcp-web`, `vcp-worker`, `vcp-script:<stem>`,
  overridable via `VCP_APP_NAME`) plus per-connection PostgreSQL lock / idle-in-transaction
  timeouts via guarded SQLAlchemy `"connect"` listeners (never mutating `connect_args` or
  pool defaults, and skipping SQLite / `MagicMock` test engines).
- Runs a daemon `threading.Thread` watchdog (never an `asyncio.Task`) every 5 s over in-memory
  transaction start records, logging `WARNING` at >= 10 s and `ERROR` + owning thread stack
  at >= 30 s (once per threshold per transaction, zero DB connections, zero SQL parameter values).
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
import traceback
from typing import Any, Mapping, Sequence
from unittest.mock import MagicMock

logger = logging.getLogger(__name__)

WATCHDOG_INTERVAL_SECONDS = 5.0
WATCHDOG_WARN_AGE_SECONDS = 10.0
WATCHDOG_ERROR_AGE_SECONDS = 30.0

_SAFE_APP_NAME_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_SQL_SINGLE_QUOTED_RE = re.compile(r"'(?:''|[^'])*'")
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_ident(raw: str, max_len: int = 48) -> str:
    cleaned = _SAFE_APP_NAME_RE.sub("_", str(raw or "").strip()).strip("_")
    return (cleaned or "vcp")[:max_len]


def resolve_db_component_role(
    *,
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve `<role>` (`vcp-web`, `vcp-worker`, `vcp-script:<stem>`, or `VCP_APP_NAME`)."""
    env = os.environ if environ is None else environ
    override = str(env.get("VCP_APP_NAME") or "").strip()
    if override:
        return _sanitize_ident(override, max_len=52)

    args = list(sys.argv if argv is None else argv)
    argv0 = str(args[0]) if args else ""
    stem = Path(argv0).stem.lower() if argv0 and argv0 != "-c" else ""
    joined = " ".join(str(a) for a in args).lower()

    cli_mode = ""
    for idx, tok in enumerate(args):
        if tok == "--mode" and idx + 1 < len(args):
            cli_mode = str(args[idx + 1]).strip().lower()
            break
        if str(tok).startswith("--mode="):
            cli_mode = str(tok).split("=", 1)[1].strip().lower()
            break

    eff_mode = (str(env.get("RUN_MODE") or env.get("APP_MODE") or "").strip().lower() or cli_mode)

    if eff_mode == "worker" or stem == "worker":
        return "vcp-worker"
    if (
        "uvicorn" in stem
        or "gunicorn" in stem
        or "app.webapp.server" in joined
        or (
            eff_mode in ("all", "web", "all_with_web", "web_and_bot", "bot_and_web", "bot")
            and (stem == "main" or bool(env.get("RUN_MODE") or env.get("APP_MODE") or cli_mode))
        )
    ):
        return "vcp-web"

    script_stem = _sanitize_ident(stem or "inline", max_len=32).lower()
    return f"vcp-script:{script_stem}"


def format_application_name(
    engine_kind: str,
    *,
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Build `<role>:<engine>` (capped at 63 chars for PostgreSQL `NAMEDATALEN`)."""
    role = resolve_db_component_role(argv=argv, environ=environ)
    suffix = _sanitize_ident(engine_kind, max_len=10).lower()
    if role.endswith(f":{suffix}"):
        return role[:63]
    max_role = max(8, 63 - len(suffix) - 1)
    return f"{role[:max_role]}:{suffix}"


def _is_real_sqlalchemy_target(target: Any) -> bool:
    if target is None or isinstance(target, MagicMock):
        return False
    dispatch = getattr(target, "dispatch", None)
    if dispatch is None or isinstance(dispatch, MagicMock):
        return False
    return True


def _redact_sql_summary(statement: object) -> str:
    """Return a parameter-free, literal-redacted SQL prefix (<= 96 chars) safe for logs."""
    raw = str(statement or "").strip()
    if not raw:
        return "<unknown>"
    no_literals = _SQL_SINGLE_QUOTED_RE.sub("'?'", raw)
    compact = _WHITESPACE_RE.sub(" ", no_literals)
    return compact[:96]


def install_postgres_session_listener(
    sync_engine: Any,
    *,
    engine_kind: str,
    lock_timeout_ms: int,
    idle_in_tx_timeout_ms: int,
    statement_timeout_ms: int | None = None,
) -> bool:
    """Register a PostgreSQL-only `"connect"` listener setting `application_name` and timeouts.

    Safely skips `None`, `MagicMock` engines, and non-PostgreSQL dialects (e.g. SQLite).
    """
    if not _is_real_sqlalchemy_target(sync_engine):
        return False
    dialect_name = str(getattr(getattr(sync_engine, "dialect", None), "name", "") or "").lower()
    if "postgres" not in dialect_name:
        return False
    if getattr(sync_engine, "_vcp_pg_session_listener_installed", False):
        return True

    def _on_pg_connect(dbapi_connection: Any, _connection_record: Any = None) -> None:
        app_name = format_application_name(engine_kind)
        stmts = [
            f"SET application_name = '{app_name}'",
            f"SET lock_timeout = '{int(lock_timeout_ms)}ms'",
        ]
        if statement_timeout_ms is not None:
            stmts.append(f"SET statement_timeout = '{int(statement_timeout_ms)}ms'")
        stmts.append(f"SET idle_in_transaction_session_timeout = '{int(idle_in_tx_timeout_ms)}ms'")
        try:
            cursor = dbapi_connection.cursor()
            try:
                for sql in stmts:
                    cursor.execute(sql)
            finally:
                cursor.close()
            commit_fn = getattr(dbapi_connection, "commit", None)
            if callable(commit_fn):
                commit_fn()
        except Exception as exc:
            logger.debug("db_diagnostics: connect SET skipped (%s): %s", engine_kind, type(exc).__name__)

    try:
        from sqlalchemy import event

        event.listen(sync_engine, "connect", _on_pg_connect)
        try:
            setattr(sync_engine, "_vcp_pg_session_listener_installed", True)
        except Exception:
            pass
        return True
    except Exception:
        return False


@dataclass
class _ActiveTxnRecord:
    conn_key: int
    dbapi_key: int | None
    engine_kind: str
    started_mono: float
    thread_ident: int
    thread_name: str
    stmt_summary: str = "<begin>"
    warned_10s: bool = False
    errored_30s: bool = False


class TransactionWatchdog:
    """Report-only daemon thread monitoring open SQLAlchemy transaction ages."""

    def __init__(
        self,
        *,
        interval_s: float = WATCHDOG_INTERVAL_SECONDS,
        warn_age_s: float = WATCHDOG_WARN_AGE_SECONDS,
        error_age_s: float = WATCHDOG_ERROR_AGE_SECONDS,
    ) -> None:
        self._interval_s = float(interval_s)
        self._warn_age_s = float(warn_age_s)
        self._error_age_s = float(error_age_s)
        self._lock = threading.Lock()
        self._active: dict[int, _ActiveTxnRecord] = {}
        self._thread: threading.Thread | None = None

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        t = threading.Thread(
            target=self._run_loop,
            name="vcp-db-tx-watchdog",
            daemon=True,
        )
        self._thread = t
        t.start()

    def _run_loop(self) -> None:
        while True:
            time.sleep(self._interval_s)
            try:
                self.inspect_once()
            except Exception:
                pass

    def on_begin(self, conn: Any, engine_kind: str) -> None:
        conn_key = id(conn)
        dbapi_conn = getattr(getattr(conn, "connection", None), "dbapi_connection", None)
        dbapi_key = id(dbapi_conn) if dbapi_conn is not None else None
        cur_thread = threading.current_thread()
        rec = _ActiveTxnRecord(
            conn_key=conn_key,
            dbapi_key=dbapi_key,
            engine_kind=engine_kind,
            started_mono=time.monotonic(),
            thread_ident=threading.get_ident(),
            thread_name=cur_thread.name,
        )
        with self._lock:
            self._active[conn_key] = rec
            self._ensure_thread_locked()

    def on_before_execute(self, conn: Any, statement: object) -> None:
        conn_key = id(conn)
        summary = _redact_sql_summary(statement)
        cur_thread = threading.current_thread()
        tid = threading.get_ident()
        with self._lock:
            rec = self._active.get(conn_key)
            if rec is not None:
                rec.stmt_summary = summary
                rec.thread_ident = tid
                rec.thread_name = cur_thread.name

    def on_end(self, conn: Any) -> None:
        conn_key = id(conn)
        with self._lock:
            self._active.pop(conn_key, None)

    def on_pool_checkin(self, dbapi_connection: Any) -> None:
        if dbapi_connection is None:
            return
        target_dbapi_key = id(dbapi_connection)
        with self._lock:
            stale_keys = [
                k for k, rec in self._active.items() if rec.dbapi_key == target_dbapi_key
            ]
            for k in stale_keys:
                self._active.pop(k, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def inspect_once(self, *, now_mono: float | None = None) -> list[dict[str, Any]]:
        """Inspect active transactions once and emit deduplicated WARNING (>=10s) / ERROR (>=30s)."""
        now = time.monotonic() if now_mono is None else float(now_mono)
        to_warn: list[tuple[_ActiveTxnRecord, float]] = []
        to_error: list[tuple[_ActiveTxnRecord, float]] = []

        with self._lock:
            for rec in self._active.values():
                age_s = max(0.0, now - rec.started_mono)
                if age_s >= self._warn_age_s and not rec.warned_10s:
                    rec.warned_10s = True
                    to_warn.append((rec, age_s))
                if age_s >= self._error_age_s and not rec.errored_30s:
                    rec.errored_30s = True
                    to_error.append((rec, age_s))

        events: list[dict[str, Any]] = []
        frames = sys._current_frames() if to_error else {}

        for rec, age_s in to_warn:
            app_label = format_application_name(rec.engine_kind)
            logger.warning(
                "db_tx_watchdog: transaction open %.1fs (>=%.0fs) app=%s thread=%s(%d) sql=%s",
                age_s,
                self._warn_age_s,
                app_label,
                rec.thread_name,
                rec.thread_ident,
                rec.stmt_summary,
            )
            events.append({"level": "WARNING", "age_s": age_s, "engine_kind": rec.engine_kind, "sql": rec.stmt_summary})

        for rec, age_s in to_error:
            app_label = format_application_name(rec.engine_kind)
            frame = frames.get(rec.thread_ident)
            stack_txt = "".join(traceback.format_stack(frame)).rstrip() if frame is not None else "<no python frame>"
            logger.error(
                "db_tx_watchdog: STUCK transaction open %.1fs (>=%.0fs) app=%s thread=%s(%d) sql=%s\n%s",
                age_s,
                self._error_age_s,
                app_label,
                rec.thread_name,
                rec.thread_ident,
                rec.stmt_summary,
                stack_txt,
            )
            events.append(
                {
                    "level": "ERROR",
                    "age_s": age_s,
                    "engine_kind": rec.engine_kind,
                    "sql": rec.stmt_summary,
                    "stack": stack_txt,
                }
            )
        return events


DEFAULT_TX_WATCHDOG = TransactionWatchdog()


def register_engine_watchdog(
    sync_engine: Any,
    *,
    engine_kind: str,
    watchdog: TransactionWatchdog | None = None,
) -> bool:
    """Attach `begin`/`before_cursor_execute`/`commit`/`rollback`/`checkin` listeners to `sync_engine`."""
    if not _is_real_sqlalchemy_target(sync_engine):
        return False
    if getattr(sync_engine, "_vcp_tx_watchdog_installed", False):
        return True
    wd = watchdog or DEFAULT_TX_WATCHDOG

    def _on_begin(conn: Any) -> None:
        wd.on_begin(conn, engine_kind)

    def _on_before_cursor_execute(
        conn: Any, _cursor: Any, statement: Any, _parameters: Any, _context: Any, _executemany: Any
    ) -> None:
        wd.on_before_execute(conn, statement)

    def _on_commit(conn: Any) -> None:
        wd.on_end(conn)

    def _on_rollback(conn: Any) -> None:
        wd.on_end(conn)

    def _on_checkin(dbapi_conn: Any, _conn_record: Any) -> None:
        wd.on_pool_checkin(dbapi_conn)

    try:
        from sqlalchemy import event

        event.listen(sync_engine, "begin", _on_begin)
        event.listen(sync_engine, "before_cursor_execute", _on_before_cursor_execute)
        event.listen(sync_engine, "commit", _on_commit)
        event.listen(sync_engine, "rollback", _on_rollback)
        pool = getattr(sync_engine, "pool", None)
        if _is_real_sqlalchemy_target(pool):
            event.listen(pool, "checkin", _on_checkin)
        try:
            setattr(sync_engine, "_vcp_tx_watchdog_installed", True)
        except Exception:
            pass
        return True
    except Exception:
        return False
