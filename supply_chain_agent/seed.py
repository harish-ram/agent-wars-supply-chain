"""Deterministic mock data: 3 warehouses, 50 SKUs, 5 suppliers, 6 months of stock movements.

Run `python -m supply_chain_agent.seed --reset` to rebuild the database from scratch.
The agent also calls seed() on startup, which only inserts data when the DB is empty.
"""
import argparse
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .db import (
    engine, inventory, metadata, po_lines, purchase_orders, sku_suppliers, skus,
    stock_movements, suppliers, warehouses,
)

DAYS = 180

WAREHOUSES = [
    {"id": 1, "name": "Chicago DC", "location": "Chicago, USA", "capacity": 60000},
    {"id": 2, "name": "Rotterdam DC", "location": "Rotterdam, Netherlands", "capacity": 45000},
    {"id": 3, "name": "Singapore DC", "location": "Singapore", "capacity": 35000},
]
WAREHOUSE_DEMAND = {1: 1.0, 2: 0.8, 3: 0.6}

SUPPLIERS = [
    {"id": 1, "name": "Apex Components Ltd", "lead_time_days": 7, "contact": "orders@apex-components.example"},
    {"id": 2, "name": "BluePeak Packaging Co", "lead_time_days": 4, "contact": "sales@bluepeak.example"},
    {"id": 3, "name": "Orion Industrial Supply", "lead_time_days": 12, "contact": "procurement@orion-supply.example"},
    {"id": 4, "name": "Nimbus Wholesale", "lead_time_days": 5, "contact": "b2b@nimbus.example"},
    {"id": 5, "name": "Global Sourcing Partners", "lead_time_days": 21, "contact": "trade@gsp.example"},
]

# category: (unit cost range, daily demand range per warehouse, product names)
CATALOG = {
    "Electronics": ((4, 180), (2, 12), [
        "USB-C Cable 1m", "Wireless Mouse", "Bluetooth Headset", "27in Monitor", "Mechanical Keyboard",
        "Power Bank 20000mAh", "HDMI Cable 2m", "Webcam 1080p", "Laptop Stand", "Smart Plug"]),
    "Packaging": ((0.2, 25), (10, 40), [
        "Corrugated Box Small", "Corrugated Box Medium", "Corrugated Box Large", "Bubble Wrap Roll",
        "Packing Tape", "Stretch Film", "Pallet Wrap", "Void Fill Paper", "Poly Mailer", "Shipping Labels 4x6"]),
    "Industrial": ((3, 90), (2, 10), [
        "Safety Gloves", "Hard Hat", "Hi-Vis Vest", "Steel Bolts M8 (100)", "Hydraulic Oil 5L",
        "Cable Ties (500)", "Work Boots", "Ear Defenders", "Pallet Jack Wheel", "LED Work Light"]),
    "Office": ((1, 150), (3, 15), [
        "A4 Paper Ream", "Ballpoint Pens (50)", "Stapler", "Sticky Notes", "Whiteboard Markers",
        "Desk Organizer", "Printer Toner", "File Folders (100)", "Desk Lamp", "Ergonomic Chair"]),
    "Grocery": ((1, 20), (8, 30), [
        "Bottled Water 24pk", "Coffee Beans 1kg", "Green Tea 100ct", "Granola Bars 12pk", "Rice 5kg",
        "Olive Oil 1L", "Pasta 1kg", "Canned Tomatoes", "Almonds 500g", "Oat Milk 1L"]),
}


def build(rng_seed: int = 42) -> dict:
    """Simulate 6 months of demand and replenishment so stock levels and history are consistent."""
    rng = random.Random(rng_seed)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    start = (now - timedelta(days=DAYS)).replace(hour=0, minute=0, second=0)

    products = [(cat, name, spec) for cat, (*spec, names) in CATALOG.items() for name in names]
    dead_skus = set(rng.sample(range(len(products)), 4))  # no sales for the last 90 days
    starved = set(rng.sample([(p, w["id"]) for p in range(len(products)) for w in WAREHOUSES], 8))

    data = {k: [] for k in ("skus", "sku_suppliers", "inventory", "stock_movements")}
    for idx, (category, name, ((cost_lo, cost_hi), (dem_lo, dem_hi))) in enumerate(products):
        sku_id = f"SKU-{1001 + idx}"
        cost = round(rng.uniform(cost_lo, cost_hi), 2)
        base = rng.uniform(dem_lo, dem_hi)
        trend = rng.uniform(-0.4, 0.6)  # demand change over the 6 months
        rop = int(base * 12) + 5
        rqty = max(20, int(round(base * 45, -1)))
        data["skus"].append({"id": sku_id, "name": name, "category": category, "unit_cost": cost,
                             "reorder_point": rop, "reorder_qty": rqty})

        for sup in rng.sample(SUPPLIERS, rng.choice([2, 3])):
            factor = rng.uniform(0.82, 0.92) if sup["id"] == 5 else rng.uniform(0.95, 1.2)
            data["sku_suppliers"].append({"sku_id": sku_id, "supplier_id": sup["id"],
                                          "unit_price": round(cost * factor, 2), "moq": rng.choice([10, 20, 50, 100])})

        for wh in WAREHOUSES:
            qty, pending, lead = rop + rqty, None, rng.choice([3, 5, 7])
            for day in range(DAYS):
                ts = start + timedelta(days=day, hours=rng.randint(7, 19), minutes=rng.randint(0, 59))
                arriving = pending[1] if pending and pending[0] == day else 0
                if idx in dead_skus and day == DAYS - 90:
                    arriving += 2 * rqty  # over-buy right before demand disappears -> dead stock
                if arriving:
                    qty += arriving
                    data["stock_movements"].append({"sku_id": sku_id, "warehouse_id": wh["id"],
                                                    "qty_delta": arriving, "type": "inbound", "ts": ts})
                if pending and pending[0] == day:
                    pending = None
                if idx in dead_skus and day >= DAYS - 90:
                    demand = 0
                else:
                    mean = base * WAREHOUSE_DEMAND[wh["id"]] * (1 + trend * day / DAYS) * (0.6 if ts.weekday() >= 5 else 1)
                    demand = max(0, round(rng.gauss(mean, mean * 0.35)))
                shipped = min(demand, qty)
                if shipped:
                    qty -= shipped
                    data["stock_movements"].append({"sku_id": sku_id, "warehouse_id": wh["id"],
                                                    "qty_delta": -shipped, "type": "outbound", "ts": ts})
                replenish = not ((idx, wh["id"]) in starved and day >= DAYS - 30)
                if qty <= rop and pending is None and replenish:
                    pending = (day + lead, rqty)
            data["inventory"].append({"warehouse_id": wh["id"], "sku_id": sku_id, "qty_on_hand": qty,
                                      "qty_reserved": int(qty * rng.uniform(0, 0.12)), "updated_at": now})
    return data


def seed(reset: bool = False) -> bool:
    """Create tables and load mock data if the DB is empty. Returns True when data was inserted."""
    if reset:
        metadata.drop_all(engine)
    metadata.create_all(engine)
    with engine.begin() as conn:
        if conn.execute(select(func.count()).select_from(skus)).scalar():
            return False
        data = build()
        conn.execute(warehouses.insert(), WAREHOUSES)
        conn.execute(suppliers.insert(), SUPPLIERS)
        conn.execute(skus.insert(), data["skus"])
        conn.execute(sku_suppliers.insert(), data["sku_suppliers"])
        conn.execute(inventory.insert(), data["inventory"])
        conn.execute(stock_movements.insert(), data["stock_movements"])
        # One open (submitted) PO so PO tracking has something to show.
        po_id = conn.execute(purchase_orders.insert().values(
            supplier_id=2, warehouse_id=1, status="submitted",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2),
        )).inserted_primary_key[0]
        offer = next(o for o in data["sku_suppliers"] if o["supplier_id"] == 2)
        conn.execute(po_lines.insert(), [{"po_id": po_id, "sku_id": offer["sku_id"],
                                          "qty": max(offer["moq"], 100), "unit_price": offer["unit_price"]}])
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the supply-chain database with mock data.")
    parser.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    args = parser.parse_args()
    inserted = seed(reset=args.reset)
    print("Seeded mock data." if inserted else "Database already has data (use --reset to rebuild).")
