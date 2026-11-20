"""空间事实测试：移库不增体积、open 证据、超容、三类事实分离。"""
import tempfile
import unittest
from pathlib import Path

from coldstore import warehouse
from tests.fixtures import base_dataset, write_dataset


class SpaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "w.duckdb"
        self.data_dir = write_dataset(Path(self.tmp.name) / "data", base_dataset())
        self.con = warehouse.open_warehouse(self.db_path)
        warehouse.ingest_and_build(self.con, self.data_dir)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_move_does_not_increase_volume(self):
        """C1 从 Z 移到 ZA 是同一货物两段位置区间，体积都是 40，总量不翻倍。"""
        rows = self.con.execute(
            "SELECT zone, volume_m3 FROM fact_occupancy_interval WHERE cargo_id='C1' "
            "ORDER BY start_ts").fetchall()
        self.assertEqual(rows, [("Z", 40.0), ("ZA", 40.0)])
        total = self.con.execute(
            "SELECT SUM(volume_m3 * DATE_DIFF('second', start_ts, end_ts)) / 3600.0 "
            "FROM fact_occupancy_interval WHERE cargo_id='C1'").fetchone()[0]
        # 两段：360h（首日 16h+14 整天）+ 336h（首日 16h+13 整天）= 696h，体积始终 40
        self.assertAlmostEqual(total, 40 * 696)

    def test_three_facts_separated(self):
        """占用、预留、容量是三套独立事实；预留不产生任何能量事实。"""
        # 预留只进入空间表
        n_res = self.con.execute("SELECT COUNT(*) FROM fact_reservation_interval").fetchone()[0]
        self.assertEqual(n_res, 4)
        # 能量表里没有以 reservation 为来源的行
        ref = self.con.execute(
            "SELECT COUNT(*) FROM fact_bill_line WHERE line_type='space'").fetchone()[0]
        # 尚未结算时为 0，结算后场地费行 base_kwh 必须为空
        warehouse.close_period(self.con, __import__("datetime").date(2026, 10, 1),
                               __import__("datetime").date(2026, 10, 31))
        space_lines = self.con.execute(
            "SELECT base_kwh, base_volume_hours FROM fact_bill_line WHERE line_type='space'"
        ).fetchall()
        self.assertTrue(space_lines)
        for kwh, vh in space_lines:
            self.assertIsNone(kwh)
            self.assertGreater(vh, 0)

    def test_open_evidence_propagates(self):
        """open 占用在日事实上保持 open 标记。"""
        ev = self.con.execute(
            "SELECT DISTINCT evidence FROM fact_occupancy_day WHERE tenant='C'").fetchall()
        self.assertEqual(ev, [("open",)])

    def test_over_capacity_flagged(self):
        """同时占用峰值超过温区容量时登记超容问题与容量日超容量。"""
        data = base_dataset()
        data["occupancy"].append(
            {"occupancy_id": "O9", "cargo_id": "C9", "tenant": "B", "zone": "ZB",
             "start": "2026-10-17T08:00:00+08:00", "end": "2026-10-20T08:00:00+08:00",
             "volume_m3": 90, "evidence": "complete"})
        with tempfile.TemporaryDirectory() as folder:
            d = write_dataset(Path(folder) / "data", data)
            con = warehouse.open_warehouse(Path(folder) / "w.duckdb")
            warehouse.ingest_and_build(con, d)
            over = con.execute(
                "SELECT MAX(occupancy_over_m3) FROM fact_capacity_day "
                "WHERE zone='ZB'").fetchone()[0]
            self.assertGreater(over, 0)
            issue = con.execute(
                "SELECT COUNT(*) FROM quality_issue "
                "WHERE issue_type='occupancy_over_capacity'").fetchone()[0]
            self.assertGreater(issue, 0)
            con.close()

    def test_capacity_split_zone_boundary(self):
        """拆区当天新旧温区容量各自正确，Z 在 10-16 后不再出现。"""
        zones_16 = {r[0] for r in self.con.execute(
            "SELECT zone FROM fact_capacity_day WHERE day=DATE '2026-10-16'").fetchall()}
        self.assertEqual(zones_16, {"Z", "ZA", "ZB"})
        zones_17 = {r[0] for r in self.con.execute(
            "SELECT zone FROM fact_capacity_day WHERE day=DATE '2026-10-17'").fetchall()}
        self.assertEqual(zones_17, {"ZA", "ZB"})

    def test_space_fee_hourly_proration(self):
        """场地费按小时折算：16 小时的首日 = 全日的 2/3。"""
        first = self.con.execute(
            "SELECT space_fee_yuan FROM fact_reservation_day "
            "WHERE reservation_id='RS5' ORDER BY day LIMIT 1").fetchone()[0]
        # RS5: 20m3 * 2 元/m3/天 * 16/24（仓内按 6 位小数存储）
        self.assertAlmostEqual(float(first), 20 * 2 * 16 / 24, places=4)


if __name__ == "__main__":
    unittest.main()
