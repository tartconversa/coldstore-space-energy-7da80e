"""面向使用者的查询：按租户、温区、自然日、账期核对原始计量、已确认耗用、
公共部分与未解释差额，并可追到所依据的读数、占用与账单行。"""
from __future__ import annotations

from datetime import datetime, timedelta



def _dicts(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _day_bounds(day):
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


# ------------------------------------------------------------ 原始计量溯源
def meter_intervals(con, meter_id=None, start=None, end=None):
    sql = "SELECT * FROM fact_meter_interval WHERE 1=1"
    params = []
    if meter_id:
        sql += " AND meter_id = ?"
        params.append(meter_id)
    if start:
        sql += " AND end_ts > ?"
        params.append(start)
    if end:
        sql += " AND start_ts < ?"
        params.append(end)
    sql += " ORDER BY meter_id, start_ts"
    return _dicts(con, sql, params)


def readings_trace(con, meter_id):
    """某只表的原始读数与换表底数（账单异议时的第一层证据）。"""
    return {
        "readings": _dicts(
            con, "SELECT * FROM raw_readings WHERE meter_id = ? ORDER BY read_at", [meter_id]),
        "replacements": _dicts(
            con, "SELECT * FROM fact_meter_replacement WHERE old_meter = ? OR new_meter = ?",
            [meter_id, meter_id]),
        "chain": _dicts(
            con, "SELECT * FROM fact_meter_chain_segment WHERE meter_id = ? ORDER BY start_ts",
            [meter_id]),
    }


def pool_segments(con, start=None, end=None):
    sql = "SELECT * FROM fact_pool_segment WHERE 1=1"
    params = []
    if start:
        sql += " AND end_ts > ?"
        params.append(start)
    if end:
        sql += " AND start_ts < ?"
        params.append(end)
    sql += " ORDER BY segment_id"
    return _dicts(con, sql, params)


def unexplained(con, start=None, end=None):
    """未解释差额（公共负荷证据之外的残差）与挂起池段。"""
    sql = ("SELECT segment_id, parent_meter, start_ts, end_ts, parent_kwh, "
           "independent_child_kwh, public_evidence_kwh, pool_kwh, unexplained_kwh, "
           "coverage_status, pool_status, detail FROM fact_pool_segment WHERE 1=1")
    params = []
    if start:
        sql += " AND end_ts > ?"
        params.append(start)
    if end:
        sql += " AND start_ts < ?"
        params.append(end)
    sql += " ORDER BY segment_id"
    return _dicts(con, sql, params)


# ------------------------------------------------------------ 租户/温区/日
def tenant_day_energy(con, tenant, start_day=None, end_day=None):
    sql = "SELECT * FROM mart_day_energy WHERE tenant = ?"
    params = [tenant]
    if start_day:
        sql += " AND day >= ?"
        params.append(start_day)
    if end_day:
        sql += " AND day <= ?"
        params.append(end_day)
    sql += " ORDER BY day, zone"
    return _dicts(con, sql, params)


def zone_day_space(con, zone, start_day=None, end_day=None):
    sql = "SELECT * FROM mart_day_space WHERE zone = ?"
    params = [zone]
    if start_day:
        sql += " AND day >= ?"
        params.append(start_day)
    if end_day:
        sql += " AND day <= ?"
        params.append(end_day)
    sql += " ORDER BY day, tenant"
    return _dicts(con, sql, params)


def day_overview(con, day):
    """某自然日的能量、公共池、空间全貌。"""
    return {
        "energy": _dicts(con, "SELECT * FROM mart_day_energy WHERE day = ? ORDER BY tenant, zone",
                         [day]),
        "pool": _dicts(con, "SELECT * FROM mart_day_pool WHERE day = ? ORDER BY parent_meter",
                       [day]),
        "space": _dicts(con, "SELECT * FROM mart_day_space WHERE day = ? ORDER BY tenant, zone",
                        [day]),
    }


# ------------------------------------------------------------ 账期与账单
def list_runs(con):
    return _dicts(con, "SELECT * FROM settlement_run ORDER BY run_id")


def bill(con, run_id, tenant=None):
    params = [run_id]
    sql = "SELECT * FROM fact_bill_line WHERE run_id = ?"
    if tenant:
        sql += " AND tenant = ?"
        params.append(tenant)
    sql += " ORDER BY line_no"
    lines = _dicts(con, sql, params)
    summary = _dicts(
        con, "SELECT * FROM bill_summary WHERE run_id = ?"
        + (" AND tenant = ?" if tenant else "") + " ORDER BY tenant",
        params if tenant else [run_id])
    holds = _dicts(
        con, "SELECT * FROM fact_hold WHERE run_id = ?"
        + (" AND (tenant = ? OR tenant IS NULL)" if tenant else "")
        + " ORDER BY hold_id",
        [run_id, tenant] if tenant else [run_id])
    return {"run_id": run_id, "lines": lines, "summary": summary, "holds": holds}


def bill_lineage(con, run_id, line_no):
    """账单行溯源：逐层追到池段、读数区间与占用区间。"""
    lines = _dicts(con, "SELECT * FROM fact_bill_line WHERE run_id = ? AND line_no = ?",
                   [run_id, line_no])
    if not lines:
        return {}
    line = lines[0]
    lineage = {"bill_line": line}
    if line["line_type"] == "energy_direct":
        relation_id, meter_id = line["ref_id"].split(":")
        lineage["topology"] = _dicts(
            con, "SELECT * FROM fact_topology_edge WHERE relation_id = ?", [relation_id])
        lineage["meter_trace"] = readings_trace(con, meter_id)
    elif line["line_type"] == "energy_public":
        seg_id = int(line["ref_id"].split("-")[1])
        lineage["pool_segment"] = _dicts(
            con, "SELECT * FROM fact_pool_segment WHERE segment_id = ?", [seg_id])
        lineage["allocation"] = _dicts(
            con, "SELECT * FROM fact_pool_allocation WHERE pool_segment_id = ? AND tenant = ?",
            [seg_id, line["tenant"]])
        lineage["occupancy"] = _dicts(
            con, "SELECT * FROM fact_occupancy_interval WHERE tenant = ? AND zone = ? "
                 "AND start_ts < ? AND end_ts > ? ORDER BY start_ts",
            [line["tenant"], line["zone"], line["end_ts"], line["start_ts"]])
        lineage["pool_meter_intervals"] = _dicts(
            con, "SELECT * FROM fact_meter_interval WHERE meter_id = "
                 "(SELECT parent_meter FROM fact_pool_segment WHERE segment_id = ?) "
                 "ORDER BY start_ts", [seg_id])
    elif line["line_type"] == "space":
        lineage["reservation"] = _dicts(
            con, "SELECT * FROM fact_reservation_interval WHERE reservation_id = ?",
            [line["ref_id"]])
        lineage["reservation_days"] = _dicts(
            con, "SELECT * FROM fact_reservation_day WHERE reservation_id = ? ORDER BY day",
            [line["ref_id"]])
    return lineage


def quality_issues(con, severity=None):
    sql = "SELECT * FROM quality_issue WHERE 1=1"
    params = []
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    sql += " ORDER BY issue_id"
    return _dicts(con, sql, params)


def holds(con, run_id=None):
    sql = "SELECT * FROM fact_hold WHERE 1=1"
    params = []
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " ORDER BY hold_id"
    return _dicts(con, sql, params)
