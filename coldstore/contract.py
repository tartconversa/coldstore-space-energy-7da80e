"""来源资料字段契约。

与 docs/资料口径.md 对齐，独立于 data/catalog.json：
catalog 校验由 sourcepack 负责，本仓接收资料不依赖 catalog 在场。
空字符串按口径表示未知/不适用，解析为 None，绝不解释为零。
"""
from __future__ import annotations

import datetime as _dt
import math

SH_TZ = _dt.timezone(_dt.timedelta(hours=8))

# 类型解析
def _number(value: str):
    if value == "":
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("非有限数值")
    return number


def _dt_stamp(value: str):
    if value == "":
        return None
    stamp = _dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("时刻必须带时区")
    # 仓内统一存 +08:00 挂钟朴素时间（DuckDB TIMESTAMP），口径时刻不再带 tzinfo
    return stamp.astimezone(SH_TZ).replace(tzinfo=None)


def wall_naive(stamp: _dt.datetime) -> _dt.datetime:
    """aware 时刻转 +08:00 挂钟朴素时间；naive 原样返回。"""
    if stamp.tzinfo is None:
        return stamp
    return stamp.astimezone(SH_TZ).replace(tzinfo=None)


def _string(value: str):
    return value if value != "" else None


PARSERS = {
    "string": _string,
    "number": _number,
    "datetime": _dt_stamp,
}

# 每张来源表的列名与类型，顺序即 CSV 表头顺序
CONTRACT: dict[str, list[tuple[str, str]]] = {
    "zones": [
        ("zone_version", "string"),
        ("zone", "string"),
        ("start", "datetime"),
        ("end", "datetime"),
        ("capacity_m3", "number"),
        ("weight", "number"),
    ],
    "meters": [
        ("meter_id", "string"),
        ("modulus", "number"),
    ],
    "topology": [
        ("relation_id", "string"),
        ("parent", "string"),
        ("child", "string"),
        ("zone", "string"),
        ("tenant", "string"),
        ("start", "datetime"),
        ("end", "datetime"),
    ],
    "readings": [
        ("reading_id", "string"),
        ("meter_id", "string"),
        ("read_at", "datetime"),
        ("kwh", "number"),
    ],
    "replacements": [
        ("replacement_id", "string"),
        ("old_meter", "string"),
        ("new_meter", "string"),
        ("at", "datetime"),
        ("old_end", "number"),
        ("new_start", "number"),
    ],
    "occupancy": [
        ("occupancy_id", "string"),
        ("cargo_id", "string"),
        ("tenant", "string"),
        ("zone", "string"),
        ("start", "datetime"),
        ("end", "datetime"),
        ("volume_m3", "number"),
        ("evidence", "string"),
    ],
    "reservations": [
        ("reservation_id", "string"),
        ("tenant", "string"),
        ("zone", "string"),
        ("start", "datetime"),
        ("end", "datetime"),
        ("volume_m3", "number"),
        ("space_price", "number"),
    ],
    "tariffs": [
        ("tariff_id", "string"),
        ("start", "datetime"),
        ("end", "datetime"),
        ("energy_price", "number"),
    ],
}

# DuckDB 物理类型
DB_TYPES = {"string": "VARCHAR", "number": "DOUBLE", "datetime": "TIMESTAMPTZ"}

KEY_COLUMNS = {
    "zones": ["zone_version"],
    "meters": ["meter_id"],
    "topology": ["relation_id"],
    "readings": ["reading_id"],
    "replacements": ["replacement_id"],
    "occupancy": ["occupancy_id"],
    "reservations": ["reservation_id"],
    "tariffs": ["tariff_id"],
}


def parse_row(table: str, raw: dict[str, str]) -> dict[str, object]:
    """按契约把一行 CSV 字符串解析为带类型的字典。"""
    contract = CONTRACT[table]
    return {name: PARSERS[kind](raw[name]) for name, kind in contract}
