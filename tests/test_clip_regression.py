"""缺陷回归：当前时间轴查询时回放裁剪必须返回与窗口相交的可定位声段。

输入半开 UTC 窗口 [1700000004000,1700000019000)，可定位声段 CAP-0001 与
该窗口相交（README/演示口径：锚点
(local=1000,utc=1700000000000)-(local=61000,utc=1700000060500)，
声段本地 [5000,20000] 投影到 [1700000004033,1700000019158]）。

接口曾在"当前时间轴"路径错误返回 200 + segments=[]。正确行为：
- 保留 capture_id / device_id / cycle_id；
- 返回本地裁剪起止（起点截在声段自身，终点取"映射 UTC 首次不小于窗口终点"
  的整数本地毫秒）与映射后的 utc_start_ms / utc_end_ms；
- segment_count 与 segments 长度一致；
- 无相交、不可定位、裁后零长度仍不返回；非法窗口继续按现有契约拒绝。
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

WINDOW_START = 1700000004000
WINDOW_END = 1700000019000


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


class ClipCurrentRegressionTestCase(unittest.TestCase):
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

    def _seed(self, *, relocate=False, unlocatable=True):
        """演示口径数据：D1 线性漂移锚点 + 相交声段 CAP-0001。

        relocate=True 时模拟锚点更正后（投影平移），相交关系仍成立。
        unlocatable=True 时额外放一条越界不可定位声段（不应返回）。
        """
        self.req("POST", "/devices", {"device_id": "D1", "name": "录音笔-甲"})
        if not relocate:
            self.req("POST", "/devices/D1/anchors",
                     {"local_ms": 1000, "utc_ms": 1700000000000})
            self.req("POST", "/devices/D1/anchors",
                     {"local_ms": 61000, "utc_ms": 1700000060500})
        else:
            self.req("PUT", "/devices/D1/anchors",
                     {"anchors": [{"local_ms": 1000, "utc_ms": 1700000000250},
                                  {"local_ms": 61000, "utc_ms": 1700000060750}]})
        self.req("POST", "/segments",
                 {"capture_id": "CAP-0001", "device_id": "D1",
                  "local_start_ms": 5000, "local_end_ms": 20000, "note": "鸟鸣"})
        if unlocatable:
            # 超出锚点覆盖 -> 不可定位，即使窗口再大也不返回
            self.req("POST", "/segments",
                     {"capture_id": "CAP-UNLOC", "device_id": "D1",
                      "local_start_ms": 90000, "local_end_ms": 95000})

    # ------------------------------------------------------------ 核心回归

    def test_current_timeline_returns_intersecting_located_segment(self):
        """报告场景：相交且可定位的 CAP-0001 必须出现在当前时间轴裁剪结果中。"""
        self._seed()
        status, body = self.req("POST", "/segments/clip",
                                {"start_ms": WINDOW_START, "end_ms": WINDOW_END})
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "current")
        self.assertIsNone(body["version_no"])
        self.assertEqual(body["start_ms"], WINDOW_START)
        self.assertEqual(body["end_ms"], WINDOW_END)

        self.assertEqual(body["segment_count"], 1)
        self.assertEqual(len(body["segments"]), body["segment_count"])
        seg = body["segments"][0]
        self.assertEqual(seg["capture_id"], "CAP-0001")
        self.assertEqual(seg["device_id"], "D1")
        self.assertEqual(seg["cycle_id"], "default")
        # 本地裁剪：声段起点在窗口内 -> 截在自身起点 5000；
        # 终点取首个映射 UTC >= 1700000019000 的整数本地毫秒（=19843）
        self.assertEqual(seg["local_start_ms"], 5000)
        self.assertEqual(seg["local_end_ms"], 19843)
        self.assertLess(seg["local_start_ms"], seg["local_end_ms"])
        # 映射后的 UTC 起止（沿用线性漂移 + 四舍五入）
        self.assertEqual(seg["utc_start_ms"], 1700000004033)
        self.assertEqual(seg["utc_end_ms"], WINDOW_END)
        # 不可定位声段不返回
        self.assertEqual([s["capture_id"] for s in body["segments"]], ["CAP-0001"])

    def test_current_timeline_after_anchor_correction_still_clipped(self):
        """锚点更正后当前时间轴实时重算：相交声段仍按既有裁剪规则返回。"""
        self._seed(relocate=True)
        status, body = self.req("POST", "/segments/clip",
                                {"start_ms": WINDOW_START, "end_ms": WINDOW_END})
        self.assertEqual(status, 200)
        self.assertEqual([s["capture_id"] for s in body["segments"]], ["CAP-0001"])
        seg = body["segments"][0]
        # +250 漂移后：起点首个 utc>=1700000004000 为 local 4750（utc=1700000004033 附近）
        self.assertLess(seg["local_start_ms"], seg["local_end_ms"])
        self.assertGreaterEqual(seg["utc_start_ms"], WINDOW_START)
        self.assertEqual(seg["utc_end_ms"], WINDOW_END)

    # ------------------------------------------------------------ 不返回的情形

    def test_non_intersecting_and_unlocatable_not_returned(self):
        """无相交 / 不可定位 / 裁后零长度：仍不返回。"""
        self._seed()
        # 窗口完全在声段之后：窗口起点 >= 声段末端 -> 不相交
        status, body = self.req("POST", "/segments/clip",
                                {"start_ms": 1700000020000, "end_ms": 1700000030000})
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [])
        self.assertEqual(body["segment_count"], 0)

        # 窗口与声段仅边界相接（半开 [..,utc_end)）：无内容
        # 声段起点 1700000004033；取一个恰在其起点结束的窗口
        status, body = self.req("POST", "/segments/clip",
                                {"start_ms": 1700000000000,
                                 "end_ms": 1700000004033})
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [])
        self.assertEqual(body["segment_count"], 0)

    # ------------------------------------------------------------ 非法窗口契约

    def test_invalid_window_rejected(self):
        """非法窗口继续按现有错误契约拒绝。"""
        self._seed()
        for payload, code in [
            ({"start_ms": WINDOW_END, "end_ms": WINDOW_START}, "invalid_range"),
            ({"start_ms": WINDOW_START, "end_ms": WINDOW_START}, "invalid_range"),
            ({"start_ms": -1, "end_ms": WINDOW_END}, "invalid_time"),
            ({"start_ms": WINDOW_START, "end_ms": 2 ** 63}, "invalid_time"),
            ({"start_ms": "x", "end_ms": WINDOW_END}, "invalid_time"),
            ({"end_ms": WINDOW_END}, "validation_error"),
        ]:
            with self.subTest(payload=payload):
                status, b = self.req("POST", "/segments/clip", payload)
                self.assertEqual(status, 400)
                self.assertEqual(b["error"]["code"], code)


if __name__ == "__main__":
    unittest.main()
