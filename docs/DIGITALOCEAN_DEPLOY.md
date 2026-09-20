# DigitalOcean App Platform — PortGuard Demo API

Beginner-friendly steps to deploy the **read-only PortGuard Demo API** on DigitalOcean App
Platform (~$5/month). This is **additive**: it does not replace or migrate the existing
[Vercel](https://pixelsplease.vercel.app) frontend or [Render](https://portguard-api.onrender.com)
backend.

The DigitalOcean service serves **seeded demo SQLite data** built at deploy time by
`src/seed_demo_db.py`. It does **not** run local Nemotron, Ollama, GDELT ingestion, or
`run_pipeline.py --loop`.

---

## 1. Prerequisites

- A GitHub account with access to this repository (`KPandya1903/Pixels`, or your fork).
- A DigitalOcean account ([sign up](https://cloud.digitalocean.com/registrations/new)).
- The MLH / DigitalOcean student credits applied if you have them.
- Your Vercel frontend URL (example from the handoff: `https://pixelsplease.vercel.app`).
- **You do not need** a `DIGITALOCEAN_ACCESS_TOKEN`, `doctl`, a paid database, Redis, workers,
  GPUs, or Gradient AI for this demo.

---

## 2. What gets deployed

| Field | Value |
|---|---|
| Spec file (optional) | `.do/app.yaml` |
| App name (suggested) | `portguard-demo` |
| Service name | `portguard-api` |
| Service type | **Web Service** |
| Repository | `KPandya1903/Pixels` (or your fork) |
| Branch | `main` |
| Source directory | `/` (repository root — leave blank / default) |
| Environment | Python |
| Build command | `pip install -r requirements-demo.txt && python src/seed_demo_db.py` |
| Run command | `uvicorn api:app --app-dir src --host 0.0.0.0 --port $PORT` |
| HTTP port | `8080` |
| Health check path | `/health` |
| Instance size | **`apps-s-1vcpu-0.5gb`** (1 shared vCPU, 512 MiB, **~$5/month**) |
| Instance count | `1` |
| Database / workers / jobs | **None** |

---

## 3. Deploy with the DigitalOcean website

### Option A — Import from the app spec (recommended)

1. Open [DigitalOcean Apps](https://cloud.digitalocean.com/apps) → **Create App**.
2. Choose **GitHub**, authorize DigitalOcean, select **`KPandya1903/Pixels`** (or your fork),
   branch **`main`**.
3. If the UI offers to detect resources from `.do/app.yaml`, accept that. Otherwise continue
   with Option B values below.
4. Confirm there is **exactly one** web service named `portguard-api`, no databases, no
   workers, no jobs.
5. Open **Environment Variables** and set `CORS_ORIGINS` (see §4). Leave secrets empty — this
   demo API needs none.
6. Choose region (e.g. **NYC**) → **Create Resources** / **Create App**.
7. Wait for the build (pip install + `seed_demo_db.py`) and the first health check on `/health`.

### Option B — Manual resource settings (if the UI does not pick up `.do/app.yaml`)

Enter these exactly:

| UI field | Enter this |
|---|---|
| Resource type | Web Service |
| Name | `portguard-api` |
| Source directory | leave empty / `/` |
| Build command | `pip install -r requirements-demo.txt && python src/seed_demo_db.py` |
| Run command | `uvicorn api:app --app-dir src --host 0.0.0.0 --port $PORT` |
| HTTP port | `8080` |
| Health check | `/health` |
| Plan | Basic **$5** / `apps-s-1vcpu-0.5gb` (512 MB RAM) |

If the UI shows “Autodetect” for the run command, **override** it with the uvicorn line above.

---

## 4. Environment variables

Set these in **App → Settings → App-Level / portguard-api Environment Variables**.

| Key | Example value | Required? | Notes |
|---|---|---|---|
| `PORTGUARD_ENV` | `production` | Yes | Disables automatic localhost CORS. |
| `DEMO_MODE` | `true` | Yes | Advertised in `/api/meta` and `/health`. |
| `APP_VERSION` | `0.1.0-demo` | Optional | Shown in `/api/meta`. |
| `DB_BACKEND` | `sqlite` | Yes | Demo API is SQLite-only. |
| `CORS_ORIGINS` | `https://pixelsplease.vercel.app` | **Yes for browser use** | Comma-separated. **No** `*`. |
| `PYTHON_VERSION` | `3.12.7` | Optional | Build-time pin for the Python buildpack. |
| `PORTGUARD_DEMO_DB` | *(leave unset)* | No | Defaults to `data/demo_portguard.db`. |

**CORS for your Vercel URL**

```text
CORS_ORIGINS=https://pixelsplease.vercel.app
```

Multiple origins (comma-separated, no spaces required):

```text
CORS_ORIGINS=https://pixelsplease.vercel.app,https://your-preview.vercel.app
```

Do **not** put a DigitalOcean token, NVIDIA key, Gemini key, or database password on this app.
The demo endpoints work with **zero** model/API secrets.

---

## 5. After deploy — get your public URL

1. Open the app in DigitalOcean → **Settings** → **Domains**, or copy the default URL from the
   app overview (looks like `https://portguard-api-xxxxx.ondigitalocean.app`).
2. That URL is your **API base** (no trailing slash). Call paths like `/health` and `/api/meta`
   on it.

---

## 6. Test every endpoint

Replace `BASE` with your DigitalOcean URL.

```bash
BASE=https://YOUR-APP.ondigitalocean.app

curl -sS -o /tmp/pg-health.json -w "%{http_code}\n" "$BASE/health"
curl -sS -o /tmp/pg-meta.json   -w "%{http_code}\n" "$BASE/api/meta"
curl -sS -o /tmp/pg-events.json -w "%{http_code}\n" "$BASE/api/events"
curl -sS -o /tmp/pg-ships.json  -w "%{http_code}\n" "$BASE/api/shipments"
curl -sS -o /tmp/pg-decs.json   -w "%{http_code}\n" "$BASE/api/decisions"
curl -sS -o /tmp/pg-traf.json   -w "%{http_code}\n" "$BASE/api/traffic"
```

Expected: **HTTP 200** for all six. `/health` should show `"ok": true` and non-zero
`table_counts`. `/api/meta` should show `"demo_mode": true`.

**Browser:** open `$BASE/health` and `$BASE/api/meta` directly.

**CORS check** (allowed origin should include `Access-Control-Allow-Origin`):

```bash
curl -sS -D - -o /dev/null -X OPTIONS "$BASE/api/meta" \
  -H "Origin: https://pixelsplease.vercel.app" \
  -H "Access-Control-Request-Method: GET"

curl -sS -D - -o /dev/null -X OPTIONS "$BASE/api/meta" \
  -H "Origin: https://evil.example" \
  -H "Access-Control-Request-Method: GET"
```

Allowed → response headers include your Vercel origin. Disallowed → no matching
`Access-Control-Allow-Origin` (browser would block; curl still returns the OPTIONS status).

---

## 7. Point the Vercel frontend at DigitalOcean (optional)

The React app lives in `frontend/` and already prefers:

```text
VITE_PORTGUARD_API_BASE_URL
```

over the existing Render setting `VITE_API_BASE`.

In the **Vercel** project → **Settings → Environment Variables**:

| Key | Value |
|---|---|
| `VITE_PORTGUARD_API_BASE_URL` | `https://YOUR-APP.ondigitalocean.app` |

- No trailing slash.
- Redeploy the frontend after saving (Vite inlines this at **build** time).
- To keep using Render for the full dashboard, **leave this unset** and keep `VITE_API_BASE`
  pointed at `https://portguard-api.onrender.com`.

**Important:** The DigitalOcean demo API exposes only the six read-only routes above. The full
dashboard also needs `/api/kpis`, `/api/map`, `/api/ask`, etc., which remain on **Render**. For
the MLH “Best Use of DigitalOcean” demo, show DigitalOcean serving `/health` + `/api/*` with
real PortGuard schema data; keep the rich UI on Vercel→Render unless you only exercise the
overlapping routes (`/api/events`, `/api/shipments`, `/api/traffic`).

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Build fails on `seed_demo_db.py` | Source directory must be repo root (needs `docs/SCHEMAS.md` + `samples/`). |
| Health check failing | Confirm run command uses `--host 0.0.0.0 --port $PORT` and HTTP port `8080`. |
| `ModuleNotFoundError: api` | Use `--app-dir src` (or `PYTHONPATH=src`). |
| Empty JSON arrays | Re-run build so `python src/seed_demo_db.py` runs; check `/health` `table_counts`. |
| Browser CORS errors | Set `CORS_ORIGINS` to the exact Vercel origin (`https://…`, no path). Redeploy API. |
| Accidental `*` in CORS | The API strips `*`; set explicit origins instead. |
| Want to stop billing | **Destroy the App** (see §9). |

---

## 9. Delete the app after the demo (stop billing)

1. DigitalOcean → **Apps** → open `portguard-demo` (or whatever you named it).
2. **Settings** → **Destroy App** (wording may be “Destroy” / “Delete”).
3. Confirm. Billing for that web service stops; App Platform charges by the second with a short
   minimum, so destroy as soon as judging is done.
4. Optionally remove `VITE_PORTGUARD_API_BASE_URL` from Vercel and redeploy so the UI falls back
   to Render via `VITE_API_BASE`.

---

## 10. Local smoke test (before deploying)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-demo.txt
.venv/bin/python src/seed_demo_db.py
PORTGUARD_ENV=development CORS_ORIGINS=http://localhost:5173 \
  .venv/bin/uvicorn api:app --app-dir src --host 0.0.0.0 --port 8080
```

Then hit `http://127.0.0.1:8080/health` and the five `/api/*` routes.
