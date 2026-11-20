"""入仓与集市测试：幂等接收、整表修订、日粒度标记、溯源查询。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from coldstore import query, warehouse
from tests.fixtures import base_dataset, write_dataset


class IngestMartCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "w.duckdb"
        self.data_dir = write_dataset(Path(self.tmp.name) / "data", base_dataset())
        self.con = warehouse.open_warehouse(self.db_path)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_idempotent_receive(self):
        first = warehouse.ingest_and_build(self.con, self.data_dir)
        second = warehouse.ingest_and_build(self.con, self.data_dir)
        self.assertTrue(all(r.is_new for r in first))
        self.assertTrue(all(not r.is_new for r in second))
        # 落地历史仍只有每个来源一次接收
        count = self.con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT source_table, file_sha256 "
            "FROM landing_receipt)").fetchone()[0]
        self.assertEqual(count, 8)

    def test_row_revision_replaces_current_keeps_history(self):
        """同键新文件整行修订：原始当前区更新，落地历史保留两个版本。"""
        warehouse.ingest_and_build(self.con, self.data_dir)
        old_digest = self.con.execute(
            "SELECT file_sha256 FROM landing_receipt WHERE source_table='tariffs'").fetchone()[0]
        # 修订电价
        p = self.data_dir / "tariffs.csv"
        text = p.read_text(encoding="utf-8").replace("1.2", "1.5")
        p.write_text(text, encoding="utf-8")
        warehouse.ingest_and_build(self.con, self.data_dir)
        price = self.con.execute(
            "SELECT energy_price FROM raw_tariffs WHERE tariff_id='P1'").fetchone()[0]
        self.assertEqual(float(price), 1.5)
        history = self.con.execute(
            "SELECT COUNT(*) FROM landing_receipt WHERE source_table='tariffs'").fetchone()[0]
        self.assertEqual(history, 2)
        # 旧版本仍在落地原始行中可追溯
        digests = {r[0] for r in self.con.execute(
            "SELECT DISTINCT file_sha256 FROM landing_receipt "
            "WHERE source_table='tariffs'").fetchall()}
        self.assertIn(old_digest, digests)

    def test_day_mart_marks_estimated(self):
        """读数覆盖的是 15 天区间增量，日行必须标 estimated 而非伪装实测。"""
        warehouse.ingest_and_build(self.con, self.data_dir)
        statuses = {r[0] for r in self.con.execute(
            "SELECT DISTINCT energy_status FROM mart_day_pool").fetchall()}
        self.assertEqual(statuses, {"estimated"})
        # 日池按小时比例拆分，浮点尾差内加总等于池段总量
        daily = self.con.execute(
            "SELECT SUM(parent_kwh), SUM(pool_kwh), SUM(unexplained_kwh) "
            "FROM mart_day_pool").fetchone()
        self.assertAlmostEqual(daily[0], 600.0, places=4)   # MAIN 两段各 300
        self.assertAlmostEqual(daily[1], 420.0, places=4)   # 220 + 200
        self.assertAlmostEqual(daily[2], 400.0, places=4)   # 220 + 180

    def test_day_overview_and_lineage(self):
        warehouse.ingest_and_build(self.con, self.data_dir)
        rid = warehouse.close_period(self.con, dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        overview = query.day_overview(self.con, dt.date(2026, 10, 20))
        self.assertTrue(overview["energy"])
        self.assertTrue(overview["pool"])
        self.assertTrue(overview["space"])
        # 找一条公共池账单行做溯源
        line_no = self.con.execute(
            "SELECT line_no FROM fact_bill_line WHERE run_id=? AND line_type='energy_public' "
            "LIMIT 1", [rid]).fetchone()[0]
        lineage = query.bill_lineage(self.con, rid, line_no)
        self.assertIn("pool_segment", lineage)
        self.assertIn("allocation", lineage)
        self.assertIn("occupancy", lineage)
        self.assertIn("pool_meter_intervals", lineage)

    def test_quality_issues_queryable(self):
        warehouse.ingest_and_build(self.con, self.data_dir)
        # 基础干净数据无质量问题
        self.assertEqual(query.quality_issues(self.con), [])

    def test_runs_persist_across_rebuild(self):
        """关账是持久事实：重建能量/空间事实后已有关账仍可查询，新关账另开 run。"""
        warehouse.ingest_and_build(self.con, self.data_dir)
        rid1 = warehouse.close_period(self.con, dt.date(2026, 10, 1), dt.date(2026, 10, 15))
        warehouse.rebuild(self.con)
        kept = self.con.execute(
            "SELECT COUNT(*) FROM settlement_run WHERE run_id=?", [rid1]).fetchone()[0]
        self.assertEqual(kept, 1)
        rid2 = warehouse.close_period(self.con, dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        self.assertEqual(rid2, 2)
        # 两个账期的账单都在
        runs = {r[0] for r in self.con.execute(
            "SELECT DISTINCT run_id FROM fact_bill_line").fetchall()}
        self.assertEqual(runs, {1, 2})


if __name__ == "__main__":
    unittest.main()
