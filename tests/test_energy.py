"""能量事实测试：回绕、换表、缺测、负残差与公共池口径。"""
import tempfile
import unittest
from pathlib import Path

from coldstore import warehouse
from tests.fixtures import base_dataset, write_dataset


class EnergyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "w.duckdb"
        self.data_dir = write_dataset(Path(self.tmp.name) / "data", base_dataset())
        self.con = warehouse.open_warehouse(self.db_path)
        warehouse.ingest_and_build(self.con, self.data_dir)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def fetch(self, sql, params=None):
        return self.con.execute(sql, params or []).fetchall()

    def test_declared_rollover(self):
        """OLD 9980->20 按模数 10000 解释为 40kWh，不是负耗电。"""
        rows = self.fetch(
            "SELECT start_reading_id, end_reading_id, delta_kwh, interval_status "
            "FROM fact_meter_interval WHERE meter_id='OLD' ORDER BY start_ts")
        self.assertEqual(rows[0][2], 40.0)
        self.assertEqual(rows[0][3], "rollover")
        self.assertEqual(sum(r[2] for r in rows), 80.0)

    def test_undeclared_reverse_is_held_not_negative(self):
        """无声明倒退不得产生负耗电，区间挂起并登记质量问题。"""
        data = base_dataset()
        data["meters"] = [{"meter_id": "MAIN"}, {"meter_id": "OLD"}, {"meter_id": "NEW"}]
        data["readings"] = [r for r in data["readings"] if r["meter_id"] != "OLD"]
        data["readings"].append(
            {"reading_id": "O1", "meter_id": "OLD",
             "read_at": "2026-10-01T08:00:00+08:00", "kwh": 90})
        data["readings"].append(
            {"reading_id": "O2", "meter_id": "OLD",
             "read_at": "2026-10-10T08:00:00+08:00", "kwh": 40})
        data["replacements"] = []
        with tempfile.TemporaryDirectory() as folder:
            d = write_dataset(Path(folder) / "data", data)
            con = warehouse.open_warehouse(Path(folder) / "w.duckdb")
            warehouse.ingest_and_build(con, d)
            delta, status = con.execute(
                "SELECT delta_kwh, interval_status FROM fact_meter_interval "
                "WHERE meter_id='OLD'").fetchone()
            self.assertEqual(delta, 0.0)
            self.assertEqual(status, "held")
            issues = con.execute(
                "SELECT issue_type FROM quality_issue WHERE issue_type='reverse_undeclared'"
            ).fetchall()
            self.assertTrue(issues)
            con.close()

    def test_replacement_seam(self):
        """换表旧末读/新底数与读数吻合时缝口 ok。"""
        status = self.fetch(
            "SELECT seam_status FROM fact_meter_replacement WHERE replacement_id='R1'")[0][0]
        self.assertEqual(status, "ok")

    def test_replacement_mismatch_flagged(self):
        data = base_dataset()
        data["replacements"][0]["old_end"] = 999
        with tempfile.TemporaryDirectory() as folder:
            d = write_dataset(Path(folder) / "data", data)
            con = warehouse.open_warehouse(Path(folder) / "w.duckdb")
            warehouse.ingest_and_build(con, d)
            status, detail = con.execute(
                "SELECT seam_status, detail FROM fact_meter_replacement").fetchone()
            self.assertEqual(status, "held")
            self.assertIn("old_end", detail)
            con.close()

    def test_pool_excludes_independent_keeps_public(self):
        """独立子量先剔除；PUBLIC 证据留在池内，未解释残差如实保留。"""
        seg2 = self.fetch(
            "SELECT parent_kwh, independent_child_kwh, public_evidence_kwh, pool_kwh, "
            "unexplained_kwh FROM fact_pool_segment WHERE segment_id=2")[0]
        self.assertEqual(seg2, (300.0, 100.0, 20.0, 200.0, 180.0))

    def test_negative_residual_preserved(self):
        """子量大于父量时负残差保留，池挂起，不剪零。"""
        data = base_dataset()
        for r in data["readings"]:
            if r["reading_id"] == "M3":
                r["kwh"] = 1350  # 第二段 MAIN 仅 50，NEW 独立 100 -> 残差 -50
        with tempfile.TemporaryDirectory() as folder:
            d = write_dataset(Path(folder) / "data", data)
            con = warehouse.open_warehouse(Path(folder) / "w.duckdb")
            warehouse.ingest_and_build(con, d)
            pool, status = con.execute(
                "SELECT pool_kwh, pool_status FROM fact_pool_segment WHERE segment_id=2"
            ).fetchone()
            self.assertAlmostEqual(pool, -50.0)
            self.assertEqual(status, "held")
            neg = con.execute(
                "SELECT COUNT(*) FROM quality_issue WHERE issue_type='negative_residual'"
            ).fetchone()[0]
            self.assertEqual(neg, 1)
            con.close()

    def test_child_missing_is_held_not_zero(self):
        """子表整段缺测不等于零：池段挂起，不得按零扣减。"""
        data = base_dataset()
        data["readings"] = [r for r in data["readings"] if r["meter_id"] != "NEW"]
        with tempfile.TemporaryDirectory() as folder:
            d = write_dataset(Path(folder) / "data", data)
            con = warehouse.open_warehouse(Path(folder) / "w.duckdb")
            warehouse.ingest_and_build(con, d)
            status = con.execute(
                "SELECT pool_status FROM fact_pool_segment WHERE segment_id=2").fetchone()[0]
            self.assertEqual(status, "held")
            issues = con.execute(
                "SELECT issue_type FROM quality_issue WHERE issue_type='child_coverage_gap'"
            ).fetchall()
            self.assertTrue(issues)
            con.close()

    def test_half_month_submeter_not_fabricated(self):
        """子表只覆盖到半月（缺末读）：另半月不能假装已直接计量。

        NEW 仅有 10-16 底数、缺 10-30 末读时，第二段池整体挂起，
        不产生 NEW 的独立耗用行，也不按零从父量扣减。
        """
        data = base_dataset()
        data["readings"] = [r for r in data["readings"]
                            if r["reading_id"] != "N2"]
        with tempfile.TemporaryDirectory() as folder:
            d = write_dataset(Path(folder) / "data", data)
            con = warehouse.open_warehouse(Path(folder) / "w.duckdb")
            warehouse.ingest_and_build(con, d)
            status, coverage = con.execute(
                "SELECT pool_status, coverage_status FROM fact_pool_segment "
                "WHERE segment_id=2").fetchone()
            self.assertEqual((status, coverage), ("held", "held"))
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM fact_tenant_direct WHERE meter_id='NEW'"
            ).fetchone()[0], 0)
            gap = con.execute(
                "SELECT COUNT(*) FROM quality_issue "
                "WHERE issue_type='child_coverage_gap'").fetchone()[0]
            self.assertGreaterEqual(gap, 1)
            con.close()

    def test_no_double_count_after_zone_split(self):
        """T1 在拆区时刻终止：OLD 只出现在第一段，不会挂到新池段上重复扣减。"""
        edges = self.fetch(
            "SELECT child, start_ts, end_ts FROM fact_topology_edge WHERE child='OLD'")
        self.assertEqual(len(edges), 1)
        # 第一段独立量 80（OLD），第二段独立量 100（NEW），互不串段
        indep = self.fetch(
            "SELECT segment_id, independent_child_kwh FROM fact_pool_segment ORDER BY segment_id")
        self.assertEqual(indep, [(1, 80.0), (2, 100.0)])


if __name__ == "__main__":
    unittest.main()
