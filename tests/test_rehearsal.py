"""端到端测试：锚点更正覆盖预演（coverage rehearsal，v1.7）。

覆盖：
- 基本预演：同一当前快照内分别计算更正前（before）与假设更正后（after）
  的覆盖——after 仅把目标 (设备, 周期) 的锚点整体替换为拟用列表，
  其他周期与设备保持当前值；输出两侧覆盖结论、连续缺口，以及原先达标
  而预演后不达标的各个连续区间（regressions，相邻合并、记录预演后
  设备并集与峰值设备数）；
- 与覆盖审计同口径：同设备重叠（含相接）声段只算一台、不可定位声段不计
  （含更正后变为不可定位的声段）、边界相接不重叠；
- 拟用锚点列表：非空、按本地毫秒排序后严格递增校验（本地唯一、UTC 严格
  递增）；项内 cycle_id 与路径不一致即拒绝；
- 拒绝场景：指纹过期（409 fingerprint_stale）、周期已封存
  （409 cycle_sealed）、锚点不严格递增（400 anchors_not_increasing）、
  时间/区间/门槛非法（400）、设备或周期不存在（404）——均不改动数据；
- 预演只读：当前时间轴、周期内容指纹与已发布版本均不受影响；
- 并发：预演与其他设备写入/发布并发时，每次响应都是一致快照
  （before/after 片段均不重不漏地拼满预演时段）。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402
from app.logic import compute_coverage_regressions  # noqa: E402

MAX_INT64 = 2 ** 63 - 1


def request(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class CoverageRehearsalTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        db.init_db(self.db_path)
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    # ------------------------------------------------------------ 工具

    def make_device(self, device_id="D1", name=None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self.req("POST", "/devices", payload)

    def add_anchor(self, device_id, local_ms, utc_ms, cycle_id=None):
        payload = {"local_ms": local_ms, "utc_ms": utc_ms}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self.req("POST", f"/devices/{device_id}/anchors", payload)

    def submit(self, capture_id, device_id, start, end, cycle_id=None, note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": start, "local_end_ms": end}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    def fingerprint_of(self, device_id, cycle_id="default"):
        status, body = self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")
        self.assertEqual(status, 200)
        return body["cycle"]["fingerprint"]

    def rehearse(self, device_id, cycle_id, fingerprint, anchors,
                 start_ms, end_ms, min_devices):
        payload = {"fingerprint": fingerprint, "anchors": anchors,
                   "start_ms": start_ms, "end_ms": end_ms,
                   "min_devices": min_devices}
        return self.req(
            "POST", f"/devices/{device_id}/cycles/{cycle_id}/coverage-rehearsal",
            payload)

    def make_identity_device(self, device_id):
        """建一台恒等映射设备：锚点 (0,0)-(100000,100000)，本地毫秒即 UTC 毫秒。"""
        self.make_device(device_id)
        self.add_anchor(device_id, 0, 0)
        self.add_anchor(device_id, 100000, 100000)

    def assert_partition(self, result, start_ms, end_ms):
        """一侧（before/after）的片段必须不重不漏地恰好拼满预演时段。"""
        fragments = result["fragments"]
        self.assertTrue(fragments)
        self.assertEqual(fragments[0]["start_ms"], start_ms)
        self.assertEqual(fragments[-1]["end_ms"], end_ms)
        for prev, nxt in zip(fragments, fragments[1:]):
            self.assertEqual(prev["end_ms"], nxt["start_ms"])
            self.assertLess(prev["start_ms"], prev["end_ms"])
        self.assertEqual(result["covered_ms"] + result["gap_ms"],
                         end_ms - start_ms)
        gap_ms = sum(g["end_ms"] - g["start_ms"] for g in result["gaps"])
        self.assertEqual(result["gap_ms"], gap_ms)
        self.assertEqual(result["ok"], not result["gaps"])

    # ------------------------------------------------------------ 基本预演

    def test_basic_rehearsal_worsens(self):
        """两台设备全程共同覆盖；假设 D1 锚点整体平移 +20000 后其声段移出
        预演时段 -> after 全段只剩 D2，整段成为恶化区间。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D1", 0, 15000)
        self.submit("CAP-2", "D2", 0, 15000)
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 20000},
                     {"local_ms": 100000, "utc_ms": 120000}],
            start_ms=0, end_ms=15000, min_devices=2)
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], "D1")
        self.assertEqual(body["cycle_id"], "default")
        self.assertEqual(body["fingerprint"], fp)
        self.assertEqual(body["anchor_count"], 2)
        self.assertEqual(body["start_ms"], 0)
        self.assertEqual(body["end_ms"], 15000)
        self.assertEqual(body["min_devices"], 2)
        # 更正前：整段 D1+D2 达标
        self.assertTrue(body["before"]["ok"])
        self.assertEqual(body["before"]["fragments"], [
            {"start_ms": 0, "end_ms": 15000, "device_ids": ["D1", "D2"],
             "device_count": 2, "meets_threshold": True},
        ])
        self.assertEqual(body["before"]["gaps"], [])
        self.assertEqual(body["before"]["located_segment_count"], 2)
        # 假设更正后：D1 声段移到 [20000,35000)，时段内只剩 D2
        self.assertFalse(body["after"]["ok"])
        self.assertEqual(body["after"]["fragments"], [
            {"start_ms": 0, "end_ms": 15000, "device_ids": ["D2"],
             "device_count": 1, "meets_threshold": False},
        ])
        self.assertEqual(body["after"]["gaps"], [
            {"start_ms": 0, "end_ms": 15000, "device_ids": ["D2"],
             "max_device_count": 1},
        ])
        self.assertEqual(body["after"]["located_segment_count"], 1)
        # 恶化区间：整段原先达标、预演后不达标
        self.assertTrue(body["worsened"])
        self.assertEqual(body["regression_count"], 1)
        self.assertEqual(body["regressions"], [
            {"start_ms": 0, "end_ms": 15000, "after_device_ids": ["D2"],
             "after_max_device_count": 1},
        ])
        self.assert_partition(body["before"], 0, 15000)
        self.assert_partition(body["after"], 0, 15000)

    def test_identical_anchors_no_regression(self):
        """拟用锚点与当前完全一致：before 与 after 相同，无恶化区间。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 0},
                     {"local_ms": 100000, "utc_ms": 100000}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual(status, 200)
        self.assertEqual(body["before"], body["after"])
        self.assertTrue(body["after"]["ok"])
        self.assertFalse(body["worsened"])
        self.assertEqual(body["regression_count"], 0)
        self.assertEqual(body["regressions"], [])

    def test_rehearsal_improves_coverage(self):
        """拟用锚点把原本落在时段外的声段拉回时段内：覆盖改善，
        不存在恶化区间（regressions 只报告恶化，不报告改善）。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 50000)
        self.add_anchor("D1", 100000, 150000)
        self.submit("CAP-1", "D1", 0, 10000)  # 当前映射到 [50000,60000)
        self.make_identity_device("D2")
        self.submit("CAP-2", "D2", 0, 15000)
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 0},
                     {"local_ms": 100000, "utc_ms": 100000}],
            start_ms=0, end_ms=15000, min_devices=2)
        self.assertEqual(status, 200)
        # 更正前：整段只有 D2，不达标
        self.assertFalse(body["before"]["ok"])
        self.assertEqual(body["before"]["gap_ms"], 15000)
        # 假设更正后：CAP-1 映射到 [0,10000)，前段达标
        self.assertEqual(body["after"]["fragments"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": ["D1", "D2"],
             "device_count": 2, "meets_threshold": True},
            {"start_ms": 10000, "end_ms": 15000, "device_ids": ["D2"],
             "device_count": 1, "meets_threshold": False},
        ])
        self.assertEqual(body["after"]["gap_ms"], 5000)
        self.assertFalse(body["worsened"])
        self.assertEqual(body["regressions"], [])

    def test_unlocatable_after_shrinking_anchors(self):
        """拟用锚点覆盖范围缩小：声段更正后变为不可定位，不计入覆盖。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D2", 0, 10000)
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 0},
                     {"local_ms": 5000, "utc_ms": 5000}],
            start_ms=0, end_ms=10000, min_devices=2)
        self.assertEqual(status, 200)
        self.assertTrue(body["before"]["ok"])
        # CAP-1 终点 10000 超出拟用锚点覆盖 [0,5000] -> 不可定位 -> 不计
        self.assertEqual(body["after"]["located_segment_count"], 1)
        self.assertEqual(body["after"]["fragments"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": ["D2"],
             "device_count": 1, "meets_threshold": False},
        ])
        self.assertEqual(body["regressions"], [
            {"start_ms": 0, "end_ms": 10000, "after_device_ids": ["D2"],
             "after_max_device_count": 1},
        ])

    def test_other_cycles_and_devices_unchanged(self):
        """假设替换只作用于目标 (设备, 周期)：同设备其他周期与其他设备的
        映射保持当前值，before/after 中对应覆盖不变。"""
        self.make_identity_device("D1")
        self.submit("CAP-D", "D1", 0, 10000)  # default 周期 -> [0,10000)
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.add_anchor("D1", 0, 50000, cycle_id="boot-2")
        self.add_anchor("D1", 100000, 150000, cycle_id="boot-2")
        self.submit("CAP-B", "D1", 0, 10000, cycle_id="boot-2")  # -> [50000,60000)
        self.make_identity_device("D2")
        self.submit("CAP-2", "D2", 0, 10000)
        fp = self.fingerprint_of("D1", "boot-2")

        # 预演 boot-2 锚点平移到 [80000,180000)：CAP-B 移出 [0,60000)
        status, body = self.rehearse(
            "D1", "boot-2", fp,
            anchors=[{"local_ms": 0, "utc_ms": 80000},
                     {"local_ms": 100000, "utc_ms": 180000}],
            start_ms=0, end_ms=60000, min_devices=1)
        self.assertEqual(status, 200)
        self.assertEqual(body["cycle_id"], "boot-2")
        # before：default 周期声段与 D2 在 [0,10000) 的覆盖不受预演影响
        self.assertEqual(body["before"]["fragments"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": ["D1", "D2"],
             "device_count": 2, "meets_threshold": True},
            {"start_ms": 10000, "end_ms": 50000, "device_ids": [],
             "device_count": 0, "meets_threshold": False},
            {"start_ms": 50000, "end_ms": 60000, "device_ids": ["D1"],
             "device_count": 1, "meets_threshold": True},
        ])
        # after：仅 CAP-B 移出时段；[0,10000) 覆盖与 before 完全一致
        self.assertEqual(body["after"]["fragments"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": ["D1", "D2"],
             "device_count": 2, "meets_threshold": True},
            {"start_ms": 10000, "end_ms": 60000, "device_ids": [],
             "device_count": 0, "meets_threshold": False},
        ])
        self.assertEqual(body["regressions"], [
            {"start_ms": 50000, "end_ms": 60000, "after_device_ids": [],
             "after_max_device_count": 0},
        ])
        self.assert_partition(body["before"], 0, 60000)
        self.assert_partition(body["after"], 0, 60000)

    def test_same_device_overlap_counts_once(self):
        """同设备重叠声段在预演中仍只算一台。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D1", 5000, 15000)  # 与 CAP-1 重叠
        self.submit("CAP-3", "D2", 0, 15000)
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 20000},
                     {"local_ms": 100000, "utc_ms": 120000}],
            start_ms=0, end_ms=15000, min_devices=2)
        self.assertEqual(status, 200)
        # before：D1 两段重叠只算一台，与 D2 全程共同达标
        self.assertTrue(body["before"]["ok"])
        self.assertEqual(body["before"]["located_segment_count"], 3)
        # after：D1 两段都移出时段，只剩 D2
        self.assertEqual(body["after"]["located_segment_count"], 1)
        self.assertEqual(body["regressions"], [
            {"start_ms": 0, "end_ms": 15000, "after_device_ids": ["D2"],
             "after_max_device_count": 1},
        ])

    def test_regressions_multiple_intervals(self):
        """原先达标的两段被原本就不达标的片段隔开：恶化区间为两段，不合并。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.make_identity_device("D3")
        self.submit("CAP-1", "D1", 0, 30000)
        self.submit("CAP-2", "D2", 0, 10000)
        self.submit("CAP-3", "D3", 20000, 30000)
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 40000},
                     {"local_ms": 100000, "utc_ms": 140000}],
            start_ms=0, end_ms=30000, min_devices=2)
        self.assertEqual(status, 200)
        self.assertEqual([f["meets_threshold"] for f in body["before"]["fragments"]],
                         [True, False, True])
        self.assertEqual(body["regressions"], [
            {"start_ms": 0, "end_ms": 10000, "after_device_ids": ["D2"],
             "after_max_device_count": 1},
            {"start_ms": 20000, "end_ms": 30000, "after_device_ids": ["D3"],
             "after_max_device_count": 1},
        ])
        self.assertEqual(body["regression_count"], 2)
        self.assertTrue(body["worsened"])

    def test_regressions_merge_adjacent(self):
        """预演后相邻的多个不达标片段合并为一个连续恶化区间
        （记录预演后设备并集与峰值设备数）。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.make_identity_device("D3")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D2", 0, 5000)
        self.submit("CAP-3", "D3", 5000, 10000)
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 20000},
                     {"local_ms": 100000, "utc_ms": 120000}],
            start_ms=0, end_ms=10000, min_devices=2)
        self.assertEqual(status, 200)
        # after 被设备集合变化切成两段，但恶化区间相邻合并为一个
        self.assertEqual(len(body["after"]["fragments"]), 2)
        self.assertEqual(body["regressions"], [
            {"start_ms": 0, "end_ms": 10000,
             "after_device_ids": ["D2", "D3"], "after_max_device_count": 1},
        ])

    def test_linear_drift_and_rounding_in_rehearsal(self):
        """预演沿用现有线性漂移映射与毫秒四舍五入规则。"""
        self.make_device("D1")
        self.add_anchor("D1", 1000, 1700000000000)
        self.add_anchor("D1", 61000, 1700000060500)  # 速率 1.008333...
        self.submit("CAP-1", "D1", 5000, 20000)
        # 当前投影：[1700000004033, 1700000019158]
        _, seg = self.req("GET", "/segments/CAP-1")
        self.assertEqual(seg["segment"]["utc_start_ms"], 1700000004033)
        self.assertEqual(seg["segment"]["utc_end_ms"], 1700000019158)
        fp = self.fingerprint_of("D1")

        # 拟用锚点改为无漂移恒等偏移：local 1000->utc 1000, 61000->61000
        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 1000, "utc_ms": 1000},
                     {"local_ms": 61000, "utc_ms": 61000}],
            start_ms=5000, end_ms=20000, min_devices=1)
        self.assertEqual(status, 200)
        # after：CAP-1 映射到 [5000,20000)，整段达标
        self.assertTrue(body["after"]["ok"])
        self.assertEqual(body["after"]["covered_ms"], 15000)
        # before：该时段没有任何覆盖（声段在 1.7e12 附近）
        self.assertFalse(body["before"]["ok"])
        self.assertEqual(body["regressions"], [])

    def test_int64_boundary(self):
        """大数值时段可正常预演。"""
        self.make_device("D1")
        self.add_anchor("D1", MAX_INT64 - 2, MAX_INT64 - 2)
        self.add_anchor("D1", MAX_INT64 - 1, MAX_INT64 - 1)
        self.submit("CAP-E", "D1", MAX_INT64 - 2, MAX_INT64 - 1)
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": MAX_INT64 - 2, "utc_ms": MAX_INT64 - 3},
                     {"local_ms": MAX_INT64 - 1, "utc_ms": MAX_INT64 - 2}],
            start_ms=MAX_INT64 - 3, end_ms=MAX_INT64 - 1, min_devices=1)
        self.assertEqual(status, 200)
        # 原投影 [MAX-2,MAX-1)：只覆盖时段后段；预演后投影 [MAX-3,MAX-2)
        # 只覆盖时段前段 -> 后段成为恶化区间
        self.assertFalse(body["before"]["ok"])
        self.assertEqual(body["before"]["covered_ms"], 1)
        self.assertFalse(body["after"]["ok"])
        self.assertEqual(body["after"]["covered_ms"], 1)
        self.assertEqual(body["regressions"], [
            {"start_ms": MAX_INT64 - 2, "end_ms": MAX_INT64 - 1,
             "after_device_ids": [], "after_max_device_count": 0},
        ])

    # ------------------------------------------------------------ 拒绝场景

    def test_fingerprint_stale_rejected(self):
        """周期内容被晚到记录改动后，旧指纹预演 -> 409 fingerprint_stale。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        fp = self.fingerprint_of("D1")

        # 晚到记录改动周期内容（新增锚点）
        self.add_anchor("D1", 50000, 50000)
        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 0},
                     {"local_ms": 100000, "utc_ms": 100000}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "fingerprint_stale")
        self.assertEqual(body["error"]["details"]["current_fingerprint"],
                         self.fingerprint_of("D1"))

        # 新增声段同样使旧指纹过期
        fp2 = self.fingerprint_of("D1")
        self.submit("CAP-2", "D1", 1000, 2000)
        status, body = self.rehearse(
            "D1", "default", fp2,
            anchors=[{"local_ms": 0, "utc_ms": 0},
                     {"local_ms": 100000, "utc_ms": 100000}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual((status, body["error"]["code"]), (409, "fingerprint_stale"))

        # 重新获取指纹后预演通过
        status, body = self.rehearse(
            "D1", "default", self.fingerprint_of("D1"),
            anchors=[{"local_ms": 0, "utc_ms": 0},
                     {"local_ms": 100000, "utc_ms": 100000}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual(status, 200)

    def test_sealed_cycle_rejected(self):
        """周期已封存：锚点已冻结，预演 -> 409 cycle_sealed。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        fp = self.fingerprint_of("D1")
        status, _ = self.req("POST", "/devices/D1/cycles/default/seal",
                             {"fingerprint": fp})
        self.assertEqual(status, 201)

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 0},
                     {"local_ms": 100000, "utc_ms": 100000}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")

    def test_anchors_not_increasing_rejected(self):
        """拟用锚点本地时间重复或 UTC 不严格递增 -> 400。"""
        self.make_identity_device("D1")
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 1000, "utc_ms": 1000},
                     {"local_ms": 1000, "utc_ms": 2000}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual((status, body["error"]["code"]),
                         (400, "anchors_not_increasing"))

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 1000, "utc_ms": 5000},
                     {"local_ms": 2000, "utc_ms": 5000}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual((status, body["error"]["code"]),
                         (400, "anchors_not_increasing"))

        # 乱序提交合法锚点：按本地毫秒排序后校验，可正常预演
        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 100000, "utc_ms": 100000},
                     {"local_ms": 0, "utc_ms": 0}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual(status, 200)

    def test_invalid_inputs_rejected_and_data_unchanged(self):
        """非法输入与不存在对象：明确拒绝且不改动任何数据。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        fp = self.fingerprint_of("D1")
        good_anchors = [{"local_ms": 0, "utc_ms": 0},
                        {"local_ms": 100000, "utc_ms": 100000}]
        _, before_timeline = self.req("GET", "/timeline/current")
        _, before_versions = self.req("GET", "/versions")

        def payload(**kw):
            p = {"fingerprint": fp, "anchors": good_anchors,
                 "start_ms": 0, "end_ms": 10000, "min_devices": 1}
            p.update(kw)
            return p

        cases = [
            # 缺字段 / 指纹格式非法
            ({k: v for k, v in payload().items() if k != "anchors"},
             400, "validation_error"),
            ({k: v for k, v in payload().items() if k != "fingerprint"},
             400, "validation_error"),
            (payload(fingerprint="not-a-fingerprint"), 400, "validation_error"),
            (payload(fingerprint="A" * 64), 400, "validation_error"),
            # 锚点列表：非数组 / 空 / 项非法 / 项内周期与路径不一致
            (payload(anchors="x"), 400, "validation_error"),
            (payload(anchors=[]), 400, "validation_error"),
            (payload(anchors=[1]), 400, "validation_error"),
            (payload(anchors=[{"local_ms": 0}]), 400, "validation_error"),
            (payload(anchors=[{"local_ms": 0, "utc_ms": 0,
                               "cycle_id": "boot-2"}]), 400, "validation_error"),
            (payload(anchors=[{"local_ms": -1, "utc_ms": 0},
                              {"local_ms": 1, "utc_ms": 1}]),
             400, "invalid_time"),
            (payload(anchors=[{"local_ms": 0, "utc_ms": 0.5},
                              {"local_ms": 1, "utc_ms": 1}]),
             400, "invalid_time"),
            # 时间 / 区间 / 门槛
            (payload(start_ms=-1), 400, "invalid_time"),
            (payload(end_ms=MAX_INT64 + 1), 400, "invalid_time"),
            (payload(start_ms=0.5), 400, "invalid_time"),
            (payload(start_ms=True), 400, "invalid_time"),
            (payload(start_ms=10000, end_ms=10000), 400, "invalid_range"),
            (payload(start_ms=20000, end_ms=10000), 400, "invalid_range"),
            (payload(min_devices=0), 400, "invalid_threshold"),
            (payload(min_devices=-1), 400, "invalid_threshold"),
            (payload(min_devices=1.5), 400, "invalid_threshold"),
            (payload(min_devices="2"), 400, "invalid_threshold"),
            (payload(min_devices=True), 400, "invalid_threshold"),
        ]
        for body_in, want_status, want_code in cases:
            with self.subTest(body=body_in):
                status, body = self.req(
                    "POST", "/devices/D1/cycles/default/coverage-rehearsal",
                    body_in)
                self.assertEqual(status, want_status)
                self.assertEqual(body["error"]["code"], want_code)

        # 设备 / 周期不存在
        status, body = self.req(
            "POST", "/devices/NOPE/cycles/default/coverage-rehearsal",
            payload())
        self.assertEqual((status, body["error"]["code"]), (404, "device_not_found"))
        status, body = self.req(
            "POST", "/devices/D1/cycles/boot-x/coverage-rehearsal",
            payload())
        self.assertEqual((status, body["error"]["code"]), (404, "cycle_not_found"))

        # 请求体非对象 / 空请求体
        status, body = self.req(
            "POST", "/devices/D1/cycles/default/coverage-rehearsal", [1, 2, 3])
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))
        status, body = self.req(
            "POST", "/devices/D1/cycles/default/coverage-rehearsal")
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))

        # 全部拒绝后数据不变（当前时间轴、已发布版本、周期指纹）
        _, after_timeline = self.req("GET", "/timeline/current")
        _, after_versions = self.req("GET", "/versions")
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_timeline["devices"], before_timeline["devices"])
        self.assertEqual(after_versions, before_versions)
        self.assertEqual(self.fingerprint_of("D1"), fp)

    def test_item_cycle_id_matching_path_accepted(self):
        """锚点项内 cycle_id 与路径周期一致时接受。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        fp = self.fingerprint_of("D1")
        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 0, "cycle_id": "default"},
                     {"local_ms": 100000, "utc_ms": 100000}],
            start_ms=0, end_ms=10000, min_devices=1)
        self.assertEqual(status, 200)
        self.assertEqual(body["before"], body["after"])

    # ------------------------------------------------------------ 只读性

    def test_rehearsal_is_read_only(self):
        """合法预演不改动当前时间轴、周期内容指纹与已发布版本。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D2", 0, 10000)
        self.req("POST", "/versions", {"note": "v1"})
        _, v1_before = self.req("GET", "/versions/1")
        _, before_timeline = self.req("GET", "/timeline/current")
        fp = self.fingerprint_of("D1")

        status, body = self.rehearse(
            "D1", "default", fp,
            anchors=[{"local_ms": 0, "utc_ms": 20000},
                     {"local_ms": 100000, "utc_ms": 120000}],
            start_ms=0, end_ms=10000, min_devices=2)
        self.assertEqual(status, 200)
        self.assertTrue(body["worsened"])  # 预演结论本身生效

        # 当前时间轴与周期指纹不变；已发布版本逐字段不变
        _, after_timeline = self.req("GET", "/timeline/current")
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_timeline["devices"], before_timeline["devices"])
        self.assertEqual(after_timeline["segment_count"],
                         before_timeline["segment_count"])
        self.assertEqual(self.fingerprint_of("D1"), fp)
        _, v1_after = self.req("GET", "/versions/1")
        self.assertEqual(v1_after, v1_before)
        _, versions = self.req("GET", "/versions")
        self.assertEqual([v["version_no"] for v in versions["versions"]], [1])

        # 预演后发布新版本：反映的是未改动的当前状态（与 v1 无差异）
        status, v2 = self.req("POST", "/versions", {"note": "v2"})
        self.assertEqual(status, 201)
        self.assertEqual(v2["changes"]["added"], [])
        self.assertEqual(v2["changes"]["removed"], [])
        self.assertEqual(v2["changes"]["displaced"], [])

    # ------------------------------------------------------------ 并发一致性

    def test_concurrent_rehearsal_with_unrelated_writes(self):
        """预演与其他设备写入/发布并发：目标周期指纹不受影响，预演始终 200，
        且每次响应的 before/after 都是一致快照（片段恰好拼满预演时段）。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        for i in range(5):
            self.submit(f"CAP-A{i}", "D1", i * 2000, i * 2000 + 1500)
            self.submit(f"CAP-B{i}", "D2", i * 2000 + 500, i * 2000 + 2000)
        fp = self.fingerprint_of("D1")
        hypo = [{"local_ms": 0, "utc_ms": 250},
                {"local_ms": 100000, "utc_ms": 100250}]

        errors, rehearsals = [], []

        def correct_other():
            try:
                for k in range(1, 4):
                    self.req("PUT", "/devices/D2/anchors",
                             {"anchors": [{"local_ms": 0, "utc_ms": k * 10},
                                          {"local_ms": 100000,
                                           "utc_ms": 100000 + k * 10}]})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                for _ in range(3):
                    self.req("POST", "/versions")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def rehearse():
            try:
                for _ in range(3):
                    status, body = self.rehearse(
                        "D1", "default", fp, hypo, 0, 12000, 2)
                    if status != 200:
                        errors.append(("rehearsal_status", status, body))
                    else:
                        rehearsals.append(body)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=correct_other),
                   threading.Thread(target=publish),
                   threading.Thread(target=rehearse)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(rehearsals)
        for body in rehearsals:
            # 每次响应都是一致快照：before/after 片段均不重不漏拼满时段
            # （before/after 会随 D2 投影变化，但单次响应内部必然自洽）
            self.assert_partition(body["before"], 0, 12000)
            self.assert_partition(body["after"], 0, 12000)
        # 目标周期指纹未被并发写入影响：预演从未因指纹过期被拒绝
        self.assertEqual(self.fingerprint_of("D1"), fp)

    # ------------------------------------------------------------ 纯逻辑

    def test_compute_coverage_regressions_pure(self):
        """纯逻辑：原子区间对齐、相邻合并、设备并集与峰值统计。"""
        def frag(start, end, devices, meets):
            return {"start_ms": start, "end_ms": end,
                    "device_ids": devices, "device_count": len(devices),
                    "meets_threshold": meets}

        # before 两段达标；after 切成三段，前两段不达标 -> 合并为一个区间
        regs = compute_coverage_regressions(
            [frag(0, 10, ["D1", "D2"], True), frag(10, 20, ["D1", "D3"], True)],
            [frag(0, 5, ["D2"], False), frag(5, 15, [], False),
             frag(15, 20, ["D1", "D3"], True)])
        self.assertEqual(regs, [
            {"start_ms": 0, "end_ms": 15, "after_device_ids": ["D2"],
             "after_max_device_count": 1},
        ])
        # before 不达标的片段不产生恶化区间；after 达标的片段也不产生
        regs = compute_coverage_regressions(
            [frag(0, 10, ["D1"], False), frag(10, 20, ["D1", "D2"], True)],
            [frag(0, 10, [], False), frag(10, 20, ["D1", "D2"], True)])
        self.assertEqual(regs, [])
        # 空输入（防御）：无区间
        self.assertEqual(compute_coverage_regressions([], []), [])


if __name__ == "__main__":
    unittest.main()
