"""FastAPI service: web UI, chat endpoint and plain REST endpoints for the supply-chain agent.

Run:  uvicorn main:app --port 8095   ->  UI at http://localhost:8095, API docs at /docs
"""
import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()  # before importing the agent so DATABASE_URL / model settings apply

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from supply_chain_agent import tools  # noqa: E402
from supply_chain_agent.agent import ORCHESTRATOR_MODEL, SUBAGENT_MODEL, TRACE_SINK, root_agent  # noqa: E402
from supply_chain_agent.dashboard import dashboard_metrics, supplier_summary  # noqa: E402

logging.basicConfig(level=logging.INFO)
for noisy in ("google_adk", "google_genai", "httpx"):  # ADK logs full LLM requests/responses at INFO
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("supply_chain_api")

APP_NAME = "supply_chain_agent"
STATIC_DIR = Path(__file__).resolve().parent / "static"
session_service = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

app = FastAPI(title="Supply Chain Copilot", version="1.0.0",
              description="Multi-agent inventory & procurement copilot (Google ADK + Gemini).")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _key_present() -> bool:
    if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true"):
        return bool(os.getenv("GOOGLE_CLOUD_PROJECT"))
    key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    is_placeholder = key.lower().startswith(("paste", "your")) or "api-key-here" in key.lower()
    return bool(key) and not is_placeholder


def llm_configured() -> bool:
    if not _key_present():
        load_dotenv(override=True)  # pick up a key saved to .env after startup, no restart needed
    return _key_present()


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    user_id: str = "demo-user"
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    agents_called: list[str]


@app.get("/", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
@app.get("/inventory", include_in_schema=False)
@app.get("/procurement", include_in_schema=False)
@app.get("/analytics", include_in_schema=False)
@app.get("/agents", include_in_schema=False)
def web_ui() -> FileResponse:
    # no-cache: browsers always revalidate, so UI updates show up on a normal refresh
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/status")
def status() -> dict:
    return {"llm_configured": llm_configured(), "orchestrator_model": ORCHESTRATOR_MODEL,
            "subagent_model": SUBAGENT_MODEL}


NOT_CONFIGURED = ("Gemini is not configured. Put your GOOGLE_API_KEY in .env and save the file "
                  "(free key: https://aistudio.google.com/apikey), then try again.")
SUB_AGENTS = {"inventory_agent", "procurement_agent", "analytics_agent"}


def _error_for(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, genai_errors.APIError):
        logger.warning("Gemini API error %s: %s", exc.code, exc.message)
        if exc.code == 429:
            return 429, ("Gemini free-tier quota reached. Wait a minute (per-minute limit) or until "
                         "the daily reset, or use another API key.")
        if exc.code == 404:
            return 502, f"Model not available for this API key ({exc.message}). Set ORCHESTRATOR_MODEL / SUBAGENT_MODEL in .env."
        return 502, f"Gemini error {exc.code}: {exc.message}"
    logger.exception("Agent run failed")
    return 502, f"Agent error: {exc}"


async def _run_agents(req: ChatRequest, emit) -> tuple[str, str, list[str]]:
    """Run the orchestrator; `emit(event_dict)` receives live activity. Returns (session_id, reply, agents_called)."""
    session_id = req.session_id or uuid.uuid4().hex
    if not await session_service.get_session(app_name=APP_NAME, user_id=req.user_id, session_id=session_id):
        await session_service.create_session(app_name=APP_NAME, user_id=req.user_id, session_id=session_id)
    emit({"type": "session", "session_id": session_id})

    TRACE_SINK.set(emit)  # sub-agent tool calls are reported through the same channel
    message = types.Content(role="user", parts=[types.Part(text=req.message)])
    reply, agents_called = "", []
    async for event in runner.run_async(user_id=req.user_id, session_id=session_id, new_message=message):
        for call in event.get_function_calls():
            agents_called.append(call.name)
            if call.name in SUB_AGENTS:
                emit({"type": "delegate", "agent": call.name, "request": (call.args or {}).get("request", "")})
        for resp in event.get_function_responses():
            if resp.name in SUB_AGENTS:
                emit({"type": "agent_done", "agent": resp.name})
        if event.is_final_response() and event.content and event.content.parts:
            reply = "".join(part.text or "" for part in event.content.parts)
    return session_id, reply or "(no response)", agents_called


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if not llm_configured():
        raise HTTPException(503, NOT_CONFIGURED)
    try:
        session_id, reply, agents_called = await _run_agents(req, lambda event: None)
    except Exception as exc:
        status_code, detail = _error_for(exc)
        raise HTTPException(status_code, detail) from exc
    return ChatResponse(session_id=session_id, reply=reply, agents_called=agents_called)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Same as /chat but streams newline-delimited JSON events while the agents work:
    session | delegate | tool | agent_done | final | error."""
    if not llm_configured():
        raise HTTPException(503, NOT_CONFIGURED)
    queue: asyncio.Queue = asyncio.Queue()

    async def produce() -> None:
        try:
            session_id, reply, agents_called = await _run_agents(req, queue.put_nowait)
            queue.put_nowait({"type": "final", "session_id": session_id, "reply": reply, "agents_called": agents_called})
        except Exception as exc:
            queue.put_nowait({"type": "error", "message": _error_for(exc)[1]})
        queue.put_nowait(None)

    async def stream():
        task = asyncio.create_task(produce())
        try:
            while (event := await queue.get()) is not None:
                yield json.dumps(event, default=str) + "\n"
        finally:
            task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


# Plain REST endpoints (no LLM) used by the dashboard.
@app.get("/api/dashboard")
def dashboard() -> dict:
    """Chart data: weekly flow, stock health, category value, top movers, KPIs."""
    return dashboard_metrics()


@app.get("/api/warehouses")
def warehouse_list() -> dict:
    return tools.list_warehouses()


@app.get("/api/low-stock")
def low_stock(warehouse: Optional[str] = None) -> dict:
    return tools.list_low_stock(warehouse=warehouse)


@app.get("/api/dead-stock")
def dead_stock_list(days: int = 60) -> dict:
    return tools.dead_stock(days=days)


@app.get("/api/purchase-orders")
def purchase_order_list(status: Optional[str] = None) -> dict:
    return tools.list_purchase_orders(status=status)


@app.post("/api/purchase-orders/{po_id}/submit")
def approve_purchase_order(po_id: int) -> dict:
    """Human approval from the UI: submit a draft PO to its supplier."""
    result = tools.submit_purchase_order(po_id)
    if result["status"] != "success":
        raise HTTPException(400, result["message"])
    return result


@app.get("/api/skus")
def sku_list() -> dict:
    return tools.search_skus("")


@app.get("/api/demand-trend")
def demand_trend(sku: str, weeks: int = 12) -> dict:
    result = tools.demand_trend(sku, weeks)
    if result["status"] != "success":
        raise HTTPException(404, result["message"])
    return result


@app.get("/api/suppliers")
def supplier_list() -> dict:
    return supplier_summary()
