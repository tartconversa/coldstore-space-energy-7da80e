"""公共池向租户的能量分摊（事实层，独立于账期关账）。

口径（docs/资料口径.md）：
- 独立分表租户的已计量部分不参与公共分配；
- 公共池按未独立计量租户的 m3×小时×温区权重 分担；
- open 行体积是暂估分母，其份额标 provisional 保留待分；
- 负残差、覆盖不全的池段整体挂起，不分给任何租户，不剪零；
- 没有占用分母的池段整段待分，不在资料齐的租户之间分光。

本模块只产出事实（fact_pool_allocation 与待核标记），不碰电价与金额；
金额化、挂起金额与账单由 settle 在关账时基于这些事实完成。
"""
from __future__ import annotations

import duckdb

from .timeutil import hours_between, overlap

EPS = 1e-9


class AllocationModel:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.alloc_id = 0
        # 独立计量租户（拓扑 tenant 非空）
        self.independent_tenants = {
            r[0] for r in con.execute(
                "SELECT DISTINCT tenant FROM fact_topology_edge WHERE tenant IS NOT NULL"
            ).fetchall()
        }
        self.zones = con.execute(
            "SELECT zone, start_ts, end_ts, weight FROM dim_zone ORDER BY 2"
        ).fetchall()
        # 占用区间（已按温区边界切分）
        self.occupancy = con.execute(
            "SELECT occupancy_id, cargo_id, tenant, zone, start_ts, end_ts, "
            "volume_m3, evidence FROM fact_occupancy_interval ORDER BY 1"
        ).fetchall()

    def _wvh_piecewise(self, zone, start, end, volume):
        total = 0.0
        cursor = start
        versions = [(zs, ze, float(w)) for z, zs, ze, w in self.zones
                    if z == zone and overlap(start, end, zs, ze)]
        for zs, ze, w in versions:
            h0 = max(cursor, zs)
            h1 = min(end, ze)
            if h0 < h1:
                total += volume * hours_between(h0, h1) * w
                cursor = h1
        if cursor < end:  # 无权重定义时段按 1.0 兜底
            total += volume * hours_between(cursor, end)
        return total

    def _weighted_volume_hours(self, start, end):
        """返回 [(tenant, zone, wvh, is_open, volume, hours)]，排除独立计量租户。"""
        agg: dict[tuple, dict] = {}
        for _occ_id, _cargo, tenant, zone, os_, oe, volume, evidence in self.occupancy:
            if tenant in self.independent_tenants:
                continue
            hit = overlap(start, end, os_, oe)
            if not hit:
                continue
            h0, h1 = hit
            hours = hours_between(h0, h1)
            wvh = self._wvh_piecewise(zone, h0, h1, float(volume))
            bucket = agg.setdefault(
                (tenant, zone),
                {"wvh": 0.0, "open": False, "volume": float(volume), "hours": 0.0})
            bucket["wvh"] += wvh
            bucket["hours"] += hours
            if evidence == "open":
                bucket["open"] = True
        return [(t, z, b["wvh"], b["open"], b["volume"], b["hours"])
                for (t, z), b in sorted(agg.items())]

    def _capacity_conflict_zones(self, start, end):
        rows = self.con.execute(
            "SELECT DISTINCT zone FROM fact_capacity_day "
            "WHERE day >= ?::DATE AND day < ?::DATE AND occupancy_over_m3 > 0",
            [start.date(), end.date()],
        ).fetchall()
        return {r[0] for r in rows}

    def build(self):
        """把可分摊池段写入 fact_pool_allocation；挂起池段/无分母段不产生分摊，
        由 settle 在关账时按事实表登记挂起金额。"""
        segments = self.con.execute(
            "SELECT * FROM fact_pool_segment ORDER BY segment_id"
        ).fetchall()
        cols = [d[0] for d in self.con.description]
        for row in segments:
            seg = dict(zip(cols, row))
            s0, s1 = seg["start_ts"], seg["end_ts"]
            contributions = self._weighted_volume_hours(s0, s1)
            denominator = sum(c[2] for c in contributions)
            conflict_zones = self._capacity_conflict_zones(s0, s1)

            # 负残差/覆盖不全的池段挂起；无占用分母的池段整段待分：都不写分摊
            if seg["pool_status"] == "held" or denominator <= EPS:
                continue

            open_present = any(c[3] for c in contributions)
            for tenant, zone, wvh, is_open, _volume, _hours in contributions:
                share = seg["pool_kwh"] * wvh / denominator
                ev_part = seg["public_evidence_kwh"] * wvh / denominator
                un_part = seg["unexplained_kwh"] * wvh / denominator
                status = "confirmed"
                details = []
                if is_open:
                    status = "provisional"
                    details.append("占用证据 open，暂估分母份额保留待分")
                if open_present and not is_open:
                    details.append("分母含 open 暂估行，open 份额已保留待分")
                if zone in conflict_zones:
                    status = "provisional"
                    details.append("温区存在容量冲突，份额挂起")
                self.alloc_id += 1
                self.con.execute(
                    "INSERT INTO fact_pool_allocation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [self.alloc_id, seg["segment_id"], tenant, zone, round(wvh, 6),
                     round(share, 6), round(ev_part, 6), round(un_part, 6),
                     status, "; ".join(details)],
                )


def build_allocations(con: duckdb.DuckDBPyConnection) -> None:
    AllocationModel(con).build()
