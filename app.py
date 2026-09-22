"""RouteProfit: deadhead / schedule-gap / billable-hour leakage from Jobs + Mileage CSVs."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

import helpers as H

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "routeprofit-self-hosted-change-me")

app.teardown_appcontext(H.close_db)


def init_db() -> None:
    with app.app_context():
        H.init_schema(H.get_db())


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": H._env("MARKETING_URL"),
        "smtp_configured": H.smtp_configured(),
        "business_name": H.business_name(),
        "owner_locked": bool(H.owner_password()),
        "logged_in": bool(session.get("owner")) or not H.owner_password(),
        "format_money": H.format_money,
        "owner_email_set": bool(H.owner_email()),
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in H.OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not H.owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "smtp_configured": H.smtp_configured()})


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not H.owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        provided = (request.form.get("password") or "").encode("utf-8")
        expected = H.owner_password().encode("utf-8")
        ok = len(provided) == len(expected) and secrets.compare_digest(provided, expected)
        if ok:
            session["owner"] = True
            return redirect(nxt)
        error = "Incorrect password."
    return render_template("login.html", next=nxt, error=error, public=True)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    report = H.latest_report()
    stats = H.upload_stats()
    blurb = H.summary_blurb(report) if report else ""
    return render_template(
        "index.html",
        report=report,
        stats=stats,
        blurb=blurb,
        rates={
            "hourly": H.hourly_rate_dollars(),
            "cpm": H.deadhead_cost_per_mile(),
            "gap": H.gap_threshold_minutes(),
        },
    )


@app.route("/upload/jobs", methods=["GET", "POST"])
def upload_jobs():
    if request.method == "GET":
        return render_template("upload_jobs.html")
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a Jobs CSV file.", "error")
        return redirect(url_for("upload_jobs"))
    raw = f.read().decode("utf-8-sig", errors="replace")
    try:
        rows, notes = H.parse_jobs_csv(raw)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("upload_jobs"))
    if not rows:
        flash("No valid job rows found. " + "; ".join(notes[:3]), "error")
        return redirect(url_for("upload_jobs"))
    name = secure_filename(f.filename) or "jobs.csv"
    dest = H.upload_dir() / name
    dest.write_text(raw, encoding="utf-8")
    note_str = " | ".join(notes) if notes else ""
    H.save_jobs_upload(name, rows, note_str)
    flash(f"Uploaded {len(rows)} jobs from {name}.", "ok")
    return redirect(url_for("index"))


@app.route("/upload/mileage", methods=["GET", "POST"])
def upload_mileage():
    if request.method == "GET":
        return render_template("upload_mileage.html")
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a Mileage CSV file.", "error")
        return redirect(url_for("upload_mileage"))
    raw = f.read().decode("utf-8-sig", errors="replace")
    try:
        rows, notes = H.parse_mileage_csv(raw)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("upload_mileage"))
    if not rows:
        flash("No valid mileage rows found. " + "; ".join(notes[:3]), "error")
        return redirect(url_for("upload_mileage"))
    name = secure_filename(f.filename) or "mileage.csv"
    dest = H.upload_dir() / name
    dest.write_text(raw, encoding="utf-8")
    note_str = " | ".join(notes) if notes else ""
    H.save_mileage_upload(name, rows, note_str)
    flash(f"Uploaded {len(rows)} mileage rows from {name}.", "ok")
    return redirect(url_for("index"))


@app.post("/report/run")
def report_run():
    stats = H.upload_stats()
    if stats["jobs_count"] == 0 and stats["mileage_count"] == 0:
        flash("Upload jobs and/or mileage CSVs first.", "error")
        return redirect(url_for("index"))
    result = H.compute_leakage()
    H.save_report(result)
    flash(
        f"Report ready — total leak {H.format_money(result['totals']['total_dollars'])}.",
        "ok",
    )
    return redirect(url_for("index"))


@app.get("/report.csv")
def report_csv():
    report = H.latest_report()
    if not report:
        flash("Run a report first.", "error")
        return redirect(url_for("index"))
    body = H.report_to_csv(report)
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=routeprofit-report.csv"},
    )


@app.post("/report/email")
def report_email():
    report = H.latest_report()
    if not report:
        flash("Run a report first.", "error")
        return redirect(url_for("index"))
    ok, msg = H.send_summary_email(report)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        hourly = (request.form.get("hourly_rate") or "").strip()
        cpm = (request.form.get("deadhead_cpm") or "").strip()
        gap = (request.form.get("gap_threshold") or "").strip()
        try:
            if hourly:
                H.set_setting("HOURLY_RATE_DOLLARS", str(float(hourly)))
            if cpm:
                H.set_setting("DEADHEAD_COST_PER_MILE", str(float(cpm)))
            if gap:
                H.set_setting("GAP_THRESHOLD_MINUTES", str(int(float(gap))))
            flash("Settings saved.", "ok")
        except ValueError:
            flash("Invalid number in settings.", "error")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        hourly=H.hourly_rate_dollars(),
        cpm=H.deadhead_cost_per_mile(),
        gap=H.gap_threshold_minutes(),
    )


# Ensure DB exists on import (gunicorn workers)
try:
    Path(H.database_path()).parent.mkdir(parents=True, exist_ok=True)
    H.upload_dir()
    with app.app_context():
        H.init_schema(H.get_db())
except Exception:
    pass


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT") or "8080")
    app.run(host="0.0.0.0", port=port, debug=True)
