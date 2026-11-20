"""结算事实：把已构建的能量/空间事实金额化为账期账单。

分层：分摊量（fact_pool_allocation）、独立耗用（fact_tenant_direct）、
场地费基数（fact_reservation_day）都在入仓构建阶段算好；本模块只在关账时
按账期裁剪、套电价、做分位舍入、登记挂起金额。

口径要点（docs/资料口径.md）：
- open 暂估份额、容量冲突份额、负残差/覆盖不全池段只挂起，不交付结算；
- 结算舍入到分的尾差归该账单分摊基数最大项，并列按业务键升序；
- reservation 只形成场地费，不形成耗电；预留与实占不叠加场地费。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from .contract import wall_naive
from .timeutil import hours_between, overlap

DAY = timedelta(days=1)
EPS = 1e-9

# 挂起原因
HOLD_NEGATIVE = "negative_residual"
HOLD_COVERAGE = "coverage_incomplete"
HOLD_OPEN_OCCUPANCY = "open_occupancy_denominator"
HOLD_NO_OCCUPANCY = "no_occupancy_evidence"
HOLD_CAPACITY = "capacity_conflict"
HOLD_NO_TARIFF = "no_tariff"


class Settlement:
    def __init__(self, con: duckdb.DuckDBPyConnection, period_start, period_end,
                 created_at: datetime, note: str = ""):
        self.con = con
        self.created_at = wall_naive(created_at)
        self.note = note
        self.ps = datetime(period_start.year, period_start.month, period_start.day)
        self.pe = self.ps + timedelta(days=(period_end - period_start).days + 1)
        self.run_id = con.execute(
            "SELECT COALESCE(MAX(run_id), 0) + 1 FROM settlement_run"
        ).fetchone()[0]
        self.line_no = 0
        self.hold_id = 0
        self.tariffs = con.execute(
            "SELECT tariff_id, start_ts, end_ts, energy_price FROM raw_tariffs ORDER BY 2"
        ).fetchall()

    def _price_at(self, at):
        for _tid, ts, te, price in self.tariffs:
            if ts <= at < te:
                return float(price)
        return None

    def _hold(self, hold_type, tenant, zone, start, end, kwh, reason):
        self.hold_id += 1
        price = self._price_at(start)
        amount = round(kwh * price, 6) if price is not None else None
        self.con.execute(
            "INSERT INTO fact_hold VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [self.hold_id, self.run_id, hold_type, tenant, zone, start, end,
             round(kwh, 6), amount, reason],
        )

    # ------------------------------------------------- 公共池：已算分摊金额化
    def bill_public(self):
        # 池段挂起 / 无占用分母的待分，直接从池段事实按账期裁剪登记挂起
        segments = self.con.execute(
            "SELECT * FROM fact_pool_segment ORDER BY segment_id"
        ).fetchall()
        seg_cols = [d[0] for d in self.con.description]
        for raw in segments:
            seg = dict(zip(seg_cols, raw))
            hit = overlap(self.ps, self.pe, seg["start_ts"], seg["end_ts"])
            if not hit:
                continue
            w0, w1 = hit
            ratio = hours_between(w0, w1) / hours_between(seg["start_ts"], seg["end_ts"])
            if seg["pool_status"] == "held":
                pool = seg["pool_kwh"] * ratio
                reason = HOLD_NEGATIVE if pool < -EPS else HOLD_COVERAGE
                self._hold(reason, None, None, w0, w1, pool,
                           f"池段 {seg['segment_id']} {seg['coverage_status']}/"
                           f"{seg['pool_status']}：{seg['detail']}")
                continue
            # 有池量但无任何分摊（无占用分母）：整段待分
            allocated = self.con.execute(
                "SELECT COALESCE(SUM(share_kwh), 0) FROM fact_pool_allocation "
                "WHERE pool_segment_id = ?", [seg["segment_id"]]).fetchone()[0]
            if allocated <= EPS and seg["pool_kwh"] > EPS:
                self._hold(HOLD_NO_OCCUPANCY, None, None, w0, w1,
                           seg["pool_kwh"] * ratio,
                           f"池段 {seg['segment_id']} 期间内无可分摊占用分母")

        # 租户分摊行金额化
        allocs = self.con.execute(
            "SELECT a.*, s.start_ts AS seg_start, s.end_ts AS seg_end, "
            "s.public_evidence_kwh AS seg_evidence, s.pool_kwh AS seg_pool, "
            "s.unexplained_kwh AS seg_unexplained "
            "FROM fact_pool_allocation a JOIN fact_pool_segment s "
            "ON a.pool_segment_id = s.segment_id ORDER BY a.allocation_id"
        ).fetchall()
        cols = [d[0] for d in self.con.description]
        for raw in allocs:
            a = dict(zip(cols, raw))
            hit = overlap(self.ps, self.pe, a["seg_start"], a["seg_end"])
            if not hit:
                continue
            w0, w1 = hit
            ratio = hours_between(w0, w1) / hours_between(a["seg_start"], a["seg_end"])
            kwh = a["share_kwh"] * ratio
            ev_kwh = a["evidence_kwh"] * ratio
            un_kwh = a["residual_kwh"] * ratio
            price = self._price_at(w0)
            if price is None:
                self._hold(HOLD_NO_TARIFF, a["tenant"], a["zone"], w0, w1, kwh,
                           f"池段 {a['pool_segment_id']} 份额无适用电价")
                continue
            if a["allocation_status"] == "provisional":
                hold_type = (HOLD_CAPACITY if "容量冲突" in (a["detail"] or "")
                             and "open" not in (a["detail"] or "")
                             else HOLD_OPEN_OCCUPANCY)
                self._hold(hold_type, a["tenant"], a["zone"], w0, w1, kwh,
                           f"池段 {a['pool_segment_id']}：{a['detail']}")
            self.line_no += 1
            detail = ((a["detail"] + "；") if a["detail"] else "") + \
                f"公共负荷证据 {ev_kwh:.3f}kWh、未解释残差 {un_kwh:.3f}kWh"
            self.con.execute(
                "INSERT INTO fact_bill_line VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [self.run_id, self.line_no, a["tenant"], "energy_public",
                 f"POOL-{a['pool_segment_id']}", a["zone"], w0, w1,
                 round(kwh, 6), None, price, round(kwh * price, 6),
                 a["allocation_status"], detail],
            )

    # ------------------------------------------------------- 独立计量账单
    def bill_direct(self):
        rows = self.con.execute(
            "SELECT * FROM fact_tenant_direct ORDER BY direct_id"
        ).fetchall()
        cols = [d[0] for d in self.con.description]
        for row in rows:
            d = dict(zip(cols, row))
            hit = overlap(self.ps, self.pe, d["start_ts"], d["end_ts"])
            if not hit:
                continue
            w0, w1 = hit
            ratio = hours_between(w0, w1) / hours_between(d["start_ts"], d["end_ts"])
            kwh = d["kwh"] * ratio
            price = self._price_at(w0)
            status = "confirmed" if d["direct_status"] == "ok" else "provisional"
            if price is None:
                self._hold(HOLD_NO_TARIFF, d["tenant"], d["zone"], w0, w1, kwh,
                           f"独立分表 {d['meter_id']} 用量无适用电价")
                continue
            self.line_no += 1
            self.con.execute(
                "INSERT INTO fact_bill_line VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [self.run_id, self.line_no, d["tenant"], "energy_direct",
                 f"{d['relation_id']}:{d['meter_id']}", d["zone"], w0, w1,
                 round(kwh, 6), None, price, round(kwh * price, 6), status,
                 f"独立分表 {d['meter_id']} 已从公共池剔除" +
                 ("；含按比例拆分估计" if d["direct_status"] == "estimated" else "")],
            )

    # ------------------------------------------------------- 场地费
    def bill_space(self):
        rows = self.con.execute(
            "SELECT * FROM fact_reservation_day WHERE day >= ?::DATE AND day < ?::DATE "
            "ORDER BY reservation_id, day",
            [self.ps.date(), self.pe.date()],
        ).fetchall()
        cols = [d[0] for d in self.con.description]
        agg: dict[str, dict] = {}
        for row in rows:
            d = dict(zip(cols, row))
            b = agg.setdefault(d["reservation_id"], {
                "tenant": d["tenant"], "zone": d["zone"], "amount": 0.0,
                "volume_hours": 0.0, "price": float(d["space_price"]),
                "status": "confirmed", "days": set(),
            })
            b["amount"] += float(d["space_fee_yuan"])
            b["volume_hours"] += float(d["volume_m3"]) * float(d["hours"])
            b["days"].add(d["day"])
            if d["reservation_status"] == "held":
                b["status"] = "provisional"
        for rid, b in sorted(agg.items()):
            self.line_no += 1
            start = midnight_date(min(b["days"]))
            end = midnight_date(max(b["days"])) + DAY
            self.con.execute(
                "INSERT INTO fact_bill_line VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [self.run_id, self.line_no, b["tenant"], "space", rid, b["zone"],
                 start, end, None, round(b["volume_hours"], 6), b["price"],
                 round(b["amount"], 6), b["status"],
                 "合同预留场地费（按小时折算，不与实占叠加；预留不产生耗电）"],
            )

    # ------------------------------------------------------- 汇总结算
    def summarize(self):
        receipts = self.con.execute(
            "SELECT string_agg(CAST(receipt_id AS VARCHAR), ',' ORDER BY receipt_id) "
            "FROM (SELECT DISTINCT receipt_id FROM landing_receipt)"
        ).fetchone()[0]
        self.con.execute(
            "INSERT INTO settlement_run VALUES (?, ?, ?, ?, ?, ?)",
            [self.run_id, self.ps.date(), self.pe.date(), self.created_at,
             receipts or "", self.note],
        )
        tenants = [r[0] for r in self.con.execute(
            "SELECT DISTINCT tenant FROM fact_bill_line WHERE run_id = ? ORDER BY 1",
            [self.run_id],
        ).fetchall()]
        for tenant in tenants:
            lines = self.con.execute(
                "SELECT line_no, line_type, line_status, base_kwh, base_volume_hours, "
                "amount_yuan FROM fact_bill_line WHERE run_id = ? AND tenant = ? "
                "ORDER BY line_no",
                [self.run_id, tenant],
            ).fetchall()
            conf_kwh = prov_kwh = 0.0
            # 每个账单内：尾差归分摊基数最大项，并列按业务键升序；key 含 line_no 保证唯一
            conf_energy, held_energy, space = [], [], []
            for line_no, line_type, status, kwh, volume_hours, amount in lines:
                amount = float(amount)
                base = float(kwh) if line_type.startswith("energy") else float(volume_hours or 0)
                key = f"{tenant}|{line_no}|{line_type}"
                if line_type == "space":
                    space.append((key, base, amount))
                elif status == "confirmed":
                    conf_kwh += float(kwh or 0)
                    conf_energy.append((key, base, amount))
                else:
                    prov_kwh += float(kwh or 0)
                    held_energy.append((key, base, amount))
            conf_fen = round_to_fen(conf_energy)
            held_fen = round_to_fen(held_energy)
            space_fen = round_to_fen(space)
            self.con.execute(
                "INSERT INTO bill_summary VALUES (?, ?, ?, ?, ?, ?, ?)",
                [self.run_id, tenant, round(conf_kwh, 6), round(prov_kwh, 6),
                 conf_fen / 100, held_fen / 100, space_fen / 100],
            )


def midnight_date(day):
    return datetime(day.year, day.month, day.day)


def round_to_fen(items):
    """账单内分位舍入。

    items: [(业务键, 分摊基数(kWh 或体积小时), 原始金额元), ...]
    规则（docs/资料口径.md）：舍入到分的尾差归该账单分摊基数最大项，
    基数并列按业务键升序。返回舍入后的总金额（分，整数）。
    """
    if not items:
        return 0
    exact_fen = [(key, base, amount * 100.0) for key, base, amount in items]
    floors = [(key, base, int(fen)) for key, base, fen in exact_fen]
    target = int(round(sum(fen for _, _, fen in exact_fen)))
    remainder = target - sum(f for _, _, f in floors)
    ranked = sorted(floors, key=lambda x: (-x[1], x[0]))
    result = {key: f for key, _, f in floors}
    for i in range(abs(remainder)):
        key = ranked[i % len(ranked)][0]
        result[key] += 1 if remainder > 0 else -1
    return sum(result.values())


def run_settlement(con, period_start, period_end, created_at, note=""):
    settlement = Settlement(con, period_start, period_end, created_at, note)
    settlement.bill_public()
    settlement.bill_direct()
    settlement.bill_space()
    settlement.summarize()
    return settlement.run_id
