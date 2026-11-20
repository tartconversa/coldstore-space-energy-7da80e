"""测试用合成资料构造：在临时目录生成符合契约的 CSV，不触碰真实 data/。"""
from __future__ import annotations

import csv
from pathlib import Path

HEADER = {
    "zones": ["zone_version", "zone", "start", "end", "capacity_m3", "weight"],
    "meters": ["meter_id", "modulus"],
    "topology": ["relation_id", "parent", "child", "zone", "tenant", "start", "end"],
    "readings": ["reading_id", "meter_id", "read_at", "kwh"],
    "replacements": ["replacement_id", "old_meter", "new_meter", "at", "old_end", "new_start"],
    "occupancy": ["occupancy_id", "cargo_id", "tenant", "zone", "start", "end",
                  "volume_m3", "evidence"],
    "reservations": ["reservation_id", "tenant", "zone", "start", "end",
                     "volume_m3", "space_price"],
    "tariffs": ["tariff_id", "start", "end", "energy_price"],
}


def write_dataset(folder: str | Path, tables: dict[str, list[dict]]) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with (folder / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=HEADER[name])
            writer.writeheader()
            for row in rows:
                full = {key: row.get(key, "") for key in HEADER[name]}
                writer.writerow(full)
    return folder


# 基础时刻
T0 = "2026-10-01T08:00:00+08:00"
T1 = "2026-10-16T08:00:00+08:00"
T2 = "2026-10-30T08:00:00+08:00"


def base_dataset() -> dict[str, list[dict]]:
    """与仓库 data/ 同构的最小双温区数据集。"""
    return {
        "zones": [
            {"zone_version": "Z0", "zone": "Z", "start": T0, "end": T1,
             "capacity_m3": 200, "weight": 1},
            {"zone_version": "Z1", "zone": "ZA", "start": T1, "end": T2,
             "capacity_m3": 100, "weight": 1},
            {"zone_version": "Z2", "zone": "ZB", "start": T1, "end": T2,
             "capacity_m3": 100, "weight": 1.5},
        ],
        "meters": [
            {"meter_id": "MAIN"}, {"meter_id": "OLD", "modulus": 10000},
            {"meter_id": "NEW"}, {"meter_id": "PUBLIC"},
        ],
        "topology": [
            {"relation_id": "T1", "parent": "MAIN", "child": "OLD", "zone": "Z",
             "tenant": "A", "start": T0, "end": T1},
            {"relation_id": "T2", "parent": "MAIN", "child": "NEW", "zone": "ZA",
             "tenant": "A", "start": T1, "end": T2},
            {"relation_id": "T3", "parent": "MAIN", "child": "PUBLIC", "zone": "ZB",
             "tenant": "", "start": T1, "end": T2},
        ],
        "readings": [
            {"reading_id": "M1", "meter_id": "MAIN", "read_at": T0, "kwh": 1000},
            {"reading_id": "M2", "meter_id": "MAIN", "read_at": T1, "kwh": 1300},
            {"reading_id": "M3", "meter_id": "MAIN", "read_at": T2, "kwh": 1600},
            {"reading_id": "O1", "meter_id": "OLD", "read_at": T0, "kwh": 9980},
            {"reading_id": "O2", "meter_id": "OLD", "read_at": "2026-10-10T08:00:00+08:00",
             "kwh": 20},
            {"reading_id": "O3", "meter_id": "OLD", "read_at": T1, "kwh": 60},
            {"reading_id": "N1", "meter_id": "NEW", "read_at": T1, "kwh": 5},
            {"reading_id": "N2", "meter_id": "NEW", "read_at": T2, "kwh": 105},
            {"reading_id": "P1", "meter_id": "PUBLIC", "read_at": T1, "kwh": 0},
            {"reading_id": "P2", "meter_id": "PUBLIC", "read_at": T2, "kwh": 20},
        ],
        "replacements": [
            {"replacement_id": "R1", "old_meter": "OLD", "new_meter": "NEW", "at": T1,
             "old_end": 60, "new_start": 5},
        ],
        "occupancy": [
            {"occupancy_id": "O1", "cargo_id": "C1", "tenant": "A", "zone": "Z",
             "start": T0, "end": T1, "volume_m3": 40, "evidence": "complete"},
            {"occupancy_id": "O2", "cargo_id": "C2", "tenant": "B", "zone": "Z",
             "start": T0, "end": T1, "volume_m3": 30, "evidence": "complete"},
            {"occupancy_id": "O3", "cargo_id": "C1", "tenant": "A", "zone": "ZA",
             "start": T1, "end": T2, "volume_m3": 40, "evidence": "complete"},
            {"occupancy_id": "O4", "cargo_id": "C2", "tenant": "B", "zone": "ZB",
             "start": T1, "end": "2026-10-22T08:00:00+08:00", "volume_m3": 30,
             "evidence": "complete"},
            {"occupancy_id": "O5", "cargo_id": "C3", "tenant": "C", "zone": "ZB",
             "start": T1, "end": T2, "volume_m3": 20, "evidence": "open"},
        ],
        "reservations": [
            {"reservation_id": "RS1", "tenant": "A", "zone": "Z", "start": T0, "end": T1,
             "volume_m3": 50, "space_price": 2},
            {"reservation_id": "RS3", "tenant": "A", "zone": "ZA", "start": T1, "end": T2,
             "volume_m3": 50, "space_price": 2},
            {"reservation_id": "RS4", "tenant": "B", "zone": "ZB", "start": T1, "end": T2,
             "volume_m3": 40, "space_price": 2},
            {"reservation_id": "RS5", "tenant": "C", "zone": "ZB", "start": T1, "end": T2,
             "volume_m3": 20, "space_price": 2},
        ],
        "tariffs": [
            {"tariff_id": "P1", "start": T0, "end": T2, "energy_price": 1.2},
        ],
    }
