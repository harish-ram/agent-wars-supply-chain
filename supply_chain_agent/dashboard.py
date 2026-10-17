"""Aggregations behind the web dashboard's charts (plain SQL, no LLM calls)."""
from collections import defaultdict
from datetime import timedelta

from .tools import _now, _rows, _to_dt, stock_turnover

WEEKS = 12
EXCESS_DAYS_OF_COVER = 90


def dashboard_metrics() -> dict:
    now = _now()
    start = now - timedelta(weeks=WEEKS)

    # Network flow in rolling 7-day windows ending now, so the latest week is never a partial dip.
    inbound, outbound = [0] * WEEKS, [0] * WEEKS
    for r in _rows("SELECT ts, qty_delta FROM stock_movements WHERE ts >= :cutoff", cutoff=start):
        week = min(int((_to_dt(r["ts"]) - start).total_seconds() // (7 * 86400)), WEEKS - 1)
        if r["qty_delta"] > 0:
            inbound[week] += r["qty_delta"]
        else:
            outbound[week] -= r["qty_delta"]
    labels = [(start + timedelta(weeks=i)).strftime("%b %d") for i in range(WEEKS)]

    sold_90d = {(r["sku_id"], r["warehouse_id"]): int(r["units"]) for r in _rows(
        """SELECT sku_id, warehouse_id, SUM(-qty_delta) AS units FROM stock_movements
           WHERE type = 'outbound' AND ts >= :cutoff GROUP BY sku_id, warehouse_id""",
        cutoff=now - timedelta(days=90),
    )}
    positions = _rows(
        """SELECT i.sku_id, i.warehouse_id, w.name AS warehouse, s.category, s.unit_cost, s.reorder_point,
                  i.qty_on_hand, i.qty_on_hand - i.qty_reserved AS available
           FROM inventory i JOIN skus s ON s.id = i.sku_id JOIN warehouses w ON w.id = i.warehouse_id
           ORDER BY w.id, i.sku_id"""
    )

    health = {}
    categories = defaultdict(lambda: {"inventory_value": 0.0, "units_on_hand": 0, "units_sold_90d": 0})
    for p in positions:
        sold = sold_90d.get((p["sku_id"], p["warehouse_id"]), 0)
        daily = sold / 90
        counts = health.setdefault(p["warehouse"], {"stockout": 0, "low": 0, "healthy": 0, "excess": 0})
        if p["available"] <= 0:
            counts["stockout"] += 1
        elif p["available"] <= p["reorder_point"]:
            counts["low"] += 1
        elif daily == 0 or p["available"] / daily > EXCESS_DAYS_OF_COVER:
            counts["excess"] += 1
        else:
            counts["healthy"] += 1
        cat = categories[p["category"]]
        cat["inventory_value"] += p["qty_on_hand"] * p["unit_cost"]
        cat["units_on_hand"] += p["qty_on_hand"]
        cat["units_sold_90d"] += sold

    movers = stock_turnover(days=90, top_n=8)
    last_4w, prev_4w = sum(outbound[-4:]), sum(outbound[-8:-4])
    return {
        "status": "success",
        "weekly_flow": {"labels": labels, "inbound": inbound, "outbound": outbound},
        "stock_health": [{"warehouse": name, **counts} for name, counts in health.items()],
        "categories": sorted(
            ({"category": name, **c, "inventory_value": round(c["inventory_value"], 2)} for name, c in categories.items()),
            key=lambda c: c["inventory_value"], reverse=True,
        ),
        "top_movers": movers.get("fastest_movers", []),
        "slow_movers": movers.get("slowest_movers", []),
        "kpis": {
            "units_shipped_4w": last_4w,
            "units_shipped_prev_4w": prev_4w,
            "shipped_change_pct": round((last_4w - prev_4w) / prev_4w * 100, 1) if prev_4w else None,
            "units_received_4w": sum(inbound[-4:]),
        },
    }


def supplier_summary() -> dict:
    """Suppliers with lead time, catalogue size and average price vs unit cost (1.00 = at cost)."""
    rows = _rows(
        """SELECT sp.id, sp.name, sp.lead_time_days, sp.contact, COUNT(ss.sku_id) AS skus_supplied,
                  AVG(ss.unit_price / s.unit_cost) AS price_index
           FROM suppliers sp
           LEFT JOIN sku_suppliers ss ON ss.supplier_id = sp.id
           LEFT JOIN skus s ON s.id = ss.sku_id
           GROUP BY sp.id, sp.name, sp.lead_time_days, sp.contact ORDER BY sp.id"""
    )
    suppliers = [{**r, "skus_supplied": int(r["skus_supplied"]),
                  "price_index": round(float(r["price_index"]), 2) if r["price_index"] is not None else None} for r in rows]
    return {"status": "success", "suppliers": suppliers}
