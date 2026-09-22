"""RouteProfit helpers: DB, CSV ingest, leakage math, optional SMTP."""
from __future__ import annotations

import csv
import io
import json
import os
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from flask import g

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = str(APP_ROOT / "data" / "routeprofit.db")

OPEN_ENDPOINTS = {"health", "login", "static"}

JOB_ALIASES = {
    "tech": ("tech", "technician", "employee"),
    "job_ref": ("job_ref", "id", "job_id", "job"),
    "customer": ("customer", "customer_name", "client"),
    "area": ("area", "zip", "city", "zone"),
    "start_at": ("start_at", "started", "start", "start_time"),
    "end_at": ("end_at", "ended", "completed_at", "end", "end_time"),
    "billable_hours": ("billable_hours", "billable", "hours"),
    "invoiced_hours": ("invoiced_hours", "invoiced", "billed_hours"),
    "nonbillable_hours": ("nonbillable_hours", "nonbillable", "non_billable_hours"),
}

MILEAGE_ALIASES = {
    "tech": ("tech", "technician", "employee"),
    "date": ("date", "day", "work_date"),
    "miles": ("miles", "distance", "total_miles"),
    "kind": ("kind", "type", "category"),
    "is_deadhead": ("is_deadhead", "deadhead", "deadhead_flag"),
    "job_miles": ("job_miles", "billable_miles"),
    "deadhead_miles": ("deadhead_miles", "dh_miles"),
}

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

def database_path() -> str:
    return _env("DATABASE_PATH") or DEFAULT_DB

def upload_dir() -> Path:
    raw = _env("UPLOAD_DIR") or str(APP_ROOT / "data" / "uploads")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path

def owner_password() -> str:
    return _env("OWNER_PASSWORD")

def business_name() -> str:
    return _env("BUSINESS_NAME") or "RouteProfit"

def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))

def owner_email() -> str:
    return _env("OWNER_EMAIL")

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = database_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db

def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            filename TEXT NOT NULL,
            at TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER NOT NULL,
            tech TEXT NOT NULL,
            job_ref TEXT,
            customer TEXT,
            area TEXT,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            billable_hours REAL,
            invoiced_hours REAL,
            nonbillable_hours REAL,
            FOREIGN KEY (upload_id) REFERENCES uploads(id)
        );
        CREATE TABLE IF NOT EXISTS mileage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER NOT NULL,
            tech TEXT NOT NULL,
            date TEXT NOT NULL,
            miles REAL NOT NULL,
            kind TEXT NOT NULL DEFAULT 'deadhead',
            FOREIGN KEY (upload_id) REFERENCES uploads(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            total_leak_cents INTEGER NOT NULL DEFAULT 0,
            meta_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    db.commit()

def get_setting(key: str, default: str = "") -> str:
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    return default

def set_setting(key: str, value: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()

def hourly_rate_dollars() -> float:
    raw = get_setting("HOURLY_RATE_DOLLARS") or _env("HOURLY_RATE_DOLLARS") or "125"
    try:
        return float(raw)
    except ValueError:
        return 125.0

def deadhead_cost_per_mile() -> float:
    raw = get_setting("DEADHEAD_COST_PER_MILE") or _env("DEADHEAD_COST_PER_MILE") or "0.80"
    try:
        return float(raw)
    except ValueError:
        return 0.80

def gap_threshold_minutes() -> int:
    raw = get_setting("GAP_THRESHOLD_MINUTES") or _env("GAP_THRESHOLD_MINUTES") or "30"
    try:
        return int(float(raw))
    except ValueError:
        return 30

def format_money(dollars: float) -> str:
    return f"${dollars:,.2f}"

def _norm_header(h: str) -> str:
    return (h or "").strip().lower().replace(" ", "_")

def _map_columns(fieldnames: list[str] | None, aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    if not fieldnames:
        return {}
    normed = {_norm_header(f): f for f in fieldnames}
    mapping: dict[str, str] = {}
    for canonical, names in aliases.items():
        for name in names:
            if name in normed:
                mapping[canonical] = normed[name]
                break
    return mapping

def _parse_dt(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    # Drop timezone suffix for strptime fallbacks
    bare = raw
    if "+" in bare[10:] or bare.endswith("Z"):
        bare = bare[:19] if "T" in bare else bare.split("+")[0].strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(bare, fmt)
        except ValueError:
            continue
    return None

def _parse_date(value: str) -> str | None:
    dt = _parse_dt(value)
    if dt:
        return dt.strftime("%Y-%m-%d")
    raw = (value or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return None

def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "y", "deadhead", "dh")

def _float_or_none(val: str | None) -> float | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(str(val).strip().replace(",", ""))
    except ValueError:
        return None


def parse_jobs_csv(text: str):
    from csv_io import parse_jobs_csv as _fn
    return _fn(text)

def parse_mileage_csv(text: str):
    from csv_io import parse_mileage_csv as _fn
    return _fn(text)

def save_jobs_upload(filename: str, rows: list, notes: str) -> int:
    from csv_io import save_jobs_upload as _fn
    return _fn(filename, rows, notes)

def save_mileage_upload(filename: str, rows: list, notes: str) -> int:
    from csv_io import save_mileage_upload as _fn
    return _fn(filename, rows, notes)

def send_summary_email(result: dict[str, Any]) -> tuple[bool, str]:
    if not smtp_configured():
        return False, "SMTP not configured"
    to_addr = owner_email()
    if not to_addr:
        return False, "OWNER_EMAIL not set"
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    use_tls = (_env("SMTP_TLS") or "true").lower() in ("1", "true", "yes")
    from_email = _env("FROM_EMAIL") or user or to_addr
    from_name = _env("FROM_NAME") or business_name()

    msg = EmailMessage()
    msg["Subject"] = f"{business_name()} weekly leakage summary"
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_addr
    from leakage import summary_blurb as _blurb
    msg.set_content(_blurb(result))

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True, "Email sent"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

def upload_stats() -> dict[str, Any]:
    db = get_db()
    jobs_n = db.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    miles_n = db.execute("SELECT COUNT(*) AS c FROM mileage").fetchone()["c"]
    last_jobs = db.execute(
        "SELECT filename, at, row_count, notes FROM uploads WHERE kind='jobs' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_miles = db.execute(
        "SELECT filename, at, row_count, notes FROM uploads WHERE kind='mileage' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "jobs_count": jobs_n,
        "mileage_count": miles_n,
        "last_jobs": dict(last_jobs) if last_jobs else None,
        "last_mileage": dict(last_miles) if last_miles else None,
    }

def compute_leakage():
    from leakage import compute_leakage as _fn
    return _fn()

def save_report(result):
    from leakage import save_report as _fn
    return _fn(result)

def latest_report():
    from leakage import latest_report as _fn
    return _fn()

def report_to_csv(result):
    from leakage import report_to_csv as _fn
    return _fn(result)

def summary_blurb(result):
    from leakage import summary_blurb as _fn
    return _fn(result)
