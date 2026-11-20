"""DuckDB 连接与仓表初始化。"""
from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DERIVED_TABLES = [
    "dim_meter", "dim_zone", "dim_tenant",
    "fact_meter_replacement", "fact_meter_interval", "fact_meter_chain_segment",
    "fact_topology_edge", "fact_pool_segment", "fact_pool_allocation",
    "fact_tenant_direct",
    "fact_occupancy_interval", "fact_occupancy_day",
    "fact_reservation_interval", "fact_reservation_day", "fact_capacity_day",
    "mart_day_energy", "mart_day_pool", "mart_day_space",
    "quality_issue",
]
# 结算层是持久事实：新一轮资料接收不自动覆盖已关账，需显式重算时单独清理
SETTLEMENT_TABLES = [
    "settlement_run", "fact_bill_line", "fact_hold", "bill_summary",
]

RAW_TABLES = [
    "raw_zones", "raw_meters", "raw_topology", "raw_readings", "raw_replacements",
    "raw_occupancy", "raw_reservations", "raw_tariffs",
]


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def reset_derived(con: duckdb.DuckDBPyConnection) -> None:
    """清空可重算的事实/集市/质量表，保留落地与原始当前区。"""
    for table in DERIVED_TABLES:
        con.execute(f"DELETE FROM {table}")


def reset_settlement(con: duckdb.DuckDBPyConnection) -> None:
    for table in SETTLEMENT_TABLES:
        con.execute(f"DELETE FROM {table}")


def reset_raw(con: duckdb.DuckDBPyConnection) -> None:
    for table in RAW_TABLES:
        con.execute(f"DELETE FROM {table}")
