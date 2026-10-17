# Supply Chain Copilot: Inventory & Procurement Agents

A multi-agent AI assistant for logistics teams, built with **Google ADK** and **Gemini**. It comes with a web
dashboard and a chat panel. Ask questions in plain English about stock, suppliers, reorders and demand. The
agents answer from a real database through tools, so they do not invent numbers.

**Live demo:** https://supplychain.velarisintelligence.in/

## Run it with Docker (recommended)

1. Get a free Gemini API key: https://aistudio.google.com/apikey
2. Create the config file:
   - Windows: `copy .env.example .env`
   - macOS/Linux: `cp .env.example .env`

   Then set `GOOGLE_API_KEY=...` in `.env`.
3. Start the app:
   ```bash
   docker compose up --build -d
   ```
4. Open **http://localhost:8095**

This starts PostgreSQL and the app. The first start creates the database and loads the mock data automatically.
The UI runs on port 8095 because 8080 is often taken. To change it, set `APP_PORT` in `.env`.

Everyday commands:

```bash
docker compose ps                 # status (app should be "healthy")
docker compose logs -f app        # logs
docker compose up -d              # apply .env changes (recreates the app)
docker compose down               # stop (add -v to also wipe the database)
```

## The web UI

The UI is a dark operations console at **http://localhost:8095**:

| Area | What it does |
|---|---|
| **Agent command bar** (top) | Ask anything in plain English. Press `/` to focus it. The pipeline under it highlights which agents ran, and replies appear in the agent console with the agents used and the response time. |
| KPI tiles | Inventory value, units shipped in the last 4 weeks (with change and a 12-week sparkline), low-stock positions, dead-stock value, and POs awaiting approval |
| Network flow | Line chart of units shipped vs received per week, last 12 weeks |
| Stock health | Stacked bars per warehouse showing stockout, below reorder point, healthy, and excess positions |
| Inventory value by category, Fastest movers | Bar charts with values at the bar ends. Every chart has a **Table** toggle and hover tooltips. |
| Warehouse capacity | Utilisation meters that change color at 70% and 85% |
| Low stock / Purchase orders / Dead stock | Action tables. **Reorder** and **Analyze** send a query to the agents. **Approve** submits a draft PO, so a human makes the final call. |

## Architecture

```
          Web UI / REST (/chat)  ·  ADK dev UI (adk web)  ·  CLI (adk run)
                                   |
                  supply_chain_orchestrator  (gemini-flash-latest)
            routes the request, chains specialists, asks for PO approval
              |                     |                        |
     inventory_agent       procurement_agent          analytics_agent     (gemini-flash-lite-latest)
     search_skus           get_suppliers_for_sku      movement_history
     get_stock             recommend_reorder          demand_trend
     list_low_stock        create_purchase_order      stock_turnover
     list_warehouses       submit_purchase_order      dead_stock
                           list_purchase_orders
              \                     |                        /
                    SQLAlchemy -> PostgreSQL (Docker) or SQLite (local)
```

- The specialists are attached to the orchestrator as **AgentTools**, so the orchestrator can call several of them for one request and combine their answers.
- **Human in the loop:** agents can only create *draft* purchase orders. A draft is submitted when the user explicitly approves it, either in chat or with the Approve button.
- **Reorder math:** 90-day daily demand gives `safety stock = 1.65 * sigma * sqrt(lead time)` (95% service level), `reorder point = demand * lead time + safety stock` and `EOQ = sqrt(2DS/H)`. The cheapest supplier is chosen, unless stock would run out before it can deliver; then the fastest supplier is chosen.
- **Mock data:** 3 warehouses, 50 SKUs in 5 categories, 5 suppliers and a 6-month simulation of demand and replenishment. The data includes stockouts, rising and falling demand, and dead stock.

## Free-tier (demo plan) setup

The defaults are tuned for a free Google AI Studio key:

- **Models:** `gemini-flash-latest` (orchestrator) and `gemini-flash-lite-latest` (specialists). These are Google's auto-updating aliases. The `gemini-2.5-*` models were retired for new keys on 2026-10-16. Using two models also splits the work across two separate per-model quotas, and Flash-Lite has the highest free limits.
- **Rate limits:** one question costs about 4–8 model calls (orchestrator, then specialist, then tools, then answer). Every call retries `429`/`5xx` errors with backoff (5s, 10s, 20s, 40s; `LLM_RETRY_ATTEMPTS`), so a busy minute slows the reply down instead of failing it. Expect 10–60 s per answer on the free tier.
- **Daily quota:** if the daily quota is used up, the UI shows a clear "quota reached" message. Wait for the daily reset or use another key.
- **Dashboard:** the dashboard, Approve button and REST endpoints use **no** model calls. They keep working even without a key.

## Run locally without Docker

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8095      # web UI on http://localhost:8095 (uses a local SQLite file)
adk web                           # optional: ADK developer UI on http://localhost:8000
```

## REST API

Interactive API docs are available at http://localhost:8095/docs.

| Method | Path | Purpose |
|---|---|---|
| POST | `/chat` | `{"message": "...", "session_id": "optional"}` returns `reply`, `session_id`, `agents_called` |
| GET | `/api/status` | Whether Gemini is configured, and which models are in use |
| GET | `/api/warehouses` | Capacity, utilisation, value |
| GET | `/api/low-stock?warehouse=Chicago` | Positions at or below their reorder point |
| GET | `/api/dead-stock?days=60` | Stock with no recent sales |
| GET | `/api/purchase-orders?status=draft` | Purchase orders |
| POST | `/api/purchase-orders/{id}/submit` | Approve and submit a draft PO |
| GET | `/health` | Liveness check |

## Example prompts

- "Which items are stocked out right now?"
- "Recommend a reorder for Poly Mailer at Rotterdam and draft the PO."
- "Approve PO 2."
- "Is demand for Packing Tape rising or falling?"
- "Show dead stock and how much cash is tied up in it."
- "What are our fastest and slowest movers this quarter?"

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `GOOGLE_API_KEY` | required for chat | Free key from AI Studio. For Vertex AI instead, set `GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`. |
| `ORCHESTRATOR_MODEL` | `gemini-flash-latest` | Model for the orchestrator |
| `SUBAGENT_MODEL` | `gemini-flash-lite-latest` | Model for the three specialists |
| `LLM_RETRY_ATTEMPTS` | `5` | Retries per model call on 429/5xx |
| `APP_PORT` | `8095` | Host port for the UI and API in Docker |
| `DATABASE_URL` | SQLite file | Local runs only. Docker Compose always uses its own Postgres. |

To rebuild the mock data: `docker compose down -v && docker compose up -d`. For a local run: `python -m supply_chain_agent.seed --reset`.

## Deploy to Google Cloud Run

```bash
gcloud run deploy supply-chain-copilot --source . --region us-central1 --allow-unauthenticated \
  --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=FALSE,GOOGLE_API_KEY=YOUR_KEY
```

Without `DATABASE_URL`, Cloud Run uses SQLite, and the data resets whenever the container restarts. Point
`DATABASE_URL` at a Cloud SQL Postgres database to keep data.

## Project structure

```
supply_chain_agent/
  agent.py    # orchestrator + 3 specialist agents, free-tier model/retry config
  tools.py    # 13 database-backed function tools
  db.py       # schema (SQLAlchemy Core), Postgres/SQLite engine
  seed.py     # deterministic mock-data generator
static/index.html   # web UI (dashboard + chat)
main.py             # FastAPI: UI, /chat, REST endpoints
Dockerfile, docker-compose.yml, requirements.txt, .env.example
```
