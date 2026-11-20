"""资料接收：落地区只追加、原始当前区按来源整表替换。

接收语义（docs/资料口径.md）：
- 同一来源记录键的新文件表示整行修订；原始文件与接收历史都保留；
- 相同文件摘要不增加事实（按 sha256 幂等）；
- 业务时点与接收时点分开记录；
- 空值表示未知/不适用，绝不当零。
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from .contract import CONTRACT, KEY_COLUMNS, parse_row

SOURCE_FILES = {name: f"{name}.csv" for name in CONTRACT}

# 原始当前区表的物理列（与 schema.sql 对齐）
RAW_COLUMNS = {
    "zones": ["zone_version", "zone", "start_ts", "end_ts", "capacity_m3", "weight"],
    "meters": ["meter_id", "modulus"],
    "topology": ["relation_id", "parent", "child", "zone", "tenant", "start_ts", "end_ts"],
    "readings": ["reading_id", "meter_id", "read_at", "kwh"],
    "replacements": ["replacement_id", "old_meter", "new_meter", "at_ts", "old_end", "new_start"],
    "occupancy": ["occupancy_id", "cargo_id", "tenant", "zone", "start_ts", "end_ts",
                  "volume_m3", "evidence"],
    "reservations": ["reservation_id", "tenant", "zone", "start_ts", "end_ts",
                     "volume_m3", "space_price"],
    "tariffs": ["tariff_id", "start_ts", "end_ts", "energy_price"],
}
# 契约列名 -> 原始当前区物理列名
PHYSICAL = {
    "start": "start_ts", "end": "end_ts", "read_at": "read_at", "at": "at_ts",
}


@dataclass
class Receipt:
    receipt_id: int
    source_table: str
    file_name: str
    file_sha256: str
    received_at: datetime
    row_count: int
    is_new: bool


def _next_receipt_id(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT COALESCE(MAX(receipt_id), 0) + 1 FROM landing_receipt").fetchone()
    return int(row[0])


def _read_contract_rows(path: Path, table: str) -> list[dict[str, object]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        expected = [name for name, _ in CONTRACT[table]]
        if reader.fieldnames != expected:
            raise ValueError(f"字段不匹配：{path.name}，应为 {expected}")
        rows: list[dict[str, object]] = []
        seen = set()
        for line, raw in enumerate(reader, 2):
            if None in raw or len(raw) != len(expected):
                raise ValueError(f"列数不匹配：{path.name}:{line}")
            parsed = parse_row(table, raw)
            key = tuple(parsed[k] for k in KEY_COLUMNS[table])
            if any(v is None or v == "" for v in key) or key in seen:
                raise ValueError(f"记录键缺失或重复：{path.name}:{line}")
            seen.add(key)
            rows.append(parsed)
        return rows


def _to_physical(table: str, row: dict[str, object]) -> tuple:
    out = []
    for column in RAW_COLUMNS[table]:
        logical = column
        if column.endswith("_ts"):
            logical = column[:-3]
        out.append(row.get(logical))
    return tuple(out)


def receive_file(con: duckdb.DuckDBPyConnection, table: str, path: str | Path,
                 received_at: datetime) -> Receipt:
    """接收单个来源文件；摘要已存在则只回传既有接收记录，不新增事实。"""
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = con.execute(
        "SELECT receipt_id, received_at, row_count FROM landing_receipt "
        "WHERE source_table = ? AND file_sha256 = ? ORDER BY receipt_id",
        [table, digest],
    ).fetchall()
    if existing:
        rid, at, count = existing[0]
        return Receipt(int(rid), table, path.name, digest, at, int(count), False)

    text = path.read_text(encoding="utf-8")
    rows = _read_contract_rows(path, table)
    receipt_id = _next_receipt_id(con)
    con.execute(
        "INSERT INTO landing_receipt VALUES (?, ?, ?, ?, ?, ?)",
        [receipt_id, table, path.name, digest, received_at, len(rows)],
    )
    payload_rows = [
        [receipt_id, table, idx, json.dumps(raw, ensure_ascii=False, default=str)]
        for idx, raw in enumerate(csv.DictReader(text.splitlines()), 1)
    ]
    if payload_rows:
        con.executemany(
            "INSERT INTO landing_raw_row VALUES (?, ?, ?, ?)", payload_rows
        )
    return Receipt(receipt_id, table, path.name, digest, received_at, len(rows), True)


def refresh_raw(con: duckdb.DuckDBPyConnection) -> None:
    """按每个来源最新一次接收，重写原始当前区（整表修订语义）。"""
    latest = con.execute(
        "SELECT source_table, MAX(receipt_id) FROM landing_receipt GROUP BY 1"
    ).fetchall()
    latest_by_table = dict(latest)
    from .db import reset_raw
    reset_raw(con)
    for table, receipt_id in latest_by_table.items():
        payloads = con.execute(
            "SELECT payload FROM landing_raw_row WHERE receipt_id = ? AND source_table = ? "
            "ORDER BY row_index",
            [receipt_id, table],
        ).fetchall()
        columns = RAW_COLUMNS[table]
        placeholders = ", ".join("?" for _ in columns)
        phys_rows = [_to_physical(table, parse_row(table, json.loads(p[0]))) for p in payloads]
        if phys_rows:
            con.executemany(
                f"INSERT INTO raw_{table} ({', '.join(columns)}) VALUES ({placeholders})",
                phys_rows,
            )


def ingest_directory(con: duckdb.DuckDBPyConnection, data_dir: str | Path,
                     received_at: datetime) -> list[Receipt]:
    """接收目录下全部契约内来源文件，随后刷新原始当前区。"""
    data_dir = Path(data_dir)
    receipts: list[Receipt] = []
    for table, file_name in SOURCE_FILES.items():
        path = data_dir / file_name
        if not path.is_file():
            continue
        receipts.append(receive_file(con, table, path, received_at))
    refresh_raw(con)
    return receipts
