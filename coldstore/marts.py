"""自然日粒度集市。

口径：读数覆盖的是两个明确读数之间的累计增量，不是每分钟实测。
跨日或跨读数切片的日行一律标 estimated；与读数边界对齐的全日标 measured。
能量、公共池、空间三套日集市共用 +08:00 自然日。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from .timeutil import hours_between

DAY = timedelta(days=1)


def midnight(day) -> datetime:
    return datetime(day.year, day.month, day.day)


def _day_windows(start: datetime, end: datetime):
    cursor = start
    day = start.date()
    while cursor < end:
        nxt = midnight(day) + DAY
        seg_end = min(end, nxt)
        yield day, cursor, seg_end
        cursor = seg_end
        day = seg_end.date()


def build_marts(con: duckdb.DuckDBPyConnection):
    build_day_pool(con)
    build_day_energy(con)
    build_day_space(con)


def build_day_pool(con):
    """父表公共池按日展开；跨读数边界的日为 estimated。相邻池段在交界日聚合。"""
    segments = con.execute("SELECT * FROM fact_pool_segment ORDER BY segment_id").fetchall()
    cols = [d[0] for d in con.description]
    buckets: dict[tuple, dict] = {}
    for raw in segments:
        seg = dict(zip(cols, raw))
        for day, ds, de in _day_windows(seg["start_ts"], seg["end_ts"]):
            # 只有当日窗口恰好等于整段读数区间才可直接证明，否则都是按比例拆分估计
            aligned = ds == seg["start_ts"] and de == seg["end_ts"]
            factor = hours_between(ds, de) / hours_between(seg["start_ts"], seg["end_ts"])
            key = (day, seg["parent_meter"])
            b = buckets.setdefault(key, {
                "parent": 0.0, "indep": 0.0, "evidence": 0.0,
                "pool": 0.0, "unexplained": 0.0,
                "pool_status": seg["pool_status"], "measured": True,
            })
            b["parent"] += seg["parent_kwh"] * factor
            b["indep"] += seg["independent_child_kwh"] * factor
            b["evidence"] += seg["public_evidence_kwh"] * factor
            b["pool"] += seg["pool_kwh"] * factor
            b["unexplained"] += seg["unexplained_kwh"] * factor
            if not aligned:
                b["measured"] = False
            if seg["pool_status"] == "held":
                b["pool_status"] = "held"
    rows = []
    for (day, parent), b in sorted(buckets.items()):
        rows.append((day, parent, round(b["parent"], 6), round(b["indep"], 6),
                     round(b["evidence"], 6), round(b["pool"], 6),
                     round(b["unexplained"], 6), b["pool_status"],
                     "measured" if b["measured"] else "estimated"))
    con.executemany(
        "INSERT INTO mart_day_pool VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )


def build_day_energy(con):
    """租户级日能量：独立计量 + 公共池分摊，按日比例拆分并标记。"""
    # 独立分表
    direct = con.execute("SELECT * FROM fact_tenant_direct ORDER BY direct_id").fetchall()
    dcols = [d[0] for d in con.description]
    buckets: dict[tuple, dict] = {}

    def add(day, tenant, zone, **kw):
        key = (day, tenant, zone)
        b = buckets.setdefault(key, {
            "direct": 0.0, "public": 0.0, "evidence": 0.0, "unexplained": 0.0,
            "estimated": False, "details": set(),
        })
        for k, v in kw.items():
            if k == "detail":
                b["details"].add(v)
            else:
                b[k] += v

    for raw in direct:
        d = dict(zip(dcols, raw))
        for day, ds, de in _day_windows(d["start_ts"], d["end_ts"]):
            factor = hours_between(ds, de) / hours_between(d["start_ts"], d["end_ts"])
            add(day, d["tenant"], d["zone"],
                direct=d["kwh"] * factor,
                estimated=(ds, de) != (d["start_ts"], d["end_ts"]),
                detail="独立分表按日拆分")
    # 公共池分摊
    allocs = con.execute(
        "SELECT a.allocation_id, a.pool_segment_id AS segment_id, a.tenant, a.zone, "
        "a.weighted_volume_hours, a.share_kwh, a.evidence_kwh, a.residual_kwh, "
        "a.allocation_status, s.start_ts AS seg_start, s.end_ts AS seg_end "
        "FROM fact_pool_allocation a JOIN fact_pool_segment s "
        "ON a.pool_segment_id = s.segment_id ORDER BY allocation_id"
    ).fetchall()
    acols = [d[0] for d in con.description]
    for raw in allocs:
        a = dict(zip(acols, raw))
        for day, ds, de in _day_windows(a["seg_start"], a["seg_end"]):
            factor = hours_between(ds, de) / hours_between(a["seg_start"], a["seg_end"])
            add(day, a["tenant"], a["zone"],
                public=a["share_kwh"] * factor,
                evidence=a["evidence_kwh"] * factor,
                unexplained=a["residual_kwh"] * factor,
                estimated=True, detail=f"公共池分摊（{a['allocation_status']}）")
    rows = []
    for (day, tenant, zone), b in sorted(buckets.items()):
        status = "estimated" if b["estimated"] or b["public"] > 0 else "measured"
        rows.append((day, tenant, zone, round(b["direct"], 6), round(b["public"], 6),
                     round(b["evidence"], 6), round(b["unexplained"], 6), status,
                     "；".join(sorted(b["details"]))))
    con.executemany("INSERT INTO mart_day_energy VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def build_day_space(con):
    """租户×温区×日的占用、预留与容量对照。"""
    rows = con.execute("""
        WITH keys AS (
            SELECT day, tenant, zone FROM fact_occupancy_day
            UNION
            SELECT day, tenant, zone FROM fact_reservation_day
        ),
        occ AS (
            SELECT day, tenant, zone,
                   SUM(volume_m3) AS occupied_m3,
                   SUM(weighted_volume_hours) AS weighted_volume_hours
            FROM fact_occupancy_day GROUP BY 1, 2, 3
        ),
        res AS (
            SELECT day, tenant, zone, SUM(volume_m3) AS reserved_m3
            FROM fact_reservation_day GROUP BY 1, 2, 3
        )
        SELECT k.day, k.tenant, k.zone,
               COALESCE(occ.occupied_m3, 0),
               COALESCE(res.reserved_m3, 0),
               cap.capacity_m3,
               COALESCE(occ.weighted_volume_hours, 0),
               COALESCE(cap.occupancy_over_m3, 0),
               COALESCE(cap.reservation_over_m3, 0)
        FROM keys k
        LEFT JOIN occ ON occ.day = k.day AND occ.tenant = k.tenant AND occ.zone = k.zone
        LEFT JOIN res ON res.day = k.day AND res.tenant = k.tenant AND res.zone = k.zone
        LEFT JOIN fact_capacity_day cap ON cap.day = k.day AND cap.zone = k.zone
        ORDER BY 1, 2, 3
    """).fetchall()
    con.executemany("INSERT INTO mart_day_space VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
