"""RouteProfit local verifier-style tests (PRD §10)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="routeprofit-test-")
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "test.db")
os.environ["UPLOAD_DIR"] = str(Path(_TMP) / "uploads")
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["HOURLY_RATE_DOLLARS"] = "125"
os.environ["DEADHEAD_COST_PER_MILE"] = "0.80"
os.environ["GAP_THRESHOLD_MINUTES"] = "30"
os.environ["MARKETING_URL"] = ""
os.environ["SECRET_KEY"] = "test-secret"
for k in (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "OWNER_EMAIL",
    "FROM_EMAIL",
    "FROM_NAME",
):
    os.environ.pop(k, None)

import app as app_module  # noqa: E402
import helpers as H  # noqa: E402

ROOT = Path(__file__).resolve().parent
SAMPLE_JOBS = ROOT / "sample-jobs.csv"
SAMPLE_MILES = ROOT / "sample-mileage.csv"


class RouteProfitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Path(os.environ["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
        app_module.init_db()
        cls.app = app_module.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        db_path = os.environ["DATABASE_PATH"]
        for suffix in ("", "-wal", "-shm"):
            p = Path(db_path + suffix)
            if p.exists():
                p.unlink()
        with self.app.app_context():
            H.init_schema(H.get_db())

    def _login(self):
        return self.client.post(
            "/login",
            data={"password": "testpass", "next": "/"},
            follow_redirects=False,
        )

    def test_01_health_public_ok_smtp_false(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)

    def test_02_auth_gates_dashboard_health_public(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))
        r2 = self.client.get("/health")
        self.assertEqual(r2.status_code, 200)

    def test_03_upload_samples_and_report_nonzero(self):
        self._login()
        with SAMPLE_JOBS.open("rb") as fh:
            r = self.client.post(
                "/upload/jobs",
                data={"file": (fh, "sample-jobs.csv")},
                content_type="multipart/form-data",
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 302)

        with SAMPLE_MILES.open("rb") as fh:
            r = self.client.post(
                "/upload/mileage",
                data={"file": (fh, "sample-mileage.csv")},
                content_type="multipart/form-data",
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 302)

        r = self.client.post("/report/run", follow_redirects=False)
        self.assertEqual(r.status_code, 302)

        with self.app.app_context():
            report = H.latest_report()
        self.assertIsNotNone(report)
        totals = report["totals"]
        nonzero = sum(
            1
            for k in ("deadhead_dollars", "gap_dollars", "hour_leak_dollars")
            if totals.get(k, 0) > 0
        )
        self.assertGreaterEqual(
            nonzero,
            2,
            f"Need ≥2 non-zero leak categories, got {totals}",
        )
        self.assertGreater(totals["total_dollars"], 0)
        self.assertTrue(report["by_tech"])
        self.assertTrue(report["by_day"])

        dash = self.client.get("/")
        self.assertEqual(dash.status_code, 200)
        html = dash.get_data(as_text=True)
        self.assertIn("Deadhead", html)
        self.assertIn("Schedule-gap", html)
        self.assertIn("Billable-hour", html)

    def test_04_csv_export_nonempty(self):
        self._login()
        with SAMPLE_JOBS.open("rb") as fh:
            self.client.post(
                "/upload/jobs",
                data={"file": (fh, "sample-jobs.csv")},
                content_type="multipart/form-data",
            )
        with SAMPLE_MILES.open("rb") as fh:
            self.client.post(
                "/upload/mileage",
                data={"file": (fh, "sample-mileage.csv")},
                content_type="multipart/form-data",
            )
        self.client.post("/report/run")
        r = self.client.get("/report.csv")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("tech,date,area", body)
        self.assertGreater(len(body.strip().splitlines()), 1)

    def test_05_works_without_smtp(self):
        self.assertFalse(H.smtp_configured())
        r = self.client.get("/health")
        self.assertIs(r.get_json()["smtp_configured"], False)
        self._login()
        # Email endpoint should not crash; flashes error when no SMTP
        with SAMPLE_JOBS.open("rb") as fh:
            self.client.post(
                "/upload/jobs",
                data={"file": (fh, "sample-jobs.csv")},
                content_type="multipart/form-data",
            )
        with SAMPLE_MILES.open("rb") as fh:
            self.client.post(
                "/upload/mileage",
                data={"file": (fh, "sample-mileage.csv")},
                content_type="multipart/form-data",
            )
        self.client.post("/report/run")
        r = self.client.post("/report/email", follow_redirects=True)
        self.assertEqual(r.status_code, 200)

    def test_06_settings_override(self):
        self._login()
        r = self.client.post(
            "/settings",
            data={"hourly_rate": "100", "deadhead_cpm": "1.00", "gap_threshold": "45"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            self.assertEqual(H.hourly_rate_dollars(), 100.0)
            self.assertEqual(H.deadhead_cost_per_mile(), 1.0)
            self.assertEqual(H.gap_threshold_minutes(), 45)


if __name__ == "__main__":
    unittest.main()
