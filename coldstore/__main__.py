"""命令行入口。

示例：
  python3 -I -m coldstore ingest --data data --db warehouse.duckdb
  python3 -I -m coldstore rebuild --db warehouse.duckdb
  python3 -I -m coldstore issues --db warehouse.duckdb
  python3 -I -m coldstore close --db warehouse.duckdb --from 2026-10-01 --to 2026-10-31
  python3 -I -m coldstore bill --db warehouse.duckdb --run 1
  python3 -I -m coldstore lineage --db warehouse.duckdb --run 1 --line 3
  python3 -I -m coldstore query --db warehouse.duckdb --view day --day 2026-10-16
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import query, warehouse
from .contract import SH_TZ


def _default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(type(obj))


def emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_default))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="coldstore", description="共享冷库计量数据仓")
    parser.add_argument("--db", default="warehouse.duckdb", help="DuckDB 仓文件")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="接收资料目录并重算事实")
    p_ingest.add_argument("--data", default="data")
    p_ingest.add_argument("--received-at", default=None,
                          help="接收时刻 ISO，默认当前 +08:00")

    sub.add_parser("rebuild", help="从原始当前区全量重算事实与集市")
    sub.add_parser("issues", help="列出数据质量问题")
    sub.add_parser("pools", help="列出公共池分段与残差")
    sub.add_parser("runs", help="列出账期结算运行")

    p_close = sub.add_parser("close", help="关闭账期并生成结算")
    p_close.add_argument("--from", dest="period_from", required=True)
    p_close.add_argument("--to", dest="period_to", required=True)
    p_close.add_argument("--note", default="")

    p_bill = sub.add_parser("bill", help="查询账期账单")
    p_bill.add_argument("--run", type=int, required=True)
    p_bill.add_argument("--tenant", default=None)

    p_lineage = sub.add_parser("lineage", help="账单行逐层溯源")
    p_lineage.add_argument("--run", type=int, required=True)
    p_lineage.add_argument("--line", type=int, required=True)

    p_query = sub.add_parser("query", help="租户/温区/日/池查询")
    p_query.add_argument("--view", choices=["day", "tenant", "zone", "meter", "holds"],
                         required=True)
    p_query.add_argument("--day", default=None)
    p_query.add_argument("--tenant", default=None)
    p_query.add_argument("--zone", default=None)
    p_query.add_argument("--meter", default=None)

    args = parser.parse_args(argv)
    con = warehouse.open_warehouse(Path(args.db))

    if args.command == "ingest":
        received_at = (datetime.fromisoformat(args.received_at).astimezone(SH_TZ)
                       if args.received_at else None)
        receipts = warehouse.ingest_and_build(con, args.data, received_at)
        emit([vars(r) for r in receipts])
    elif args.command == "rebuild":
        warehouse.rebuild(con)
        emit({"status": "rebuilt"})
    elif args.command == "issues":
        emit(query.quality_issues(con))
    elif args.command == "pools":
        emit(query.pool_segments(con))
    elif args.command == "runs":
        emit(query.list_runs(con))
    elif args.command == "close":
        run_id = warehouse.close_period(
            con, date.fromisoformat(args.period_from),
            date.fromisoformat(args.period_to), note=args.note)
        emit({"run_id": run_id, **query.bill(con, run_id)})
    elif args.command == "bill":
        emit(query.bill(con, args.run, args.tenant))
    elif args.command == "lineage":
        emit(query.bill_lineage(con, args.run, args.line))
    elif args.command == "query":
        if args.view == "day":
            emit(query.day_overview(con, date.fromisoformat(args.day)))
        elif args.view == "tenant":
            emit(query.tenant_day_energy(con, args.tenant))
        elif args.view == "zone":
            emit(query.zone_day_space(con, args.zone))
        elif args.view == "meter":
            emit(query.readings_trace(con, args.meter))
        elif args.view == "holds":
            emit(query.holds(con))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
