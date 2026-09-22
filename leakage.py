"""RouteProfit leakage math and report export."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from helpers import (
    _parse_dt,
    business_name,
    deadhead_cost_per_mile,
    format_money,
    gap_threshold_minutes,
    get_db,
    hourly_rate_dollars,
    utc_now_iso,
)

def compute_leakage() -> dict[str, Any]:
    """Deterministic leakage: deadhead $, schedule-gap $, billable-hour leak $."""
    db = get_db()
    rate = hourly_rate_dollars()
    cpm = deadhead_cost_per_mile()
    threshold = gap_threshold_minutes()
    notes: list[str] = []

    jobs = db.execute(
        "SELECT * FROM jobs ORDER BY tech, start_at"
    ).fetchall()
    miles = db.execute(
        "SELECT * FROM mileage WHERE kind = 'deadhead' OR kind = 'total'"
    ).fetchall()

    # Deadhead by tech/date
    dh_by_key: dict[tuple[str, str], float] = {}
    for m in miles:
        key = (m["tech"], m["date"])
        dh_by_key[key] = dh_by_key.get(key, 0.0) + float(m["miles"] or 0)

    # Group jobs by tech/day
    by_tech_day: dict[tuple[str, str], list] = {}
    for j in jobs:
        day = j["start_at"][:10]
        key = (j["tech"], day)
        by_tech_day.setdefault(key, []).append(j)

    by_tech: dict[str, dict[str, float]] = {}
    by_day: dict[str, dict[str, float]] = {}
    by_area: dict[str, dict[str, float]] = {}
    detail_rows: list[dict] = []

    def _acc(bucket: dict, key: str, deadhead: float, gap: float, hours: float) -> None:
        b = bucket.setdefault(key, {"deadhead": 0.0, "gap": 0.0, "hours": 0.0, "total": 0.0})
        b["deadhead"] += deadhead
        b["gap"] += gap
        b["hours"] += hours
        b["total"] += deadhead + gap + hours

    total_deadhead = 0.0
    total_gap = 0.0
    total_hours = 0.0
    has_invoiced_data = False

    for (tech, day), day_jobs in sorted(by_tech_day.items()):
        sorted_jobs = sorted(day_jobs, key=lambda r: r["start_at"])
        # Schedule gaps
        gap_hours = 0.0
        for a, b in zip(sorted_jobs, sorted_jobs[1:]):
            end = _parse_dt(a["end_at"])
            start = _parse_dt(b["start_at"])
            if not end or not start:
                continue
            gap_min = (start - end).total_seconds() / 60.0
            if gap_min >= threshold:
                gap_hours += gap_min / 60.0

        # Hour leakage
        hour_leak_h = 0.0
        for j in sorted_jobs:
            on_site = j["billable_hours"]
            if on_site is None:
                s = _parse_dt(j["start_at"])
                e = _parse_dt(j["end_at"])
                on_site = ((e - s).total_seconds() / 3600.0) if s and e else 0.0
            invoiced = j["invoiced_hours"]
            if invoiced is not None:
                has_invoiced_data = True
                hour_leak_h += max(0.0, float(on_site) - float(invoiced))
            elif j["nonbillable_hours"] is not None:
                has_invoiced_data = True
                hour_leak_h += max(0.0, float(j["nonbillable_hours"]))

        dh_miles = dh_by_key.get((tech, day), 0.0)
        deadhead_dollars = dh_miles * cpm
        gap_dollars = gap_hours * rate
        hours_dollars = hour_leak_h * rate

        total_deadhead += deadhead_dollars
        total_gap += gap_dollars
        total_hours += hours_dollars

        area = None
        for j in sorted_jobs:
            if j["area"]:
                area = j["area"]
                break
        area_key = area or "(no area)"

        _acc(by_tech, tech, deadhead_dollars, gap_dollars, hours_dollars)
        _acc(by_day, day, deadhead_dollars, gap_dollars, hours_dollars)
        _acc(by_area, area_key, deadhead_dollars, gap_dollars, hours_dollars)

        detail_rows.append(
            {
                "tech": tech,
                "date": day,
                "area": area_key,
                "deadhead_miles": round(dh_miles, 2),
                "deadhead_dollars": round(deadhead_dollars, 2),
                "gap_hours": round(gap_hours, 3),
                "gap_dollars": round(gap_dollars, 2),
                "hour_leak_hours": round(hour_leak_h, 3),
                "hour_leak_dollars": round(hours_dollars, 2),
                "total_dollars": round(deadhead_dollars + gap_dollars + hours_dollars, 2),
            }
        )

    # Orphan deadhead days with no jobs
    for (tech, day), dh_miles in dh_by_key.items():
        if (tech, day) in by_tech_day:
            continue
        deadhead_dollars = dh_miles * cpm
        total_deadhead += deadhead_dollars
        _acc(by_tech, tech, deadhead_dollars, 0.0, 0.0)
        _acc(by_day, day, deadhead_dollars, 0.0, 0.0)
        _acc(by_area, "(no area)", deadhead_dollars, 0.0, 0.0)
        detail_rows.append(
            {
                "tech": tech,
                "date": day,
                "area": "(no area)",
                "deadhead_miles": round(dh_miles, 2),
                "deadhead_dollars": round(deadhead_dollars, 2),
                "gap_hours": 0.0,
                "gap_dollars": 0.0,
                "hour_leak_hours": 0.0,
                "hour_leak_dollars": 0.0,
                "total_dollars": round(deadhead_dollars, 2),
            }
        )

    if not has_invoiced_data and jobs:
        notes.append("no invoiced_hours column")

    upload_notes = db.execute(
        "SELECT notes FROM uploads WHERE notes != '' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for u in upload_notes:
        for part in (u["notes"] or "").split("|"):
            part = part.strip()
            if part and part not in notes:
                notes.append(part)

    total = total_deadhead + total_gap + total_hours
    result = {
        "created_at": utc_now_iso(),
        "hourly_rate": rate,
        "deadhead_cost_per_mile": cpm,
        "gap_threshold_minutes": threshold,
        "totals": {
            "deadhead_dollars": round(total_deadhead, 2),
            "gap_dollars": round(total_gap, 2),
            "hour_leak_dollars": round(total_hours, 2),
            "total_dollars": round(total, 2),
        },
        "by_tech": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in sorted(by_tech.items())},
        "by_day": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in sorted(by_day.items())},
        "by_area": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in sorted(by_area.items())},
        "details": detail_rows,
        "notes": notes,
        "job_count": len(jobs),
        "mileage_count": len(miles),
    }
    return result

def save_report(result: dict[str, Any]) -> int:
    db = get_db()
    total_cents = int(round(result["totals"]["total_dollars"] * 100))
    cur = db.execute(
        "INSERT INTO reports(created_at, total_leak_cents, meta_json) VALUES(?,?,?)",
        (result["created_at"], total_cents, json.dumps(result)),
    )
    db.commit()
    return int(cur.lastrowid)

def latest_report() -> dict[str, Any] | None:
    db = get_db()
    row = db.execute(
        "SELECT meta_json FROM reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return json.loads(row["meta_json"])

def report_to_csv(result: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "tech",
            "date",
            "area",
            "deadhead_miles",
            "deadhead_dollars",
            "gap_hours",
            "gap_dollars",
            "hour_leak_hours",
            "hour_leak_dollars",
            "total_dollars",
        ]
    )
    for d in result.get("details") or []:
        w.writerow(
            [
                d["tech"],
                d["date"],
                d["area"],
                d["deadhead_miles"],
                d["deadhead_dollars"],
                d["gap_hours"],
                d["gap_dollars"],
                d["hour_leak_hours"],
                d["hour_leak_dollars"],
                d["total_dollars"],
            ]
        )
    return buf.getvalue()

def summary_blurb(result: dict[str, Any]) -> str:
    t = result["totals"]
    biz = business_name()
    lines = [
        f"{biz} leakage summary ({result.get('created_at', '')[:10]}):",
        f"Deadhead: {format_money(t['deadhead_dollars'])}",
        f"Schedule gaps (≥{result.get('gap_threshold_minutes', 30)} min): {format_money(t['gap_dollars'])}",
        f"Billable-hour leak: {format_money(t['hour_leak_dollars'])}",
        f"Total leak: {format_money(t['total_dollars'])}",
    ]
    by_tech = result.get("by_tech") or {}
    if by_tech:
        top = sorted(by_tech.items(), key=lambda kv: kv[1].get("total", 0), reverse=True)[:5]
        lines.append("Top techs:")
        for name, vals in top:
            lines.append(f"  {name}: {format_money(vals.get('total', 0))}")
    return "\n".join(lines)
