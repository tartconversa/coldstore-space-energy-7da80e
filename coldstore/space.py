"""空间事实建模。

三类事实严格分开：
1. 货物实际占用（occupancy）：按货物在库期间计体积时长，移库/拆托换位只形成
   同一 cargo_id 的位置区间，不增加货量；open 行体积是暂估分母；
2. 合同预留（reservation）：只形成场地费，不形成耗电；预留与实占不叠加场地费；
3. 温区可用容量（zones）：按温区版本生效窗口，核对占用与预留是否超容。

临时改变温区时，容量、占用归属与能耗池共用同一时间轴核对。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from .timeutil import hours_between

DAY = timedelta(days=1)
MIDNIGHT = timedelta(0)


def midnight(day) -> datetime:
    return datetime(day.year, day.month, day.day)


class SpaceModel:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.issues: list[tuple] = []
        self.zones = con.execute(
            "SELECT zone, start_ts, end_ts, capacity_m3, weight FROM dim_zone ORDER BY 2"
        ).fetchall()
        self.occupancy = con.execute(
            "SELECT occupancy_id, cargo_id, tenant, zone, start_ts, end_ts, volume_m3, evidence "
            "FROM raw_occupancy ORDER BY cargo_id, start_ts"
        ).fetchall()
        self.reservations = con.execute(
            "SELECT reservation_id, tenant, zone, start_ts, end_ts, volume_m3, space_price "
            "FROM raw_reservations ORDER BY reservation_id"
        ).fetchall()

    def _issue(self, issue_type, severity, ref_table, ref_key, start, end, message):
        self.issues.append((issue_type, severity, ref_table, ref_key, start, end, message))

    def _zone_at(self, zone: str, at: datetime):
        for z, zs, ze, cap, weight in self.zones:
            if z == zone and zs <= at < ze:
                return cap, weight
        return None, None

    def _weight_at(self, zone: str, at: datetime) -> float:
        _, weight = self._zone_at(zone, at)
        return float(weight) if weight is not None else 1.0

    # ------------------------------------------------------------ 占用
    def build_occupancy(self):
        """切分占用区间（对齐温区版本边界），逐货物核对移库连续性。"""
        split_rows: list[tuple] = []
        by_cargo: dict[str, list] = {}
        interval_id = 0
        for occ_id, cargo, tenant, zone, start, end, volume, evidence in self.occupancy:
            by_cargo.setdefault(cargo, []).append((start, end, occ_id))
            cuts = {start, end}
            for z, zs, ze, _, _ in self.zones:
                if z == zone:
                    if start < zs < end:
                        cuts.add(zs)
                    if start < ze < end:
                        cuts.add(ze)
            for s, e in zip(sorted(cuts), sorted(cuts)[1:]):
                cap, _ = self._zone_at(zone, s)
                if cap is None:
                    self._issue("zone_undefined", "error", "occupancy", occ_id, s, e,
                                f"占用 {occ_id} 落在温区 {zone} 无容量定义的时段")
                interval_id += 1
                split_rows.append((interval_id, occ_id, cargo, tenant, zone, s, e,
                                   float(volume), evidence))
        # 同一货物的位置区间必须首尾相接；重叠会重复计体积，缺口是资料问题
        for cargo, spans in by_cargo.items():
            spans.sort()
            for (s0, e0, o0), (s1, e1, o1) in zip(spans, spans[1:]):
                if e0 != s1:
                    relation = "重叠" if e0 > s1 else "缺口"
                    self._issue("cargo_chain_gap", "warning", "occupancy",
                                f"{o0}/{o1}", min(e0, s1), max(e0, s1),
                                f"货物 {cargo} 移库区间存在{relation}：{o0}->{o1}")
        self.con.executemany(
            "INSERT INTO fact_occupancy_interval VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            split_rows,
        )
        self._build_occupancy_day(split_rows)

    def _iter_days(self, start: datetime, end: datetime):
        """按 +08:00 自然日切出 (day, seg_start, seg_end)。"""
        cursor = start
        day = start.date()
        while cursor < end:
            next_midnight = midnight(day) + DAY
            seg_end = min(end, next_midnight)
            yield day, cursor, seg_end
            cursor = seg_end
            day = seg_end.date()

    def _build_occupancy_day(self, split_rows):
        days: dict[tuple, dict] = {}
        for _, occ_id, cargo, tenant, zone, s, e, volume, evidence in split_rows:
            for day, ds, de in self._iter_days(s, e):
                key = (day, tenant, zone, cargo)
                hours = hours_between(ds, de)
                weight = self._weight_at(zone, ds)
                bucket = days.setdefault(key, {
                    "hours": 0.0, "volume_hours": 0.0, "weighted": 0.0,
                    "evidence": evidence, "volume": volume,
                })
                bucket["hours"] += hours
                bucket["volume_hours"] += volume * hours
                bucket["weighted"] += volume * hours * weight
                if evidence == "open":
                    bucket["evidence"] = "open"
        rows = []
        for (day, tenant, zone, cargo), b in sorted(days.items()):
            rows.append((day, tenant, zone, cargo, b["volume"], round(b["hours"], 6),
                         self._weight_at(zone, midnight(day)),
                         round(b["volume_hours"], 6), round(b["weighted"], 6),
                         b["evidence"]))
        self.con.executemany(
            "INSERT INTO fact_occupancy_day VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )

    # ------------------------------------------------------------ 预留与场地费
    def build_reservations(self):
        rows = []
        day_rows = []
        for rid, tenant, zone, start, end, volume, price in self.reservations:
            rows.append((rid, tenant, zone, start, end, float(volume), float(price)))
            # 场地日费按小时折算，一天 24 小时
            for day, ds, de in self._iter_days(start, end):
                hours = hours_between(ds, de)
                cap, _ = self._zone_at(zone, ds)
                status = "ok"
                if cap is not None and float(volume) > cap + 1e-9:
                    status = "held"
                    self._issue("reservation_over_capacity", "error", "reservations", rid,
                                ds, de,
                                f"预留 {rid} 体积 {volume}m3 超过温区 {zone} 容量 {cap}m3")
                fee = float(price) * float(volume) * hours / 24.0
                day_rows.append((day, rid, tenant, zone, float(volume), round(hours, 6),
                                 float(price), round(fee, 6), status))
        self.con.executemany(
            "INSERT INTO fact_reservation_interval VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        self.con.executemany(
            "INSERT INTO fact_reservation_day VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", day_rows
        )

    # ------------------------------------------------------------ 容量日
    def build_capacity(self):
        occ_intervals = self.con.execute(
            "SELECT zone, start_ts, end_ts, volume_m3 FROM fact_occupancy_interval"
        ).fetchall()
        res_intervals = [(z, s, e, v) for _, _, z, s, e, v, _ in self.reservations]
        occ_peak = self._daily_concurrent_peaks(occ_intervals)
        res_peak = self._daily_concurrent_peaks(res_intervals)

        days = set()
        for _, zs, ze, _, _ in self.zones:
            cursor = zs.date()
            last_day = (ze - timedelta(seconds=1)).date()
            while cursor <= last_day:
                days.add(cursor)
                cursor += timedelta(days=1)
        days |= {d for d, _ in occ_peak} | {d for d, _ in res_peak}

        rows = []
        for day in sorted(days):
            day_start, day_end = midnight(day), midnight(day) + DAY
            # 与该自然日有实际交集的温区版本（左闭右开）；样本中同日不重叠
            active = [(z, zs, ze, c, w) for z, zs, ze, c, w in self.zones
                      if max(zs, day_start) < min(ze, day_end)]
            for zone, zs, ze, cap, weight in sorted(active):
                occ = occ_peak.get((day, zone), 0.0)
                res = res_peak.get((day, zone), 0.0)
                occ_over = max(0.0, occ - cap) if cap is not None else 0.0
                res_over = max(0.0, res - cap) if cap is not None else 0.0
                if occ_over > 1e-9:
                    self._issue("occupancy_over_capacity", "error", "zones", zone,
                                day_start, day_end,
                                f"温区 {zone} {day} 同时占用峰值 {occ:.2f}m3 超容 {cap}m3")
                rows.append((day, zone, cap, weight, round(occ, 6), round(res, 6),
                             round(occ_over, 6), round(res_over, 6)))
        self.con.executemany(
            "INSERT INTO fact_capacity_day VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
        )

    def _daily_concurrent_peaks(self, intervals):
        """计算每个自然日每个温区的同时刻体积峰值（扫掠线，左闭右开）。"""
        by_day_zone: dict[tuple, list[tuple]] = {}
        for zone, s, e, volume in intervals:
            for day, ds, de in self._iter_days(s, e):
                by_day_zone.setdefault((day, zone), []).append((ds, de, float(volume)))
        peaks = {}
        for key, spans in by_day_zone.items():
            events = []
            for ds, de, v in spans:
                events.append((ds, v))
                events.append((de, -v))
            # 同时刻先处理结束（负）再处理开始（正），符合左闭右开
            events.sort(key=lambda x: (x[0], x[1]))
            current = peak = 0.0
            for _, delta in events:
                current += delta
                peak = max(peak, current)
            peaks[key] = peak
        return peaks

    def persist_issues(self, start_id: int = 1):
        for offset, issue in enumerate(self.issues):
            self.con.execute(
                "INSERT INTO quality_issue VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [start_id + offset, *issue],
            )
        return len(self.issues)


def build_space(con: duckdb.DuckDBPyConnection) -> SpaceModel:
    model = SpaceModel(con)
    model.build_occupancy()
    model.build_reservations()
    model.build_capacity()
    existing = con.execute("SELECT COUNT(*) FROM quality_issue").fetchone()[0]
    model.persist_issues(existing + 1)
    return model
