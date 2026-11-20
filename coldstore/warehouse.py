"""入仓编排：接收资料 -> 原始当前区 -> 能量/空间事实 -> 集市，可全量重算。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from . import allocation, db, energy, ingest, marts, settle, space
from .contract import SH_TZ, wall_naive


def open_warehouse(path: str | Path) -> duckdb.DuckDBPyConnection:
    con = db.connect(path)
    db.init_schema(con)
    return con


def rebuild(con: duckdb.DuckDBPyConnection) -> None:
    """从原始当前区重算全部事实与集市（落地区与已关账不动）。

    顺序：能量（读数链/拓扑/公共池）-> 空间（占用/容量/预留）
    -> 公共池分摊（依赖占用与拓扑）-> 自然日集市。
    """
    db.reset_derived(con)
    energy.build_energy(con)
    space.build_space(con)
    allocation.build_allocations(con)
    marts.build_marts(con)


def ingest_and_build(con: duckdb.DuckDBPyConnection, data_dir: str | Path,
                     received_at: datetime | None = None,
                     do_rebuild: bool = True) -> list[ingest.Receipt]:
    """持续接收入口：新文件落地区追加、原始当前区刷新，然后重算事实。"""
    received_at = wall_naive(received_at or datetime.now(tz=SH_TZ))
    receipts = ingest.ingest_directory(con, data_dir, received_at)
    if do_rebuild:
        rebuild(con)
    return receipts


def close_period(con: duckdb.DuckDBPyConnection, period_start, period_end,
                 created_at: datetime | None = None, note: str = "") -> int:
    """形成一个账期结算运行；历史运行保留不覆盖。"""
    created_at = wall_naive(created_at or datetime.now(tz=SH_TZ))
    return settle.run_settlement(con, period_start, period_end, created_at, note)
