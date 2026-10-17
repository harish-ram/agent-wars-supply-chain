"""Database schema and engine (SQLAlchemy Core).

Runs on a local SQLite file by default so the MVP needs zero setup.
Set DATABASE_URL to a Postgres URL (postgresql+psycopg://user:pass@host:5432/db) for production.
"""
import os
from pathlib import Path

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, MetaData, String, Table, create_engine

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "supply_chain.db"
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{_DEFAULT_DB.as_posix()}"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
metadata = MetaData()

warehouses = Table(
    "warehouses", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(100), nullable=False),
    Column("location", String(100), nullable=False),
    Column("capacity", Integer, nullable=False),
)

skus = Table(
    "skus", metadata,
    Column("id", String(20), primary_key=True),  # e.g. SKU-1001
    Column("name", String(120), nullable=False),
    Column("category", String(50), nullable=False),
    Column("unit_cost", Float, nullable=False),
    Column("reorder_point", Integer, nullable=False),
    Column("reorder_qty", Integer, nullable=False),
)

inventory = Table(
    "inventory", metadata,
    Column("warehouse_id", Integer, ForeignKey("warehouses.id"), primary_key=True),
    Column("sku_id", String(20), ForeignKey("skus.id"), primary_key=True),
    Column("qty_on_hand", Integer, nullable=False),
    Column("qty_reserved", Integer, nullable=False, default=0),
    Column("updated_at", DateTime, nullable=False),
)

suppliers = Table(
    "suppliers", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(100), nullable=False),
    Column("lead_time_days", Integer, nullable=False),
    Column("contact", String(120)),
)

sku_suppliers = Table(
    "sku_suppliers", metadata,
    Column("sku_id", String(20), ForeignKey("skus.id"), primary_key=True),
    Column("supplier_id", Integer, ForeignKey("suppliers.id"), primary_key=True),
    Column("unit_price", Float, nullable=False),
    Column("moq", Integer, nullable=False),
)

stock_movements = Table(
    "stock_movements", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sku_id", String(20), ForeignKey("skus.id"), nullable=False, index=True),
    Column("warehouse_id", Integer, ForeignKey("warehouses.id"), nullable=False),
    Column("qty_delta", Integer, nullable=False),  # + inbound, - outbound
    Column("type", String(20), nullable=False),  # inbound | outbound
    Column("ts", DateTime, nullable=False, index=True),
)

purchase_orders = Table(
    "purchase_orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("supplier_id", Integer, ForeignKey("suppliers.id"), nullable=False),
    Column("warehouse_id", Integer, ForeignKey("warehouses.id"), nullable=False),
    Column("status", String(20), nullable=False),  # draft | submitted
    Column("created_at", DateTime, nullable=False),
)

po_lines = Table(
    "po_lines", metadata,
    Column("po_id", Integer, ForeignKey("purchase_orders.id"), primary_key=True),
    Column("sku_id", String(20), ForeignKey("skus.id"), primary_key=True),
    Column("qty", Integer, nullable=False),
    Column("unit_price", Float, nullable=False),
)
