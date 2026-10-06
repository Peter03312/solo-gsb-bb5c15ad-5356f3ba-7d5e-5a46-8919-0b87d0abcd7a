"""端到端测试：现场时钟漂移审计（drift audit，v1.6）。

覆盖：
- 基本审计：锚点按本地时间相邻配对，逐段给出 UTC/本地速率与（有符号）漂移 ppm；
  返回首尾锚点、本地/UTC 区间跨度、整体最大绝对漂移与是否达标；
- 异常区间：|drift_ppm| > 阈值才标出（恰好相等不算），给出异常原因
  drift_exceeds_threshold、偏快/偏慢方向与超阈值幅度；阈值支持非负整数/浮点；
- 周期独立：审计只使用指定 (设备, 周期) 的锚点；
- 指定 version_no 只使用发布时冻结的锚点（后续锚点更正、新发布不改动历史结果）；
  旧版（format_rev=1）快照缺失的周期归属按 default 处理；
- 输入校验：锚点少于两个 -> 400 insufficient_anchors；阈值非法（负数/非数字/
  NaN/Infinity/布尔）-> 400 invalid_threshold；设备/周期/版本不存在 -> 404；
  全部明确拒绝且不改动数据；接口只读，封存周期同样可审计；
- 纯逻辑：输入乱序时按本地时间排序；阈值边界用精确分数比较，浮点不产生误判；
- 并发：审计与更正/发布并发时，每次响应都是一致快照（结果良构、跨度自洽）。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402
from app.logic import compute_drift_report  # noqa: E402

MAX_INT64 = 2 ** 63 - 1

# v1（无周期）建表脚本，仅用于构造待迁移的旧库
LEGACY_SCHEMA = """
CREATE TABLE devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE anchors (
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, local_ms)
);
CREATE TABLE segments (
    capture_id      TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE versions (
    version_no  INTEGER PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE version_anchors (
    version_no  INTEGER NOT NULL,
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    PRIMARY KEY (version_no, device_id, local_ms)
);
CREATE TABLE version_segments (
    version_no      INTEGER NOT NULL,
    capture_id      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    status          TEXT NOT NULL,
    utc_start_ms    INTEGER,
    utc_end_ms      INTEGER,
    created_at      TEXT,
    PRIMARY KEY (version_no, capture_id)
);
"""


def request(base, method, path, body=None, raw=False):
    if raw:
        data = body.encode("utf-8")
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class DriftAuditTestCase(unittest.TestCase):
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

    def req(self, method, path, body=None, raw=False):
        return request(self.base, method, path, body, raw=raw)

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

    def audit(self, device_id, cycle_id, max_drift_ppm, version_no=None):
        payload = {"max_drift_ppm": max_drift_ppm}
        if version_no is not None:
            payload["version_no"] = version_no
        return self.req(
            "POST", f"/devices/{device_id}/cycles/{cycle_id}/drift-audit", payload)

    def make_identity_device(self, device_id):
        """恒等映射设备：锚点 (0,0)-(100000,100000)，漂移为 0。"""
        self.make_device(device_id)
        self.add_anchor(device_id, 0, 0)
        self.add_anchor(device_id, 100000, 100000)

    def assert_well_formed(self, body, device_id, cycle_id, threshold, n_anchors):
        """响应良构：逐段跨度自洽、首尾锚点与总跨度一致、异常区间为逐段子集。"""
        self.assertEqual(body["device_id"], device_id)
        self.assertEqual(body["cycle_id"], cycle_id)
        self.assertEqual(body["max_drift_ppm"], float(threshold))
        self.assertEqual(body["anchor_count"], n_anchors)
        segs = body["segments"]
        self.assertEqual(len(segs), n_anchors - 1)
        self.assertEqual(body["first_anchor"], segs[0]["from_anchor"])
        self.assertEqual(body["last_anchor"], segs[-1]["to_anchor"])
        self.assertEqual(body["local_span_ms"],
                         sum(s["local_span_ms"] for s in segs))
        self.assertEqual(body["utc_span_ms"],
                         sum(s["utc_span_ms"] for s in segs))
        self.assertEqual(body["anomaly_count"], len(body["anomalies"]))
        self.assertEqual(body["ok"], body["anomaly_count"] == 0)
        flagged = [s["index"] for s in segs if s["exceeds_threshold"]]
        self.assertEqual([a["index"] for a in body["anomalies"]], flagged)
        for s in segs:
            self.assertGreater(s["local_span_ms"], 0)
            self.assertGreater(s["utc_span_ms"], 0)
            self.assertGreater(s["utc_local_rate"], 0)
            # rate 与 drift 口径一致：rate - 1 ≈ drift/1e6
            self.assertAlmostEqual((s["utc_local_rate"] - 1) * 1_000_000,
                                   s["drift_ppm"], delta=1.0)
        if segs:
            self.assertEqual(body["max_abs_drift_ppm"],
                             max(abs(s["drift_ppm"]) for s in segs))

    # ------------------------------------------------------------ 基本审计

    def test_basic_report_structure_and_values(self):
        """三个锚点配两段：速率/漂移/跨度/首尾锚点/整体结论正确。"""
        self.make_device("D1")
        self.add_anchor("D1", 1000, 1_700_000_000_000)
        self.add_anchor("D1", 61000, 1_700_000_060_500)   # dl=60000 du=60500
        self.add_anchor("D1", 121000, 1_700_000_120_000)  # dl=60000 du=59500

        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "current")
        self.assertIsNone(body["version_no"])
        self.assertEqual(body["first_anchor"],
                         {"local_ms": 1000, "utc_ms": 1_700_000_000_000})
        self.assertEqual(body["last_anchor"],
                         {"local_ms": 121000, "utc_ms": 1_700_000_120_000})
        self.assertEqual(body["local_span_ms"], 120000)
        self.assertEqual(body["utc_span_ms"], 120000)
        self.assertEqual(body["anchor_count"], 3)
        self.assertEqual(body["max_abs_drift_ppm"], 8333.333)
        self.assertFalse(body["ok"])

        s0, s1 = body["segments"]
        self.assertEqual(s0["local_span_ms"], 60000)
        self.assertEqual(s0["utc_span_ms"], 60500)
        self.assertEqual(s0["utc_local_rate"], 1.008333333)
        self.assertEqual(s0["drift_ppm"], 8333.333)
        self.assertTrue(s0["exceeds_threshold"])
        self.assertEqual(s1["local_span_ms"], 60000)
        self.assertEqual(s1["utc_span_ms"], 59500)
        self.assertEqual(s1["utc_local_rate"], 0.991666667)
        self.assertEqual(s1["drift_ppm"], -8333.333)
        self.assertTrue(s1["exceeds_threshold"])
        self.assert_well_formed(body, "D1", "default", 100, 3)

    def test_readme_example_pair(self):
        """README 示例锚点：漂移 8333.333 ppm（正，本地时钟偏慢）。"""
        self.make_device("D1")
        self.add_anchor("D1", 1000, 1700000000000)
        self.add_anchor("D1", 61000, 1700000060500)

        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        self.assertEqual(body["local_span_ms"], 60000)
        self.assertEqual(body["utc_span_ms"], 60500)
        self.assertEqual(body["segments"][0]["utc_local_rate"], 1.008333333)
        self.assertEqual(body["segments"][0]["drift_ppm"], 8333.333)
        self.assertEqual(body["anomaly_count"], 1)
        a = body["anomalies"][0]
        self.assertEqual(a["index"], 0)
        self.assertEqual(a["reason"], "drift_exceeds_threshold")
        self.assertEqual(a["direction"], "local_clock_slow")
        self.assertEqual(a["threshold_ppm"], 100.0)
        self.assertEqual(a["excess_ppm"], 8233.333)

    def test_identity_device_zero_drift_ok(self):
        """恒等锚点漂移为 0：阈值 0 也达标，ok=true。"""
        self.make_identity_device("D1")
        status, body = self.audit("D1", "default", 0)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["segments"][0]["utc_local_rate"], 1.0)
        self.assertEqual(body["segments"][0]["drift_ppm"], 0.0)
        self.assertFalse(body["segments"][0]["exceeds_threshold"])
        self.assertEqual(body["anomalies"], [])
        self.assertEqual(body["max_abs_drift_ppm"], 0.0)

    def test_under_threshold_not_flagged(self):
        """|漂移| 小于阈值：不标异常。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 1_000_000, 1_000_050)  # +50 ppm
        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["segments"][0]["drift_ppm"], 50.0)
        self.assertFalse(body["segments"][0]["exceeds_threshold"])

    def test_threshold_boundary_exact_equal_not_anomaly(self):
        """漂移恰好等于阈值（含整数/浮点阈值）不算异常，超出 0.001 才算。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 1000, 1001)  # 恰好 1000 ppm

        status, body = self.audit("D1", "default", 1000)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["segments"][0]["drift_ppm"], 1000.0)

        status, body = self.audit("D1", "default", 1000.0)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, body = self.audit("D1", "default", 999.999)
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["anomalies"][0]["excess_ppm"], 0.001)

    def test_negative_drift_direction_fast(self):
        """负漂移：UTC 推进慢于本地，设备本地时钟偏快。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 60000, 59500)  # -8333.333 ppm
        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        a = body["anomalies"][0]
        self.assertEqual(a["direction"], "local_clock_fast")
        self.assertEqual(a["drift_ppm"], -8333.333)
        self.assertEqual(a["excess_ppm"], 8233.333)

    def test_mixed_segments_only_bad_ones_flagged(self):
        """多段中只有超阈值的段进入异常列表，max_abs 取全段绝对值最大者。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 1_000_000, 1_000_050)   # +50 ppm 正常
        self.add_anchor("D1", 2_000_000, 2_001_550)   # dl=1e6 du=1_001_500 -> +1500 ppm
        self.add_anchor("D1", 3_000_000, 3_001_500)   # -50 ppm 正常
        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        self.assertEqual(body["anchor_count"], 4)
        self.assertEqual([a["index"] for a in body["anomalies"]], [1])
        self.assertEqual(body["max_abs_drift_ppm"], 1500.0)
        self.assertEqual(body["segments"][0]["drift_ppm"], 50.0)
        self.assertEqual(body["segments"][2]["drift_ppm"], -50.0)
        self.assert_well_formed(body, "D1", "default", 100, 4)

    # ------------------------------------------------------------ 周期独立

    def test_audit_uses_only_named_cycle_anchors(self):
        """审计只取指定周期锚点；不同周期相同本地毫秒互不影响。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 100000, 100000)           # default 恒等
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.add_anchor("D1", 0, 500000, "boot-2")
        self.add_anchor("D1", 60000, 560500, "boot-2")  # +8333 ppm

        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["segments"][0]["drift_ppm"], 0.0)

        status, body = self.audit("D1", "boot-2", 100)
        self.assertEqual(status, 200)
        self.assertEqual(body["cycle_id"], "boot-2")
        self.assertEqual(body["segments"][0]["drift_ppm"], 8333.333)
        self.assertFalse(body["ok"])

    def test_sealed_cycle_still_auditable(self):
        """封存周期只读审计不受影响。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 1000, 1001)
        _, detail = self.req("GET", "/devices/D1/cycles/default")
        fp = detail["cycle"]["fingerprint"]
        status, _ = self.req("POST", "/devices/D1/cycles/default/seal",
                             {"fingerprint": fp})
        self.assertEqual(status, 201)
        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        self.assertEqual(body["anomalies"][0]["reason"], "drift_exceeds_threshold")

    # ------------------------------------------------------------ 版本快照

    def test_version_audit_uses_frozen_anchors(self):
        """指定版本只审计发布时冻结的锚点：后续更正/新发布不改历史结果。"""
        self.make_identity_device("D1")
        status, _ = self.req("POST", "/versions", {"note": "v1"})
        self.assertEqual(status, 201)

        status, before = self.audit("D1", "default", 100, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(before["source"], "version")
        self.assertEqual(before["version_no"], 1)
        self.assertTrue(before["ok"])
        self.assertEqual(before["segments"][0]["drift_ppm"], 0.0)

        # 发布后锚点更正（制造 +1000 ppm 漂移），再发布 v2
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 0},
                              {"local_ms": 100000, "utc_ms": 100100}]})
        self.req("POST", "/versions", {"note": "v2"})

        # 历史 v1 结果逐字段不变
        status, after = self.audit("D1", "default", 100, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(after, before)

        # 当前与 v2 反映新锚点
        status, cur = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        self.assertEqual(cur["segments"][0]["drift_ppm"], 1000.0)
        status, v2 = self.audit("D1", "default", 100, version_no=2)
        self.assertEqual(status, 200)
        self.assertEqual(v2["segments"], cur["segments"])
        self.assertEqual(v2["first_anchor"], cur["first_anchor"])
        self.assertEqual(v2["last_anchor"], cur["last_anchor"])

    # ------------------------------------------------------------ 输入校验

    def test_insufficient_anchors_rejected(self):
        """锚点 0 个或 1 个：400 insufficient_anchors，不改动数据。"""
        self.make_device("D1")  # default 周期存在但无锚点
        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "insufficient_anchors")
        self.assertEqual(body["error"]["details"]["anchor_count"], 0)

        self.add_anchor("D1", 0, 0)
        status, body = self.audit("D1", "default", 100)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "insufficient_anchors")
        self.assertEqual(body["error"]["details"]["anchor_count"], 1)

        # 锚点数据仍在（未被审计改动）
        status, body = self.req("GET", "/devices/D1/anchors")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["anchors"]), 1)

    def test_invalid_threshold_rejected(self):
        """阈值非法：负数/非数字/null/布尔/NaN/Infinity -> 400 invalid_threshold。"""
        self.make_identity_device("D1")
        bad = [-1, -0.5, "100", None, True, False, [], {}]
        for value in bad:
            with self.subTest(value=value):
                status, body = self.audit("D1", "default", value)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_threshold")
        # NaN / Infinity：标准 json 可解析为浮点，校验阶段拒绝
        for raw in ('{"max_drift_ppm": NaN}',
                    '{"max_drift_ppm": Infinity}',
                    '{"max_drift_ppm": -Infinity}'):
            with self.subTest(raw=raw):
                status, body = self.req(
                    "POST", "/devices/D1/cycles/default/drift-audit", raw, raw=True)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_threshold")

    def test_missing_fields_and_bad_payload(self):
        """缺 max_drift_ppm / 请求体为空 / 非对象 -> 400 validation_error。"""
        self.make_identity_device("D1")
        status, body = self.req("POST", "/devices/D1/cycles/default/drift-audit",
                                {"version_no": 1})
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))
        status, body = self.req("POST", "/devices/D1/cycles/default/drift-audit")
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))
        status, body = self.req("POST", "/devices/D1/cycles/default/drift-audit",
                                [100])
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))

    def test_invalid_version_no_rejected(self):
        """version_no 非法（0/负数/字符串/浮点/布尔）-> 400 validation_error。"""
        self.make_identity_device("D1")
        for value in (0, -1, "1", 1.0, True):
            with self.subTest(value=value):
                status, body = self.req(
                    "POST", "/devices/D1/cycles/default/drift-audit",
                    {"max_drift_ppm": 100, "version_no": value})
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "validation_error")

    def test_device_cycle_version_not_found(self):
        """设备/周期/版本不存在 -> 明确 404，不改动数据。"""
        self.make_identity_device("D1")
        self.req("POST", "/versions")

        status, body = self.audit("NOPE", "default", 100)
        self.assertEqual((status, body["error"]["code"]), (404, "device_not_found"))

        status, body = self.audit("D1", "boot-x", 100)
        self.assertEqual((status, body["error"]["code"]), (404, "cycle_not_found"))

        status, body = self.audit("D1", "default", 100, version_no=999)
        self.assertEqual((status, body["error"]["code"]), (404, "version_not_found"))

        # 路径中的非法周期编号 -> 400 validation_error
        status, body = self.req("POST", "/devices/D1/cycles/bad%20id/drift-audit",
                                {"max_drift_ppm": 100})
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))

    def test_audit_is_read_only(self):
        """合法与非法审计都不改动时间轴与版本数据。"""
        self.make_identity_device("D1")
        self.req("POST", "/versions")
        _, before_timeline = self.req("GET", "/timeline/current")
        _, before_versions = self.req("GET", "/versions")

        self.audit("D1", "default", 100)
        self.audit("D1", "default", 100, version_no=1)
        self.audit("D1", "default", -1)
        self.audit("NOPE", "default", 100)
        self.audit("D1", "default", 100, version_no=999)

        _, after_timeline = self.req("GET", "/timeline/current")
        _, after_versions = self.req("GET", "/versions")
        self.assertEqual(after_timeline["devices"], before_timeline["devices"])
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_versions, before_versions)

    def test_int64_anchors_supported(self):
        """接近 int64 上限的锚点毫秒可正常计算。"""
        self.make_device("D1")
        self.add_anchor("D1", MAX_INT64 - 1_000_000, MAX_INT64 - 1_000_000)
        self.add_anchor("D1", MAX_INT64, MAX_INT64)
        status, body = self.audit("D1", "default", 0)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["local_span_ms"], 1_000_000)

    # ------------------------------------------------------------ 并发一致性

    def test_concurrent_audit_with_writes(self):
        """审计与锚点更正/发布并发：每次审计响应都是一致快照——
        结果良构（首尾自洽、跨度一致、异常区间与逐段标记对应）。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 100000, 100000)
        errors, reports = [], []

        def correct(k):
            try:
                self.req("PUT", "/devices/D1/anchors",
                         {"anchors": [{"local_ms": 0, "utc_ms": 0},
                                      {"local_ms": 100000,
                                       "utc_ms": 100000 + k * 50}]})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                self.req("POST", "/versions")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def audit():
            try:
                status, body = self.audit("D1", "default", 100)
                if status != 200:
                    errors.append(("audit_status", status, body))
                else:
                    reports.append(body)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for k in range(1, 6):
            threads.append(threading.Thread(target=correct, args=(k,)))
            threads.append(threading.Thread(target=publish))
            threads.append(threading.Thread(target=audit))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(reports)
        for body in reports:
            self.assert_well_formed(body, "D1", "default", 100, 2)


class DriftLogicTestCase(unittest.TestCase):
    """纯逻辑补充：乱序输入排序、精确阈值边界、max_abs 统计。"""

    def test_unsorted_anchors_sorted_by_local_ms(self):
        report = compute_drift_report(
            [{"local_ms": 61000, "utc_ms": 1700000060500},
             {"local_ms": 1000, "utc_ms": 1700000000000}], 100)
        self.assertEqual(report["first_anchor"]["local_ms"], 1000)
        self.assertEqual(report["last_anchor"]["local_ms"], 61000)
        self.assertEqual(report["segments"][0]["drift_ppm"], 8333.333)

    def test_exact_boundary_decimal_threshold(self):
        """500 ppm 漂移配 500.0 浮点阈值：精确分数比较，不误判为异常。"""
        report = compute_drift_report(
            [{"local_ms": 0, "utc_ms": 0},
             {"local_ms": 1_000_000, "utc_ms": 1_000_500}], 500.0)
        self.assertTrue(report["ok"])
        report = compute_drift_report(
            [{"local_ms": 0, "utc_ms": 0},
             {"local_ms": 3, "utc_ms": 4}], 333333.333)
        # 漂移 = 333333.333... ppm，阈值 333333.333 -> 超出 0.000333... -> 异常
        self.assertFalse(report["ok"])
        self.assertEqual(report["anomalies"][0]["excess_ppm"], 0.0)  # 四舍五入到 3 位

    def test_insufficient_anchors_logic_error(self):
        for anchors in ([], [{"local_ms": 0, "utc_ms": 0}]):
            with self.assertRaises(Exception) as cm:
                compute_drift_report(anchors, 100)
            self.assertEqual(cm.exception.code, "insufficient_anchors")


class LegacyVersionDriftTestCase(unittest.TestCase):
    """旧版（format_rev=1）快照：缺失的周期归属按 default 处理。"""

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
        self._seed_legacy()
        db.init_db(self.db_path)  # 在线迁移
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    def audit(self, device_id, cycle_id, max_drift_ppm, version_no=None):
        payload = {"max_drift_ppm": max_drift_ppm}
        if version_no is not None:
            payload["version_no"] = version_no
        return self.req(
            "POST", f"/devices/{device_id}/cycles/{cycle_id}/drift-audit", payload)

    def _seed_legacy(self):
        """v1 旧库：恒等锚点 (0,0)-(100000,100000)，已发布 v1（cycle_id 为 NULL）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute("INSERT INTO devices VALUES ('D1','录音笔','2026-09-01T00:00:00.000Z')")
            conn.execute("INSERT INTO anchors VALUES ('D1',0,0,'t')")
            conn.execute("INSERT INTO anchors VALUES ('D1',100000,100000,'t')")
            conn.execute("INSERT INTO versions VALUES (1,'旧版','2026-09-02T00:00:00.000Z')")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',0,0)")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',100000,100000)")
            conn.commit()
        finally:
            conn.close()

    def test_legacy_version_audit_cycle_defaults_to_default(self):
        """旧版快照审计：冻结锚点按 default 周期归属，漂移为 0。"""
        status, body = self.audit("D1", "default", 100, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "version")
        self.assertEqual(body["anchor_count"], 2)
        self.assertEqual(body["segments"][0]["drift_ppm"], 0.0)
        self.assertTrue(body["ok"])
        self.assertEqual(body["first_anchor"], {"local_ms": 0, "utc_ms": 0})
        self.assertEqual(body["last_anchor"], {"local_ms": 100000, "utc_ms": 100000})

    def test_legacy_version_other_cycle_has_no_frozen_anchors(self):
        """旧版本中新建周期无冻结锚点：周期存在 -> 400 insufficient_anchors。"""
        status, _ = self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.assertEqual(status, 201)
        status, body = self.audit("D1", "boot-2", 100, version_no=1)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "insufficient_anchors")

    def test_legacy_version_audit_frozen_after_correction(self):
        """迁移后更正锚点，旧版审计结果不变。"""
        status, before = self.audit("D1", "default", 100, version_no=1)
        self.assertEqual(status, 200)
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 0},
                              {"local_ms": 100000, "utc_ms": 100100}]})
        status, after = self.audit("D1", "default", 100, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(after, before)
        # 当前锚点已带漂移
        status, cur = self.audit("D1", "default", 100)
        self.assertEqual(status, 200)
        self.assertEqual(cur["segments"][0]["drift_ppm"], 1000.0)
        self.assertFalse(cur["ok"])


if __name__ == "__main__":
    unittest.main()
