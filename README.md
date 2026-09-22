# RouteProfit

Free, self-hosted deadhead and schedule-leak calculator for local service owners. Upload completed-job and mileage CSVs, set your rates, and see unpaid drive, idle gaps, and unbilled hours by tech and day.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Upload a **Jobs CSV** and a **Mileage CSV** (column aliases are case-insensitive)
- Set hourly rate, deadhead $/mile, and gap threshold (env or **Settings**)
- **Run report** → deadhead $, schedule-gap $, and billable-hour leak $ by tech / day / area
- **Copy summary** blurb and **Export CSV** of the leakage report
- Optional BYO SMTP: **Email weekly summary** button when `SMTP_HOST` + `OWNER_EMAIL` are set
- `GET /health` → HTTP 200 `{"status":"ok","smtp_configured":false}` even when SMTP unset

No GPS. No route optimizer. No maps API. No FSM sync. No telephony.

## Privacy

Self-hosted. You run the box; the owner is the data controller for job and mileage rows. No Stripe, no bundled SMS, no third-party analytics SaaS. Data lives in your SQLite file and CSV uploads on the Compose volume.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/routeprofit.git
cd routeprofit
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `SECRET_KEY`, and `OWNER_PASSWORD`. Rates default to `$125/hr`, `$0.80/mi`, and `30` minute gaps. Leave `SMTP_*` and `MARKETING_URL` empty unless configured. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `routeprofit-data` volume at `/data/routeprofit.db`; uploads under `/data/uploads`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
BUSINESS_NAME=Harbor HVAC
HOURLY_RATE_DOLLARS=125
DEADHEAD_COST_PER_MILE=0.80
GAP_THRESHOLD_MINUTES=30
MARKETING_URL=
SECRET_KEY=change-me
```

Leave all `SMTP_*` vars unset.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass`. Upload `sample-jobs.csv` then `sample-mileage.csv`. Hit **Run report**. Confirm summary cards show non-zero totals for at least two of: deadhead $, schedule-gap $, billable-hour leak $. Check **By tech** / **By day** tables. Use **Copy summary** and **Export /report.csv**.

3. Confirm empty `MARKETING_URL` shows no "Powered by" footer. Email button appears only when SMTP + `OWNER_EMAIL` are configured.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/routeprofit.db`. |
| `UPLOAD_DIR` | CSV uploads. Compose overrides to `/data/uploads`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | UI copy. |
| `HOURLY_RATE_DOLLARS` | Default hour-leak rate (UI Settings can override). |
| `DEADHEAD_COST_PER_MILE` | Default $/mi for deadhead. |
| `GAP_THRESHOLD_MINUTES` | Idle gap between consecutive same-tech/day jobs (default 30). |
| `OWNER_EMAIL` | Optional weekly summary destination. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional email. If `SMTP_HOST` is unset, email is unused. |
| `FROM_NAME`, `FROM_EMAIL` | SMTP From / sign-off. |
| `MARKETING_URL` | Optional footer. Leave empty for no footer. |

## CSV columns

**Jobs** (required): `tech` / `technician` / `employee`, `start_at` / `started`, `end_at` / `ended` / `completed_at`.

Optional: `job_ref` / `id`, `customer` / `customer_name`, `area` / `zip` / `city` / `zone`, `billable_hours`, `invoiced_hours`, `nonbillable_hours`.

**Mileage** (required): `tech`, `date`, and `miles` / `distance` (or split `deadhead_miles` / `job_miles`).

Optional: `kind` (`deadhead` | `job` | `total`), `is_deadhead`.

## Leakage math

- **Deadhead $** = deadhead miles × cost per mile
- **Schedule-gap $** = sum of gaps (end → next start, same tech/day) ≥ threshold, × hourly rate
- **Billable-hour leak $** = max(0, on-site hours − invoiced hours) × hourly rate
- **Total** = sum of the three

## What this is not

- Not a live route optimizer / VRP / "best stop order"
- Not GPS tracking, maps tiles, or geocoding
- Not a full FSM / Jobber API sync (CSV only)
- Not telephony / SMS dispatch (see LateBump)
- Not inventory / parts ETA / closeout invoicing (ParKit / PartPing / BillCatch)
- Not multi-tenant SaaS, Redis, Celery, or an LLM product

## License

Self-hosted. Use on your own VPS. No signup server, no license check.
