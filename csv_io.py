"""RouteProfit CSV parse and upload persistence."""
from __future__ import annotations

import csv
import io
from typing import Any

from helpers import (
    JOB_ALIASES,
    MILEAGE_ALIASES,
    _float_or_none,
    _map_columns,
    _parse_date,
    _parse_dt,
    _truthy,
    get_db,
    utc_now_iso,
)

def parse_jobs_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_columns(reader.fieldnames, JOB_ALIASES)
    if "tech" not in mapping or "start_at" not in mapping or "end_at" not in mapping:
        raise ValueError(
            "Jobs CSV needs tech/technician/employee, start_at/started, and end_at/ended/completed_at columns."
        )
    has_invoiced = "invoiced_hours" in mapping
    has_nonbillable = "nonbillable_hours" in mapping
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(reader, start=2):
        tech = (row.get(mapping["tech"]) or "").strip()
        start_raw = (row.get(mapping["start_at"]) or "").strip()
        end_raw = (row.get(mapping["end_at"]) or "").strip()
        if not tech or not start_raw or not end_raw:
            errors.append(f"Row {i}: missing tech/start/end")
            continue
        start_dt = _parse_dt(start_raw)
        end_dt = _parse_dt(end_raw)
        if not start_dt or not end_dt:
            errors.append(f"Row {i}: bad datetime")
            continue
        if end_dt <= start_dt:
            errors.append(f"Row {i}: end_at must be after start_at")
            continue
        derived_hours = (end_dt - start_dt).total_seconds() / 3600.0
        billable = _float_or_none(row.get(mapping["billable_hours"])) if "billable_hours" in mapping else None
        if billable is None:
            billable = derived_hours
        invoiced = None
        nonbillable = None
        if has_invoiced:
            invoiced = _float_or_none(row.get(mapping["invoiced_hours"]))
        if has_nonbillable:
            nonbillable = _float_or_none(row.get(mapping["nonbillable_hours"]))
        if invoiced is None and nonbillable is not None:
            invoiced = max(0.0, billable - nonbillable)
        elif invoiced is None and not has_invoiced and not has_nonbillable:
            invoiced = billable  # no leak columns → assume fully invoiced
        job_ref = (row.get(mapping["job_ref"]) or "").strip() if "job_ref" in mapping else ""
        customer = (row.get(mapping["customer"]) or "").strip() if "customer" in mapping else ""
        area = (row.get(mapping["area"]) or "").strip() if "area" in mapping else ""
        rows.append(
            {
                "tech": tech,
                "job_ref": job_ref or None,
                "customer": customer or None,
                "area": area or None,
                "start_at": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "end_at": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "billable_hours": billable,
                "invoiced_hours": invoiced,
                "nonbillable_hours": nonbillable,
            }
        )
    notes = []
    if not has_invoiced and not has_nonbillable:
        notes.append("no invoiced_hours column")
    return rows, notes + errors

def parse_mileage_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_columns(reader.fieldnames, MILEAGE_ALIASES)
    if "tech" not in mapping or "date" not in mapping:
        raise ValueError("Mileage CSV needs tech and date columns.")
    has_split = "deadhead_miles" in mapping or "job_miles" in mapping
    has_kind = "kind" in mapping
    has_flag = "is_deadhead" in mapping
    has_miles = "miles" in mapping
    if not has_miles and not has_split:
        raise ValueError("Mileage CSV needs miles/distance or deadhead_miles columns.")
    totals_only = has_miles and not has_kind and not has_flag and not has_split
    if totals_only:
        notes.append("mileage file had totals only")
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(reader, start=2):
        tech = (row.get(mapping["tech"]) or "").strip()
        date_s = _parse_date(row.get(mapping["date"]) or "")
        if not tech or not date_s:
            errors.append(f"Row {i}: missing tech/date")
            continue
        if has_split and "deadhead_miles" in mapping:
            dh = _float_or_none(row.get(mapping["deadhead_miles"])) or 0.0
            if dh > 0:
                rows.append({"tech": tech, "date": date_s, "miles": dh, "kind": "deadhead"})
            if "job_miles" in mapping:
                jm = _float_or_none(row.get(mapping["job_miles"])) or 0.0
                if jm > 0:
                    rows.append({"tech": tech, "date": date_s, "miles": jm, "kind": "job"})
            continue
        miles = _float_or_none(row.get(mapping["miles"])) if has_miles else None
        if miles is None:
            errors.append(f"Row {i}: bad miles")
            continue
        kind = "deadhead"
        if has_kind:
            k = (row.get(mapping["kind"]) or "").strip().lower()
            if k in ("job", "jobs", "billable"):
                kind = "job"
            elif k in ("total", "totals"):
                kind = "deadhead"
                notes.append("row kind=total treated as deadhead")
            else:
                kind = "deadhead"
        elif has_flag:
            kind = "deadhead" if _truthy(row.get(mapping["is_deadhead"])) else "job"
        elif totals_only:
            kind = "deadhead"
        rows.append({"tech": tech, "date": date_s, "miles": miles, "kind": kind})
    # dedupe notes
    uniq_notes = list(dict.fromkeys(notes))
    return rows, uniq_notes + errors

def save_jobs_upload(filename: str, rows: list[dict], notes: str) -> int:
    db = get_db()
    cur = db.execute(
        "INSERT INTO uploads(kind, filename, at, row_count, notes) VALUES(?,?,?,?,?)",
        ("jobs", filename, utc_now_iso(), len(rows), notes),
    )
    upload_id = cur.lastrowid
    # Replace prior jobs so recompute uses latest upload set
    db.execute("DELETE FROM jobs")
    for r in rows:
        db.execute(
            """
            INSERT INTO jobs(upload_id, tech, job_ref, customer, area, start_at, end_at,
                             billable_hours, invoiced_hours, nonbillable_hours)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                upload_id,
                r["tech"],
                r.get("job_ref"),
                r.get("customer"),
                r.get("area"),
                r["start_at"],
                r["end_at"],
                r.get("billable_hours"),
                r.get("invoiced_hours"),
                r.get("nonbillable_hours"),
            ),
        )
    db.commit()
    return int(upload_id)

def save_mileage_upload(filename: str, rows: list[dict], notes: str) -> int:
    db = get_db()
    cur = db.execute(
        "INSERT INTO uploads(kind, filename, at, row_count, notes) VALUES(?,?,?,?,?)",
        ("mileage", filename, utc_now_iso(), len(rows), notes),
    )
    upload_id = cur.lastrowid
    db.execute("DELETE FROM mileage")
    for r in rows:
        db.execute(
            "INSERT INTO mileage(upload_id, tech, date, miles, kind) VALUES(?,?,?,?,?)",
            (upload_id, r["tech"], r["date"], r["miles"], r["kind"]),
        )
    db.commit()
    return int(upload_id)
