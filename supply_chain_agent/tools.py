"""Function tools for the ADK agents.

Each tool is a typed Python function with a Google-style docstring (ADK turns these into
Gemini function declarations) and returns a JSON-serialisable dict with a "status" key.
"""
import functools
import logging
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, bindparam, text

from .db import engine, po_lines, purchase_orders

logger = logging.getLogger(__name__)

ORDERING_COST = 50.0  # cost of placing one PO (EOQ "S")
HOLDING_RATE = 0.25  # yearly holding cost as a share of unit cost (EOQ "H")
SERVICE_Z = 1.65  # z-score for ~95% cycle service level
DEMAND_WINDOW_DAYS = 90


# ----------------------------------------------------------------------------- helpers
def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_dt(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _rows(sql: str, **params) -> list[dict]:
    stmt = text(sql)
    typed = [bindparam(k, type_=DateTime) for k, v in params.items() if isinstance(v, datetime)]
    if typed:
        stmt = stmt.bindparams(*typed)
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(stmt, params)]


def _clean(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            value = value.isoformat(timespec="seconds")
        elif isinstance(value, Decimal):
            value = int(value) if value == value.to_integral_value() else round(float(value), 2)
        elif isinstance(value, float):
            value = round(value, 2)
        out[key] = value
    return out


def tool(fn):
    """Wrap a tool so failures come back to the agent as {"status": "error"} instead of crashing."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return {"status": "success", **fn(*args, **kwargs)}
        except (LookupError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:  # DB/runtime failure
            logger.exception("Tool %s failed", fn.__name__)
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    return wrapper


def _resolve_sku(sku: str) -> dict:
    key = (sku or "").strip()
    if not key:
        raise ValueError("A SKU id or product name is required.")
    rows = _rows("SELECT * FROM skus WHERE UPPER(id) = :k", k=key.upper())
    if not rows:
        rows = _rows("SELECT * FROM skus WHERE LOWER(name) LIKE :q ORDER BY id LIMIT 5", q=f"%{key.lower()}%")
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise LookupError(f"No SKU matches '{sku}'. Use search_skus to find valid SKU ids.")
    options = ", ".join(f"{r['id']} ({r['name']})" for r in rows)
    raise LookupError(f"'{sku}' matches several SKUs: {options}. Ask which one is meant.")


def _resolve_warehouse(warehouse: str) -> dict:
    key = str(warehouse or "").strip()
    if not key:
        raise ValueError("A warehouse name, city or id is required.")
    if key.isdigit():
        rows = _rows("SELECT * FROM warehouses WHERE id = :i", i=int(key))
    else:
        rows = _rows("SELECT * FROM warehouses WHERE LOWER(name) LIKE :q OR LOWER(location) LIKE :q",
                     q=f"%{key.lower()}%")
    if len(rows) == 1:
        return rows[0]
    names = ", ".join(r["name"] for r in _rows("SELECT name FROM warehouses ORDER BY id"))
    raise LookupError(f"Unknown or ambiguous warehouse '{warehouse}'. Valid warehouses: {names}.")


def _suppliers_for(sku_id: str) -> list[dict]:
    return _rows(
        """SELECT sp.id AS supplier_id, sp.name AS supplier, sp.lead_time_days, sp.contact, ss.unit_price, ss.moq
           FROM sku_suppliers ss JOIN suppliers sp ON sp.id = ss.supplier_id
           WHERE ss.sku_id = :sku ORDER BY ss.unit_price, sp.lead_time_days""",
        sku=sku_id,
    )


def _daily_demand(sku_id: str, warehouse_id: Optional[int] = None, days: int = DEMAND_WINDOW_DAYS):
    """Mean and std-dev of daily outbound units over the last `days` days (zero-demand days included)."""
    sql = "SELECT ts, qty_delta FROM stock_movements WHERE sku_id = :sku AND type = 'outbound' AND ts >= :cutoff"
    params = {"sku": sku_id, "cutoff": _now() - timedelta(days=days)}
    if warehouse_id is not None:
        sql += " AND warehouse_id = :wid"
        params["wid"] = warehouse_id
    daily = defaultdict(int)
    for r in _rows(sql, **params):
        daily[_to_dt(r["ts"]).date()] -= r["qty_delta"]
    today = _now().date()
    series = [daily.get(today - timedelta(days=i), 0) for i in range(days)]
    return statistics.fmean(series), statistics.pstdev(series)


def _po_detail(po_id: int) -> dict:
    head = _rows(
        """SELECT p.id AS po_id, p.status, p.created_at, p.supplier_id, sp.name AS supplier,
                  sp.lead_time_days, sp.contact, w.name AS warehouse
           FROM purchase_orders p
           JOIN suppliers sp ON sp.id = p.supplier_id
           JOIN warehouses w ON w.id = p.warehouse_id
           WHERE p.id = :po""",
        po=po_id,
    )
    if not head:
        raise LookupError(f"Purchase order {po_id} not found.")
    lines = [_clean(r) for r in _rows(
        """SELECT l.sku_id AS sku, s.name, l.qty, l.unit_price, l.qty * l.unit_price AS line_total
           FROM po_lines l JOIN skus s ON s.id = l.sku_id WHERE l.po_id = :po ORDER BY l.sku_id""",
        po=po_id,
    )]
    po = _clean(head[0])
    po["lines"] = lines
    po["total_value"] = round(sum(line["line_total"] for line in lines), 2)
    return po


# ----------------------------------------------------------------------------- inventory
@tool
def search_skus(query: str, category: Optional[str] = None) -> dict:
    """Find products (SKUs) by keyword, SKU id or category.

    Args:
        query: Keyword such as "cable", "box" or a SKU id like "SKU-1003". Use "" to list everything.
        category: Optional category filter: Electronics, Packaging, Industrial, Office or Grocery.

    Returns:
        Matching SKUs with id, name, category, unit cost, reorder point and reorder quantity.
    """
    sql = """SELECT id, name, category, unit_cost, reorder_point, reorder_qty FROM skus
             WHERE (LOWER(name) LIKE :q OR LOWER(id) LIKE :q)"""
    params = {"q": f"%{(query or '').strip().lower()}%"}
    if category:
        sql += " AND LOWER(category) = :c"
        params["c"] = category.strip().lower()
    rows = [_clean(r) for r in _rows(sql + " ORDER BY id LIMIT 50", **params)]
    return {"count": len(rows), "skus": rows}


@tool
def get_stock(sku: str, warehouse: Optional[str] = None) -> dict:
    """Get on-hand, reserved and available stock for one SKU, per warehouse.

    Args:
        sku: SKU id (e.g. "SKU-1003") or product name.
        warehouse: Optional warehouse name, city or id. Omit to see every warehouse.

    Returns:
        Stock per warehouse, network total, and whether each location is at or below the reorder point.
    """
    s = _resolve_sku(sku)
    sql = """SELECT w.name AS warehouse, i.qty_on_hand, i.qty_reserved, i.updated_at
             FROM inventory i JOIN warehouses w ON w.id = i.warehouse_id WHERE i.sku_id = :sku"""
    params = {"sku": s["id"]}
    if warehouse:
        sql += " AND w.id = :wid"
        params["wid"] = _resolve_warehouse(warehouse)["id"]
    locations = []
    for r in _rows(sql + " ORDER BY w.id", **params):
        r = _clean(r)
        r["available"] = r["qty_on_hand"] - r["qty_reserved"]
        r["below_reorder_point"] = r["available"] <= s["reorder_point"]
        locations.append(r)
    return {
        "sku": s["id"], "name": s["name"], "category": s["category"], "reorder_point": s["reorder_point"],
        "total_on_hand": sum(r["qty_on_hand"] for r in locations),
        "total_available": sum(r["available"] for r in locations),
        "locations": locations,
    }


@tool
def list_low_stock(warehouse: Optional[str] = None, threshold: Optional[int] = None) -> dict:
    """List SKU/warehouse positions whose available stock (on hand minus reserved) is at or below the reorder point.

    Args:
        warehouse: Optional warehouse name, city or id to restrict the check.
        threshold: Optional fixed unit threshold to use instead of each SKU's own reorder point.

    Returns:
        Low-stock positions sorted by largest shortfall first.
    """
    limit_expr = ":threshold" if threshold is not None else "s.reorder_point"
    sql = f"""SELECT s.id AS sku, s.name, s.category, w.name AS warehouse, i.qty_on_hand, i.qty_reserved,
                     i.qty_on_hand - i.qty_reserved AS available, s.reorder_point, s.reorder_qty
              FROM inventory i JOIN skus s ON s.id = i.sku_id JOIN warehouses w ON w.id = i.warehouse_id
              WHERE i.qty_on_hand - i.qty_reserved <= {limit_expr}"""
    params = {}
    if threshold is not None:
        params["threshold"] = int(threshold)
    if warehouse:
        sql += " AND w.id = :wid"
        params["wid"] = _resolve_warehouse(warehouse)["id"]
    items = [_clean(r) for r in _rows(sql, **params)]
    for item in items:
        item["shortfall"] = item["reorder_point"] - item["available"]
        item["stockout"] = item["available"] <= 0
    items.sort(key=lambda i: i["shortfall"], reverse=True)
    return {"count": len(items), "items": items[:50]}


@tool
def list_warehouses() -> dict:
    """List warehouses with location, capacity, units on hand, utilisation and inventory value.

    Returns:
        One entry per warehouse.
    """
    rows = _rows(
        """SELECT w.id, w.name, w.location, w.capacity,
                  COALESCE(SUM(i.qty_on_hand), 0) AS units_on_hand,
                  COALESCE(SUM(i.qty_on_hand * s.unit_cost), 0) AS inventory_value
           FROM warehouses w
           LEFT JOIN inventory i ON i.warehouse_id = w.id
           LEFT JOIN skus s ON s.id = i.sku_id
           GROUP BY w.id, w.name, w.location, w.capacity ORDER BY w.id"""
    )
    items = [_clean(r) for r in rows]
    for w in items:
        w["utilization_pct"] = round(100 * w["units_on_hand"] / w["capacity"], 1)
    return {"warehouses": items}


# ----------------------------------------------------------------------------- procurement
@tool
def get_suppliers_for_sku(sku: str) -> dict:
    """Compare the suppliers that can supply a SKU (unit price, minimum order quantity, lead time).

    Args:
        sku: SKU id or product name.

    Returns:
        Suppliers sorted by unit price (cheapest first).
    """
    s = _resolve_sku(sku)
    options = [_clean(o) for o in _suppliers_for(s["id"])]
    if not options:
        raise LookupError(f"No supplier is linked to {s['id']}.")
    return {"sku": s["id"], "name": s["name"], "unit_cost": s["unit_cost"], "suppliers": options}


def _reorder_for(s: dict, wh: dict, options: list[dict]) -> dict:
    inv = _rows("SELECT qty_on_hand, qty_reserved FROM inventory WHERE sku_id = :s AND warehouse_id = :w",
                s=s["id"], w=wh["id"])
    available = (inv[0]["qty_on_hand"] - inv[0]["qty_reserved"]) if inv else 0
    on_order = int(_rows(
        """SELECT COALESCE(SUM(l.qty), 0) AS q FROM po_lines l JOIN purchase_orders p ON p.id = l.po_id
           WHERE l.sku_id = :s AND p.warehouse_id = :w AND p.status IN ('draft', 'submitted')""",
        s=s["id"], w=wh["id"],
    )[0]["q"])
    mean, std = _daily_demand(s["id"], wh["id"])

    cheapest = options[0]
    fastest = min(options, key=lambda o: (o["lead_time_days"], o["unit_price"]))
    # If we would stock out before the cheapest supplier can deliver, pay for speed.
    urgent = mean > 0 and available < mean * cheapest["lead_time_days"]
    best = fastest if urgent else cheapest

    lead = best["lead_time_days"]
    safety = SERVICE_Z * std * math.sqrt(lead)
    rop = mean * lead + safety
    holding = s["unit_cost"] * HOLDING_RATE
    eoq = math.sqrt(2 * mean * 365 * ORDERING_COST / holding) if mean > 0 and holding > 0 else 0.0
    position = available + on_order
    needs = mean > 0 and position <= rop
    qty = max(math.ceil(rop + eoq - position), best["moq"]) if needs else 0

    if not needs:
        rationale = "Inventory position (available + on order) is above the reorder point; no order needed."
    else:
        rationale = ("Stock runs out before the cheapest supplier can deliver, so the fastest supplier is chosen. "
                     if urgent else "Cheapest supplier chosen. ")
        rationale += f"Quantity = reorder point + EOQ - inventory position, at least MOQ {best['moq']}."

    return {
        "warehouse": wh["name"], "available": available, "on_order": on_order,
        "avg_daily_demand": round(mean, 2), "days_of_cover": round(available / mean, 1) if mean else None,
        "safety_stock": round(safety), "calculated_reorder_point": round(rop), "eoq": round(eoq),
        "needs_reorder": needs, "urgent": urgent and needs, "recommended_qty": qty,
        "recommended_supplier": _clean(best), "estimated_cost": round(qty * best["unit_price"], 2),
        "rationale": rationale,
    }


@tool
def recommend_reorder(sku: str, warehouse: Optional[str] = None) -> dict:
    """Recommend whether and how much to reorder, using 90-day demand, safety stock, reorder point and EOQ.

    Args:
        sku: SKU id or product name.
        warehouse: Optional warehouse name, city or id. Omit to evaluate every warehouse.

    Returns:
        Per warehouse: demand, days of cover, reorder point, EOQ, recommended quantity, supplier and cost.
    """
    s = _resolve_sku(sku)
    options = _suppliers_for(s["id"])
    if not options:
        raise LookupError(f"No supplier is linked to {s['id']}.")
    whs = [_resolve_warehouse(warehouse)] if warehouse else _rows("SELECT * FROM warehouses ORDER BY id")
    return {"sku": s["id"], "name": s["name"], "recommendations": [_reorder_for(s, w, options) for w in whs]}


@tool
def create_purchase_order(supplier_id: int, warehouse: str, sku_ids: list[str], quantities: list[int]) -> dict:
    """Create a DRAFT purchase order. Drafts are not sent to the supplier until submit_purchase_order is called.

    Args:
        supplier_id: Supplier id from get_suppliers_for_sku or recommend_reorder.
        warehouse: Destination warehouse name, city or id.
        sku_ids: SKU ids to order, e.g. ["SKU-1003", "SKU-1010"].
        quantities: Quantity for each SKU, same order and length as sku_ids.

    Returns:
        The draft purchase order with lines, total value and any MOQ adjustments.
    """
    if not sku_ids or len(sku_ids) != len(quantities):
        raise ValueError("sku_ids and quantities must be non-empty lists of the same length.")
    wh = _resolve_warehouse(warehouse)
    supplier = _rows("SELECT * FROM suppliers WHERE id = :i", i=int(supplier_id))
    if not supplier:
        raise LookupError(f"Supplier {supplier_id} not found.")

    lines, notes = {}, []
    for raw_sku, raw_qty in zip(sku_ids, quantities):
        s = _resolve_sku(raw_sku)
        qty = int(raw_qty)
        if qty <= 0:
            raise ValueError(f"Quantity for {s['id']} must be positive.")
        offer = _rows("SELECT unit_price, moq FROM sku_suppliers WHERE sku_id = :s AND supplier_id = :p",
                      s=s["id"], p=int(supplier_id))
        if not offer:
            raise ValueError(f"{supplier[0]['name']} does not supply {s['id']}. Check get_suppliers_for_sku.")
        qty += lines.get(s["id"], {}).get("qty", 0)
        if qty < offer[0]["moq"]:
            notes.append(f"{s['id']}: quantity raised from {qty} to supplier MOQ {offer[0]['moq']}.")
            qty = offer[0]["moq"]
        lines[s["id"]] = {"sku_id": s["id"], "qty": qty, "unit_price": offer[0]["unit_price"]}

    with engine.begin() as conn:
        po_id = conn.execute(purchase_orders.insert().values(
            supplier_id=int(supplier_id), warehouse_id=wh["id"], status="draft", created_at=_now(),
        )).inserted_primary_key[0]
        conn.execute(po_lines.insert(), [{"po_id": po_id, **line} for line in lines.values()])
    return {"purchase_order": _po_detail(po_id), "adjustments": notes,
            "next_step": "Show the draft to the user; submit only after they explicitly approve this PO id."}


@tool
def submit_purchase_order(po_id: int) -> dict:
    """Submit a DRAFT purchase order to its supplier. Only call this after the user explicitly approved this PO.

    Args:
        po_id: Purchase order id returned by create_purchase_order.

    Returns:
        The submitted purchase order with its expected delivery date.
    """
    po = _po_detail(int(po_id))
    if po["status"] != "draft":
        raise ValueError(f"PO {po_id} is '{po['status']}'; only draft POs can be submitted.")
    with engine.begin() as conn:
        conn.execute(text("UPDATE purchase_orders SET status = 'submitted' WHERE id = :po AND status = 'draft'"),
                     {"po": int(po_id)})
    po["status"] = "submitted"
    po["expected_delivery"] = (_now() + timedelta(days=po["lead_time_days"])).date().isoformat()
    return {"purchase_order": po}


@tool
def list_purchase_orders(status: Optional[str] = None) -> dict:
    """List the 25 most recent purchase orders with supplier, destination, line count and value.

    Args:
        status: Optional filter: "draft" or "submitted".

    Returns:
        Purchase orders, newest first.
    """
    sql = """SELECT p.id AS po_id, p.status, p.created_at, sp.name AS supplier, w.name AS warehouse,
                    COUNT(l.sku_id) AS lines, COALESCE(SUM(l.qty * l.unit_price), 0) AS total_value
             FROM purchase_orders p
             JOIN suppliers sp ON sp.id = p.supplier_id
             JOIN warehouses w ON w.id = p.warehouse_id
             LEFT JOIN po_lines l ON l.po_id = p.id"""
    params = {}
    if status:
        sql += " WHERE p.status = :status"
        params["status"] = status.strip().lower()
    sql += " GROUP BY p.id, p.status, p.created_at, sp.name, w.name ORDER BY p.id DESC LIMIT 25"
    orders = [_clean(r) for r in _rows(sql, **params)]
    return {"count": len(orders), "purchase_orders": orders}


# ----------------------------------------------------------------------------- analytics
@tool
def movement_history(sku: str, days: int = 30, warehouse: Optional[str] = None) -> dict:
    """Weekly inbound (receipts) and outbound (shipments) units for a SKU.

    Args:
        sku: SKU id or product name.
        days: Look-back window in days (7-180). Defaults to 30.
        warehouse: Optional warehouse name, city or id. Omit for the whole network.

    Returns:
        Totals and a week-by-week breakdown.
    """
    s = _resolve_sku(sku)
    days = max(7, min(int(days), 180))
    sql = "SELECT ts, qty_delta FROM stock_movements WHERE sku_id = :sku AND ts >= :cutoff"
    params = {"sku": s["id"], "cutoff": _now() - timedelta(days=days)}
    if warehouse:
        sql += " AND warehouse_id = :wid"
        params["wid"] = _resolve_warehouse(warehouse)["id"]
    weeks = defaultdict(lambda: {"inbound": 0, "outbound": 0})
    for r in _rows(sql, **params):
        day = _to_dt(r["ts"]).date()
        bucket = weeks[(day - timedelta(days=day.weekday())).isoformat()]
        bucket["inbound" if r["qty_delta"] > 0 else "outbound"] += abs(r["qty_delta"])
    weekly = [{"week_start": k, **v} for k, v in sorted(weeks.items())]
    return {
        "sku": s["id"], "name": s["name"], "days": days,
        "total_inbound": sum(w["inbound"] for w in weekly),
        "total_outbound": sum(w["outbound"] for w in weekly),
        "weekly": weekly,
    }


@tool
def demand_trend(sku: str, weeks: int = 12) -> dict:
    """Weekly demand for a SKU and whether it is rising, falling or stable (last 4 weeks vs the 4 before).

    Args:
        sku: SKU id or product name.
        weeks: Number of weeks to analyse (8-26). Defaults to 12.

    Returns:
        Weekly demand series, average, percent change, slope (units/week) and trend direction.
    """
    s = _resolve_sku(sku)
    weeks = max(8, min(int(weeks), 26))
    start = _now() - timedelta(weeks=weeks)
    series = [0] * weeks
    for r in _rows("SELECT ts, qty_delta FROM stock_movements WHERE sku_id = :sku AND type = 'outbound' AND ts >= :cutoff",
                   sku=s["id"], cutoff=start):
        series[min((_to_dt(r["ts"]) - start).days // 7, weeks - 1)] -= r["qty_delta"]
    recent, prior = statistics.fmean(series[-4:]), statistics.fmean(series[-8:-4])
    change = round((recent - prior) / prior * 100, 1) if prior else None
    x_mean, y_mean = (weeks - 1) / 2, statistics.fmean(series)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in enumerate(series)) / sum((x - x_mean) ** 2 for x in range(weeks))
    if change is None:
        direction = "new demand" if recent > 0 else "no demand"
    else:
        direction = "rising" if change > 10 else "falling" if change < -10 else "stable"
    return {
        "sku": s["id"], "name": s["name"], "weekly_units": series, "avg_weekly_units": round(y_mean, 1),
        "last_4_weeks_avg": round(recent, 1), "prior_4_weeks_avg": round(prior, 1),
        "change_pct": change, "slope_units_per_week": round(slope, 2), "direction": direction,
    }


@tool
def stock_turnover(days: int = 90, top_n: int = 5) -> dict:
    """Rank SKUs by inventory turnover (annualised units sold / units on hand): fastest and slowest movers.

    Args:
        days: Sales window in days (7-180). Defaults to 90.
        top_n: How many SKUs to return in each list (1-20). Defaults to 5.

    Returns:
        Fastest and slowest movers with turnover, days of supply and inventory value.
    """
    days, top_n = max(7, min(int(days), 180)), max(1, min(int(top_n), 20))
    sold = {r["sku_id"]: int(r["units"]) for r in _rows(
        """SELECT sku_id, SUM(-qty_delta) AS units FROM stock_movements
           WHERE type = 'outbound' AND ts >= :cutoff GROUP BY sku_id""",
        cutoff=_now() - timedelta(days=days),
    )}
    items = []
    for r in _rows("""SELECT s.id AS sku, s.name, s.category, s.unit_cost, COALESCE(SUM(i.qty_on_hand), 0) AS on_hand
                      FROM skus s LEFT JOIN inventory i ON i.sku_id = s.id
                      GROUP BY s.id, s.name, s.category, s.unit_cost"""):
        on_hand, units = int(r["on_hand"]), sold.get(r["sku"], 0)
        if on_hand <= 0:
            continue  # stocked out: turnover undefined
        daily = units / days
        items.append({
            "sku": r["sku"], "name": r["name"], "category": r["category"], "units_sold": units, "on_hand": on_hand,
            "annual_turnover": round(daily * 365 / on_hand, 2),
            "days_of_supply": round(on_hand / daily, 1) if daily else None,
            "inventory_value": round(on_hand * r["unit_cost"], 2),
        })
    items.sort(key=lambda i: i["annual_turnover"], reverse=True)
    return {"period_days": days, "fastest_movers": items[:top_n], "slowest_movers": items[::-1][:top_n]}


@tool
def dead_stock(days: int = 60) -> dict:
    """Find SKUs that have stock on hand but no sales in the last N days, and the cash tied up in them.

    Args:
        days: No-sales window in days (14-180). Defaults to 60.

    Returns:
        Dead-stock SKUs sorted by value tied up, plus the total value.
    """
    days = max(14, min(int(days), 180))
    now = _now()
    cutoff = now - timedelta(days=days)
    last_sale = {r["sku_id"]: _to_dt(r["last_sale"]) for r in _rows(
        "SELECT sku_id, MAX(ts) AS last_sale FROM stock_movements WHERE type = 'outbound' GROUP BY sku_id")}
    items = []
    for r in _rows("""SELECT s.id AS sku, s.name, s.category, s.unit_cost, SUM(i.qty_on_hand) AS on_hand
                      FROM skus s JOIN inventory i ON i.sku_id = s.id
                      GROUP BY s.id, s.name, s.category, s.unit_cost"""):
        on_hand, last = int(r["on_hand"] or 0), last_sale.get(r["sku"])
        if on_hand > 0 and (last is None or last < cutoff):
            items.append({
                "sku": r["sku"], "name": r["name"], "category": r["category"], "on_hand": on_hand,
                "last_sale": last.date().isoformat() if last else None,
                "days_since_last_sale": (now - last).days if last else None,
                "value_tied_up": round(on_hand * r["unit_cost"], 2),
            })
    items.sort(key=lambda i: i["value_tied_up"], reverse=True)
    return {"no_sales_window_days": days, "count": len(items),
            "total_value_tied_up": round(sum(i["value_tied_up"] for i in items), 2), "items": items}
