"""Multi-agent supply-chain copilot built with Google ADK + Gemini.

root_agent (orchestrator) -> AgentTool(inventory_agent | procurement_agent | analytics_agent)
`adk web`, `adk run supply_chain_agent` and main.py all load `root_agent` from here.
"""
import os
from contextvars import ContextVar
from typing import Callable, Optional

from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from . import tools
from .seed import seed

seed()  # creates tables + mock data on first start; no-op afterwards

# Defaults suit the Gemini API free tier: auto-updating aliases (the gemini-2.5-* models were retired
# for new keys on 2026-10-16) and the higher-quota Flash-Lite for the specialists. Override in .env.
ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "gemini-flash-latest")
SUBAGENT_MODEL = os.getenv("SUBAGENT_MODEL", "gemini-flash-lite-latest")

# One question costs 4-8 model calls, which quickly hits free-tier per-minute limits. Every model call
# therefore retries 429/5xx with exponential backoff (5s, 10s, 20s, 40s) instead of failing the chat.
MODEL_CONFIG = types.GenerateContentConfig(
    http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(
        attempts=int(os.getenv("LLM_RETRY_ATTEMPTS", "5")),
        initial_delay=5.0, max_delay=60.0, exp_base=2.0,
        http_status_codes=[429, 500, 502, 503, 504],
    )),
)

#: Set by the API layer per request; sub-agent tool calls are reported to it so the UI can show live activity.
TRACE_SINK: ContextVar[Optional[Callable[[dict], None]]] = ContextVar("trace_sink", default=None)


def _report_tool_call(tool, args, tool_context):
    sink = TRACE_SINK.get()
    if sink:
        sink({"type": "tool", "agent": tool_context.agent_name, "tool": tool.name, "args": args})
    return None  # never short-circuits the tool


GROUNDING = (
    "Only state numbers that came from your tools; never invent data. If a tool returns status 'error', "
    "explain the problem or ask a clarifying question. Keep answers short and use markdown tables for lists. "
    "Format for readability: start with a one-line direct answer, use short bullet points, bold the key numbers, "
    "put units/currency in every figure, and end with a single 'Recommended action' line when relevant."
)

inventory_agent = LlmAgent(
    name="inventory_agent",
    model=SUBAGENT_MODEL,
    generate_content_config=MODEL_CONFIG,
    description="Stock levels per warehouse, available quantities, low-stock items, SKU lookup and warehouse capacity.",
    instruction=(
        "You are the Inventory specialist for a network of 3 warehouses (Chicago DC, Rotterdam DC, Singapore DC).\n"
        "- If the user names a product instead of a SKU id, use search_skus first.\n"
        "- get_stock for one SKU, list_low_stock for shortages, list_warehouses for capacity and utilisation.\n"
        "- Available stock = on hand minus reserved. Call out stockouts and positions below the reorder point.\n"
        + GROUNDING
    ),
    tools=[tools.search_skus, tools.get_stock, tools.list_low_stock, tools.list_warehouses],
    before_tool_callback=_report_tool_call,
)

procurement_agent = LlmAgent(
    name="procurement_agent",
    model=SUBAGENT_MODEL,
    generate_content_config=MODEL_CONFIG,
    description="Supplier comparison, reorder recommendations (EOQ / reorder point), drafting, submitting and listing purchase orders.",
    instruction=(
        "You are the Procurement specialist.\n"
        "- get_suppliers_for_sku to compare price, MOQ and lead time.\n"
        "- recommend_reorder before proposing any quantity; summarise reorder point, safety stock, EOQ, "
        "chosen supplier and estimated cost in one or two lines.\n"
        "- When asked to order or reorder, call create_purchase_order with the recommended supplier, warehouse "
        "and quantity. New purchase orders are drafts.\n"
        "- Call submit_purchase_order ONLY when the request says the user explicitly approved that specific PO id.\n"
        "- list_purchase_orders for PO status questions. Always include PO ids and totals.\n"
        + GROUNDING
    ),
    tools=[tools.search_skus, tools.get_suppliers_for_sku, tools.recommend_reorder,
           tools.create_purchase_order, tools.submit_purchase_order, tools.list_purchase_orders],
    before_tool_callback=_report_tool_call,
)

analytics_agent = LlmAgent(
    name="analytics_agent",
    model=SUBAGENT_MODEL,
    generate_content_config=MODEL_CONFIG,
    description="Movement history, demand trends, inventory turnover (fast/slow movers) and dead stock.",
    instruction=(
        "You are the Supply Chain Analytics specialist.\n"
        "- movement_history: weekly receipts vs shipments for a SKU.\n"
        "- demand_trend: weekly demand and whether it is rising, falling or stable.\n"
        "- stock_turnover: fastest and slowest movers.\n"
        "- dead_stock: stock with no recent sales and the cash tied up in it.\n"
        "Explain insights in plain business language with the key numbers and one recommended action.\n"
        + GROUNDING
    ),
    tools=[tools.search_skus, tools.movement_history, tools.demand_trend, tools.stock_turnover, tools.dead_stock],
    before_tool_callback=_report_tool_call,
)

root_agent = LlmAgent(
    name="supply_chain_orchestrator",
    model=ORCHESTRATOR_MODEL,
    generate_content_config=MODEL_CONFIG,
    description="Supply-chain copilot that routes requests to inventory, procurement and analytics specialists.",
    instruction=(
        "You are the Supply Chain Copilot for an operations team (3 warehouses, 50 SKUs, 5 suppliers).\n"
        "You never query data yourself; delegate to your specialist tools:\n"
        "- inventory_agent: stock levels, availability, low stock, product lookup, warehouse capacity.\n"
        "- procurement_agent: suppliers, reorder recommendations, drafting / submitting / listing purchase orders.\n"
        "- analytics_agent: movement history, demand trends, turnover, dead stock.\n"
        "Rules:\n"
        "1. Specialists cannot see this conversation: send each one a complete, self-contained request with the "
        "exact SKU ids, warehouse names, PO ids and numbers it needs.\n"
        "2. For multi-step requests (e.g. find low stock, then reorder it) call specialists in sequence and pass "
        "results forward. Call independent specialists as needed and combine their answers.\n"
        "3. Drafting a purchase order is fine when the user asks to order. Submitting a PO to a supplier requires "
        "the user's explicit approval of that PO id in this conversation; otherwise show the draft and ask.\n"
        "4. Lead with the answer, then brief supporting detail and a suggested next action. Never invent numbers.\n"
        "5. Format the final answer in clean markdown: a one-line bold summary first, then a table for lists of items (short column names, no empty cells), bullets for insights, bold key numbers, and a final '**Recommended action:**' line. Never output raw JSON or tool names.\n"
        "6. For requests unrelated to supply-chain operations, briefly say what you can help with."
    ),
    tools=[AgentTool(agent=inventory_agent), AgentTool(agent=procurement_agent), AgentTool(agent=analytics_agent)],
)
