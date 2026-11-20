"""结算测试：分摊口径、待分保留、分位舍入与挂起。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from coldstore import warehouse
from tests.fixtures import base_dataset, write_dataset


class SettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "w.duckdb"
        self.data_dir = write_dataset(Path(self.tmp.name) / "data", base_dataset())
        self.con = warehouse.open_warehouse(self.db_path)
        warehouse.ingest_and_build(self.con, self.data_dir)
        self.run_id = warehouse.close_period(
            self.con, dt.date(2026, 10, 1), dt.date(2026, 10, 31))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_independent_tenant_excluded_from_pool(self):
        """A 有独立分表，只出现 energy_direct，不参与公共池分摊。"""
        a_public = self.con.execute(
            "SELECT COUNT(*) FROM fact_bill_line WHERE run_id=? AND tenant='A' "
            "AND line_type='energy_public'", [self.run_id]).fetchone()[0]
        self.assertEqual(a_public, 0)
        a_direct_kwh = self.con.execute(
            "SELECT SUM(base_kwh) FROM fact_bill_line WHERE run_id=? AND tenant='A' "
            "AND line_type='energy_direct'", [self.run_id]).fetchone()[0]
        self.assertAlmostEqual(a_direct_kwh, 180.0)  # OLD 80 + NEW 100

    def test_weighted_allocation(self):
        """ZB 段按 m3×小时×1.5 权重在 B/C 间分 200kWh；B 月中出库仍按在库时长承担。"""
        rows = {r[0]: r[1:] for r in self.con.execute(
            "SELECT tenant, share_kwh, allocation_status FROM fact_pool_allocation "
            "WHERE pool_segment_id=2 ORDER BY tenant").fetchall()}
        # B: 30*6*24*1.5=6480；C: 20*14*24*1.5=10080；分母 16560
        self.assertAlmostEqual(rows["B"][0], 200 * 6480 / 16560, places=4)
        self.assertAlmostEqual(rows["C"][0], 200 * 10080 / 16560, places=4)
        self.assertEqual(rows["B"][1], "confirmed")
        self.assertEqual(rows["C"][1], "provisional")

    def test_open_share_held_not_distributed_away(self):
        """C 的 open 份额进挂起台账，确认金额为零，不会被资料齐的租户分光。"""
        held = self.con.execute(
            "SELECT hold_type, kwh, amount_yuan FROM fact_hold "
            "WHERE run_id=? AND tenant='C'", [self.run_id]).fetchall()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0][0], "open_occupancy_denominator")
        summary = self.con.execute(
            "SELECT confirmed_amount_yuan, held_amount_yuan FROM bill_summary "
            "WHERE run_id=? AND tenant='C'", [self.run_id]).fetchone()
        self.assertEqual(float(summary[0]), 0.0)
        self.assertAlmostEqual(float(summary[1]), 146.09, places=2)

    def test_pool_energy_conservation(self):
        """池段能量守恒：确认分摊 + 挂起份额 = 池总量。"""
        allocated = self.con.execute(
            "SELECT COALESCE(SUM(share_kwh),0) FROM fact_pool_allocation "
            "WHERE pool_segment_id=2 AND allocation_status='confirmed'").fetchone()[0]
        held_kwh = self.con.execute(
            "SELECT COALESCE(SUM(kwh),0) FROM fact_hold WHERE run_id=? "
            "AND hold_type='open_occupancy_denominator'", [self.run_id]).fetchone()[0]
        self.assertAlmostEqual(allocated + held_kwh, 200.0, places=4)

    def test_negative_residual_pool_not_billed(self):
        """负残差池段整体挂起，不产生任何租户分摊行。"""
        data = base_dataset()
        for r in data["readings"]:
            if r["reading_id"] == "M3":
                r["kwh"] = 1350
        with tempfile.TemporaryDirectory() as folder:
            d = write_dataset(Path(folder) / "data", data)
            con = warehouse.open_warehouse(Path(folder) / "w.duckdb")
            warehouse.ingest_and_build(con, d)
            rid = warehouse.close_period(con, dt.date(2026, 10, 1), dt.date(2026, 10, 31))
            seg2_lines = con.execute(
                "SELECT COUNT(*) FROM fact_bill_line WHERE run_id=? "
                "AND ref_id='POOL-2'", [rid]).fetchone()[0]
            self.assertEqual(seg2_lines, 0)
            hold = con.execute(
                "SELECT hold_type, kwh FROM fact_hold WHERE run_id=? "
                "AND hold_type='negative_residual'", [rid]).fetchall()
            self.assertEqual(len(hold), 1)
            self.assertAlmostEqual(hold[0][1], -50.0)
            con.close()

    def test_fen_rounding_to_largest_base(self):
        """账单金额舍入到分：确认能量与场地费各自汇总等于精确金额的分位值。"""
        rows = self.con.execute(
            "SELECT tenant, confirmed_amount_yuan, held_amount_yuan, space_amount_yuan "
            "FROM bill_summary WHERE run_id=? ORDER BY tenant", [self.run_id]).fetchall()
        for tenant, confirmed, held, space in rows:
            exact_energy = self.con.execute(
                "SELECT COALESCE(SUM(amount_yuan),0) FROM fact_bill_line "
                "WHERE run_id=? AND tenant=? AND line_type LIKE 'energy%' "
                "AND line_status='confirmed'",
                [self.run_id, tenant]).fetchone()[0]
            exact_space = self.con.execute(
                "SELECT COALESCE(SUM(amount_yuan),0) FROM fact_bill_line "
                "WHERE run_id=? AND tenant=? AND line_type='space'",
                [self.run_id, tenant]).fetchone()[0]
            self.assertAlmostEqual(float(confirmed), round(float(exact_energy), 2), places=2)
            self.assertAlmostEqual(float(space), round(float(exact_space), 2), places=2)

    def test_space_and_energy_separate_in_summary(self):
        summary = {r[0]: r[1:] for r in self.con.execute(
            "SELECT tenant, confirmed_kwh, provisional_kwh, confirmed_amount_yuan, "
            "held_amount_yuan, space_amount_yuan FROM bill_summary WHERE run_id=? "
            "ORDER BY tenant", [self.run_id]).fetchall()}
        # 切片顺序：confirmed_kwh, provisional_kwh, confirmed_amount, held_amount, space
        # B 的确认能量 = 第一段 264 元 + 第二段 93.91 元；场地费独立列示
        self.assertAlmostEqual(float(summary["B"][2]), 264.0 + 93.91, places=2)
        self.assertGreater(float(summary["B"][4]), 0)
        # C 只有挂起能量与场地费，确认能量金额为零
        self.assertEqual(float(summary["C"][2]), 0.0)
        self.assertGreater(float(summary["C"][4]), 0.0)


if __name__ == "__main__":
    unittest.main()
