"""能量事实建模。

处理链：原始读数 -> 单表区间（回绕/倒退/缺测）-> 换表缝合的计量链
-> 物理接线拓扑 -> 总表分段 -> 独立子量剔除 -> 公共池与未解释残差。

口径要点（docs/资料口径.md）：
- 读数仅在 read_at 代表累计值，区间增量由有效相邻读数解释；
- 换表用 replacement 的旧末读/新底数，不把新表较小值当负耗电；
- modulus 是已声明模数回绕；无声明的倒退记待核，不产生负耗电；
- child 缺测不等于零；覆盖不齐保留待核量；
- PUBLIC 是公共负荷子表，其量仍在 MAIN 残量中，只作证据、不重复相加；
- 负残差保留，绝不剪零后再分给租户。
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb

from .timeutil import fraction_split, hours_between, merge_timeline, overlap

# 状态词表
OK = "ok"
ESTIMATED = "estimated"          # 按比例拆分的可解释估计，仍可对账
HELD = "held"                    # 暂不交付结算，保留待核
OPEN = "open"                    # 资料不闭合

ROLLOVER = "rollover"
REVERSE_UNDECLARED = "reverse_undeclared"
REPLACEMENT_GAP = "replacement_gap"
REPLACEMENT_MISMATCH = "replacement_mismatch"


@dataclass
class Interval:
    meter_id: str
    start_ts: object
    end_ts: object
    start_reading_id: str
    end_reading_id: str
    start_kwh: float
    end_kwh: float
    delta_kwh: float
    interval_status: str
    detail: str = ""


@dataclass
class ChainSegment:
    chain_key: str
    meter_id: str
    start_ts: object
    end_ts: object
    start_reading_id: str
    end_reading_id: str
    start_kwh: float
    end_kwh: float
    delta_kwh: float
    segment_status: str
    detail: str = ""


@dataclass
class QualityIssue:
    issue_type: str
    severity: str
    ref_table: str
    ref_key: str
    start_ts: object
    end_ts: object
    message: str


class EnergyModel:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.issues: list[QualityIssue] = []
        self.intervals: list[Interval] = []
        self.chain_segments: list[ChainSegment] = []
        # meter_id -> (modulus or None)
        self.modulus: dict[str, float | None] = {}
        # meter_id -> 排序后的读数 [(reading_id, read_at, kwh)]
        self.readings: dict[str, list[tuple]] = {}
        # meter_id -> 所属链 key；chain_key -> [meter_id,...]
        self.meter_chain: dict[str, str] = {}
        self.chain_meters: dict[str, list[str]] = {}
        # 拓扑边
        self.edges: list[dict] = []
        # 父表分段结果
        self.pool_segments: list[dict] = []
        self._load()

    # ------------------------------------------------------------------ 装载
    def _load(self):
        for meter_id, modulus in self.con.execute(
            "SELECT meter_id, modulus FROM raw_meters"
        ).fetchall():
            self.modulus[meter_id] = modulus
        for reading_id, meter_id, read_at, kwh in self.con.execute(
            "SELECT reading_id, meter_id, read_at, kwh FROM raw_readings ORDER BY read_at"
        ).fetchall():
            self.readings.setdefault(meter_id, []).append((reading_id, read_at, float(kwh)))
        for meter_id in self.readings:
            self.readings[meter_id].sort(key=lambda r: r[1])

    def _issue(self, issue_type, severity, ref_table, ref_key, start, end, message):
        self.issues.append(QualityIssue(issue_type, severity, ref_table, ref_key,
                                        start, end, message))

    # ------------------------------------------------------------ 单表区间
    def build_meter_intervals(self):
        for meter_id, points in self.readings.items():
            modulus = self.modulus.get(meter_id)
            for (rid0, t0, v0), (rid1, t1, v1) in zip(points, points[1:]):
                raw_delta = v1 - v0
                status, detail, delta = OK, "", raw_delta
                if raw_delta >= 0:
                    delta = raw_delta
                elif modulus is not None:
                    # 已声明模数回绕：v1 + 模数 - v0
                    adjusted = v1 + modulus - v0
                    if adjusted < 0:
                        status, detail, delta = HELD, "回绕后增量仍为负，待核", 0.0
                        self._issue(ROLLOVER, "error", "readings", f"{rid0}->{rid1}",
                                    t0, t1, f"表 {meter_id} 按模数 {modulus} 回绕后增量为负")
                    else:
                        status, detail, delta = ROLLOVER, f"声明模数回绕 +{modulus}", adjusted
                else:
                    # 无声明倒退：不产生负耗电，区间挂起待核，增量不确认
                    status, detail, delta = HELD, REVERSE_UNDECLARED, 0.0
                    self._issue(REVERSE_UNDECLARED, "error", "readings",
                                f"{rid0}->{rid1}", t0, t1,
                                f"表 {meter_id} 读数无声明倒退 {v0}->{v1}，增量待核")
                self.intervals.append(Interval(
                    meter_id, t0, t1, rid0, rid1, v0, v1, delta, status, detail,
                ))
        # 缺测：有表无任何读数，或读数稀疏导致的覆盖缺口在池分段处暴露；此处登记无读数表
        metered = set(self.readings)
        for meter_id in self.modulus:
            if meter_id not in metered:
                self._issue("missing_readings", "warning", "meters", meter_id,
                            None, None, f"表 {meter_id} 无任何读数，缺测不等于零")

    # ------------------------------------------------------------ 换表缝合
    def build_replacements(self):
        rows = self.con.execute(
            "SELECT replacement_id, old_meter, new_meter, at_ts, old_end, new_start "
            "FROM raw_replacements ORDER BY at_ts"
        ).fetchall()
        replacements = []
        for rid, old, new, at, old_end, new_start in rows:
            old_points = self.readings.get(old, [])
            new_points = self.readings.get(new, [])
            old_last = old_points[-1] if old_points else None
            new_first = new_points[0] if new_points else None
            status, detail = OK, ""
            old_ok = old_last is not None and old_last[1] == at and old_last[2] == float(old_end)
            new_ok = new_first is not None and new_first[1] == at and new_first[2] == float(new_start)
            if not old_ok or not new_ok:
                status = HELD
                problems = []
                if not old_ok:
                    problems.append(f"旧表末读与 old_end={old_end} 不符或时刻不一致")
                if not new_ok:
                    problems.append(f"新表首读与 new_start={new_start} 不符或时刻不一致")
                detail = "; ".join(problems)
                self._issue(REPLACEMENT_MISMATCH, "error", "replacements", rid,
                            at, at, f"换表 {rid} 缝口异常：{detail}")
            replacements.append(dict(replacement_id=rid, old_meter=old, new_meter=new,
                                     at_ts=at, old_end=float(old_end),
                                     new_start=float(new_start), seam_status=status,
                                     detail=detail))
            self.con.execute(
                "INSERT INTO fact_meter_replacement VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [rid, old, new, at, float(old_end), float(new_start), status, detail],
            )
        return replacements

    def build_chains(self):
        """以换表关系把物理表缝合为逻辑计量链。无换表的表各自成链。"""
        chain_of: dict[str, str] = {}
        successors: dict[str, str] = {}
        predecessors: dict[str, str] = {}
        for rep in self.con.execute(
            "SELECT old_meter, new_meter FROM raw_replacements"
        ).fetchall():
            old, new = rep
            successors[old] = new
            predecessors[new] = old

        for meter_id in self.modulus:
            if meter_id in predecessors:
                continue
            chain_key = meter_id  # 以链上第一只表命名
            cursor = meter_id
            ordered = []
            while cursor:
                chain_of[cursor] = chain_key
                ordered.append(cursor)
                cursor = successors.get(cursor)
            self.chain_meters[chain_key] = ordered
        self.meter_chain = chain_of

        for meter_id, points in self.readings.items():
            chain_key = chain_of.get(meter_id, meter_id)
            for itv in self.intervals:
                if itv.meter_id != meter_id:
                    continue
                self.chain_segments.append(ChainSegment(
                    chain_key, itv.meter_id, itv.start_ts, itv.end_ts,
                    itv.start_reading_id, itv.end_reading_id,
                    itv.start_kwh, itv.end_kwh, itv.delta_kwh,
                    itv.interval_status, itv.detail,
                ))

    # ------------------------------------------------------------ 拓扑
    def build_edges(self):
        rows = self.con.execute(
            "SELECT relation_id, parent, child, zone, tenant, start_ts, end_ts "
            "FROM raw_topology ORDER BY start_ts"
        ).fetchall()
        for relation_id, parent, child, zone, tenant, start, end in rows:
            edge = dict(relation_id=relation_id, parent=parent, child=child, zone=zone,
                        tenant=tenant, start_ts=start, end_ts=end)
            self.edges.append(edge)
            self.con.execute(
                "INSERT INTO fact_topology_edge VALUES (?, ?, ?, ?, ?, ?, ?)",
                [relation_id, parent, child, zone, tenant, start, end],
            )

    def _active_edges(self, parent: str, start, end):
        return [e for e in self.edges
                if e["parent"] == parent and overlap(start, end, e["start_ts"], e["end_ts"])]

    def _chain_energy(self, child: str, start, end):
        """取某只表所在计量链在窗口内的有效耗用。

        返回 (kwh, status, detail)。窗口与链上读数切片部分相交时按小时比例拆分并标记
        estimated；窗口落在读数覆盖之外则缺测，返回 None 量并标记 held。
        """
        chain_key = self.meter_chain.get(child, child)
        total, held_hours, covered_hours, estimated = 0.0, 0.0, 0.0, False
        window_h = hours_between(start, end)
        for seg in self.chain_segments:
            if seg.chain_key != chain_key:
                continue
            hit = overlap(start, end, seg.start_ts, seg.end_ts)
            if not hit:
                continue
            h0, h1 = hit
            if (h0, h1) != (seg.start_ts, seg.end_ts):
                estimated = True
            part = fraction_split(seg.delta_kwh, seg.start_ts, seg.end_ts, h0, h1)
            total += part
            covered_hours += hours_between(h0, h1)
            if seg.segment_status == HELD:
                held_hours += hours_between(h0, h1)
        if covered_hours + 1e-9 < window_h:
            return None, HELD, f"子表 {child} 在窗口内读数覆盖 {covered_hours:.2f}h/{window_h:.2f}h"
        status = HELD if held_hours > 0 else (ESTIMATED if estimated else OK)
        detail = "窗口与读数切片部分相交，按小时比例拆分" if estimated else ""
        return total, status, detail

    # ------------------------------------------------------------ 公共池分段
    def build_pool_segments(self):
        # 只对作为父表出现的表按物理接线建池分段
        parent_set = sorted({e["parent"] for e in self.edges})
        for parent in parent_set:
            parent_points = [r[1] for r in self.readings.get(parent, [])]
            edge_bounds = sorted({p for e in self.edges if e["parent"] == parent
                                  for p in (e["start_ts"], e["end_ts"])})
            timeline = merge_timeline(parent_points, edge_bounds)
            parent_intervals = [i for i in self.intervals if i.meter_id == parent]
            for start, end in zip(timeline, timeline[1:]):
                # 父表自身在该窗口的量（区间含 held 时同样保留）
                parent_kwh, parent_status = 0.0, OK
                covered = 0.0
                for itv in parent_intervals:
                    hit = overlap(start, end, itv.start_ts, itv.end_ts)
                    if not hit:
                        continue
                    part = fraction_split(itv.delta_kwh, itv.start_ts, itv.end_ts,
                                          hit[0], hit[1])
                    parent_kwh += part
                    covered += hours_between(*hit)
                    if itv.interval_status == HELD:
                        parent_status = HELD
                    if hit != (itv.start_ts, itv.end_ts):
                        parent_status = ESTIMATED if parent_status == OK else parent_status
                if covered + 1e-9 < hours_between(start, end):
                    parent_status = HELD
                    self._issue("parent_coverage_gap", "error", "meters", parent, start, end,
                                f"总表 {parent} 在 {start:%Y-%m-%d %H:%M}~{end:%H:%M} 读数覆盖不全")

                independent, public_evidence = 0.0, 0.0
                child_statuses, details = [], []
                active = self._active_edges(parent, start, end)
                for edge in active:
                    energy, status, detail = self._chain_energy(edge["child"], start, end)
                    if energy is None:
                        # 子表缺测不等于零：不能从父量中扣减，整段池保留待核
                        child_statuses.append(HELD)
                        details.append(detail)
                        self._issue("child_coverage_gap", "error", "topology",
                                    edge["relation_id"], start, end, detail)
                        continue
                    child_statuses.append(status)
                    if detail:
                        details.append(f"{edge['child']}: {detail}")
                    if edge["tenant"]:
                        independent += energy  # 独立租户分表：先剔除
                    else:
                        public_evidence += energy  # 公共负荷子表：只作证据，留在池内

                pool = parent_kwh - independent
                unexplained = pool - public_evidence
                coverage_status = HELD if HELD in child_statuses or parent_status == HELD else (
                    ESTIMATED if ESTIMATED in child_statuses or parent_status == ESTIMATED else OK)

                if pool < -1e-9:
                    pool_status = HELD
                    self._issue("negative_residual", "error", "meters", parent, start, end,
                                f"总表 {parent} 分段残差为负 {pool:.3f}kWh，保留不剪零")
                elif coverage_status == HELD:
                    pool_status = HELD
                else:
                    pool_status = OK

                seg = dict(
                    parent_meter=parent, start_ts=start, end_ts=end,
                    parent_kwh=round(parent_kwh, 6),
                    independent_child_kwh=round(independent, 6),
                    public_evidence_kwh=round(public_evidence, 6),
                    pool_kwh=round(pool, 6),
                    unexplained_kwh=round(unexplained, 6),
                    coverage_status=coverage_status,
                    pool_status=pool_status,
                    detail="; ".join(details),
                )
                self.pool_segments.append(seg)

    # ------------------------------------------------------------ 持久化
    def persist(self):
        for idx, itv in enumerate(self.intervals, 1):
            self.con.execute(
                "INSERT INTO fact_meter_interval VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [idx, itv.meter_id, itv.start_ts, itv.end_ts, itv.start_reading_id,
                 itv.end_reading_id, itv.start_kwh, itv.end_kwh, itv.delta_kwh,
                 itv.interval_status, itv.detail],
            )
        for idx, seg in enumerate(self.chain_segments, 1):
            self.con.execute(
                "INSERT INTO fact_meter_chain_segment VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [idx, seg.chain_key, seg.meter_id, seg.start_ts, seg.end_ts,
                 seg.start_reading_id, seg.end_reading_id, seg.start_kwh, seg.end_kwh,
                 seg.delta_kwh, seg.segment_status, seg.detail],
            )
        for idx, seg in enumerate(self.pool_segments, 1):
            seg["segment_id"] = idx
            self.con.execute(
                "INSERT INTO fact_pool_segment VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [idx, seg["parent_meter"], seg["start_ts"], seg["end_ts"],
                 seg["parent_kwh"], seg["independent_child_kwh"], seg["public_evidence_kwh"],
                 seg["pool_kwh"], seg["unexplained_kwh"], seg["coverage_status"],
                 seg["pool_status"], seg["detail"]],
            )
        # 独立租户已确认耗用
        direct_id = 0
        for edge in self.edges:
            if not edge["tenant"]:
                continue
            windows = [s for s in self.pool_segments
                       if s["parent_meter"] == edge["parent"]
                       and overlap(s["start_ts"], s["end_ts"],
                                   edge["start_ts"], edge["end_ts"])]
            for seg in windows:
                hit = overlap(seg["start_ts"], seg["end_ts"],
                              edge["start_ts"], edge["end_ts"])
                energy, status, detail = self._chain_energy(edge["child"], *hit)
                if energy is None:
                    continue
                direct_id += 1
                self.con.execute(
                    "INSERT INTO fact_tenant_direct VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [direct_id, edge["tenant"], edge["relation_id"], edge["child"],
                     edge["zone"], hit[0], hit[1], round(energy, 6), status, detail],
                )
        # 维度
        self.con.execute("""
            INSERT OR REPLACE INTO dim_meter
            SELECT m.meter_id, m.modulus,
                   CASE WHEN c.child IS NOT NULL THEN 'sub'
                        WHEN p.parent IS NOT NULL THEN 'parent'
                        ELSE 'standalone' END
            FROM raw_meters m
            LEFT JOIN (SELECT DISTINCT child FROM raw_topology) c
                   ON m.meter_id = c.child
            LEFT JOIN (SELECT DISTINCT parent FROM raw_topology) p
                   ON m.meter_id = p.parent
        """)
        self.con.execute(
            "INSERT INTO dim_zone SELECT zone, start_ts, end_ts, capacity_m3, weight FROM raw_zones"
        )
        tenants = {r[0] for r in self.con.execute(
            "SELECT DISTINCT tenant FROM raw_occupancy WHERE tenant IS NOT NULL"
        ).fetchall()}
        tenants |= {r[0] for r in self.con.execute(
            "SELECT DISTINCT tenant FROM raw_reservations WHERE tenant IS NOT NULL"
        ).fetchall()}
        tenants |= {r[0] for r in self.con.execute(
            "SELECT DISTINCT tenant FROM raw_topology WHERE tenant IS NOT NULL"
        ).fetchall()}
        if tenants:
            self.con.executemany("INSERT INTO dim_tenant VALUES (?)",
                                 [[t] for t in sorted(tenants)])
        for idx, issue in enumerate(self.issues, 1):
            self.con.execute(
                "INSERT INTO quality_issue VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [idx, issue.issue_type, issue.severity, issue.ref_table, issue.ref_key,
                 issue.start_ts, issue.end_ts, issue.message],
            )


def build_energy(con: duckdb.DuckDBPyConnection) -> EnergyModel:
    model = EnergyModel(con)
    model.build_meter_intervals()
    model.build_replacements()
    model.build_chains()
    model.build_edges()
    model.build_pool_segments()
    model.persist()
    return model
