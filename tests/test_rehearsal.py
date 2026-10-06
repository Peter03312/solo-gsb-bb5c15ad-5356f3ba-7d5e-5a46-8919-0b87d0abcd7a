"""端到端测试：锚点更正覆盖预演（coverage rehearsal，v1.7）。

覆盖：
- 基本预演：同一当前快照内分别按现有锚点与拟整体替换锚点计算覆盖，
  返回更正前/后两侧结论（ok/片段/缺口/统计）与连续恶化区间
  （原先达标而预演后不达标的连续半开区间，含两侧设备并集与峰值设备数）；
- 预演锚点遵循现有周期映射与毫秒取整规则：不可定位声段不计、
  同设备重叠声段只算一台、其他周期与其他设备保持当前值；
- 无恶化（两侧一致 / 更正后改善）时 worsened=false；多个不连通退化区间
  分别列出，相邻退化区间合并为一个连续区间；
- 拒绝：指纹过期（409 rehearsal_fingerprint_stale）、周期已封存
  （409 cycle_sealed）、锚点不严格递增（400 anchors_not_increasing）、
  时间或门槛非法（400 invalid_time/invalid_range/invalid_threshold）、
  设备或周期不存在（404）、缺字段/锚点空/非对象（400 validation_error）；
- 只读：预演（含被拒绝的预演）不改动当前时间轴与已发布版本；
- 并发：预演与实际锚点更正/发布并发时，每次响应的两侧片段都恰好
  不重不漏地拼满查询区间，且恶化区间与两侧统计自洽。

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


class CoverageRehearseTestCase(unittest.TestCase):
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

    def fingerprint(self, device_id, cycle_id="default"):
        _, body = self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")
        return body["cycle"]["fingerprint"]

    def make_identity_device(self, device_id):
        """恒等映射设备：锚点 (0,0)-(100000,100000)，本地毫秒即 UTC 毫秒。"""
        self.make_device(device_id)
        self.add_anchor(device_id, 0, 0)
        self.add_anchor(device_id, 100000, 100000)

    def rehearse(self, device_id, anchors, start_ms, end_ms, min_devices,
                 fingerprint=None, cycle_id="default"):
        payload = {"fingerprint": fingerprint or self.fingerprint(device_id, cycle_id),
                   "anchors": anchors, "start_ms": start_ms, "end_ms": end_ms,
                   "min_devices": min_devices}
        return self.req(
            "POST",
            f"/devices/{device_id}/cycles/{cycle_id}/coverage/rehearse", payload)

    def assert_tiles(self, cov, start_ms, end_ms):
        """单侧覆盖片段不重不漏地恰好拼满查询区间，统计自洽。"""
        fragments = cov["fragments"]
        self.assertTrue(fragments)
        self.assertEqual(fragments[0]["start_ms"], start_ms)
        self.assertEqual(fragments[-1]["end_ms"], end_ms)
        for prev, nxt in zip(fragments, fragments[1:]):
            self.assertEqual(prev["end_ms"], nxt["start_ms"])
        self.assertEqual(cov["covered_ms"] + cov["gap_ms"], end_ms - start_ms)
        self.assertEqual(cov["ok"], not cov["gaps"])

    # ------------------------------------------------------------ 基本计算

    def test_basic_shift_produces_regression(self):
        """D1 锚点整体 +4000ms：更正前整段达标，更正后开头 4000ms 只剩 D2，
        形成一个连续恶化区间 [0,4000)。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)

        status, body = self.rehearse(
            "D1",
            [{"local_ms": 0, "utc_ms": 4000},
             {"local_ms": 100000, "utc_ms": 104000}],
            0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], "D1")
        self.assertEqual(body["cycle_id"], "default")
        self.assertEqual(body["proposed_anchor_count"], 2)
        self.assertTrue(body["worsened"])
        self.assertEqual(body["comparison"]["worsened"], True)

        before, after = body["before"], body["after"]
        self.assertTrue(before["ok"])
        self.assertEqual(before["gap_ms"], 0)
        self.assertEqual(before["covered_ms"], 10000)
        self.assertFalse(after["ok"])
        self.assertEqual(after["gap_ms"], 4000)
        self.assertEqual(after["covered_ms"], 6000)
        self.assert_tiles(before, 0, 10000)
        self.assert_tiles(after, 0, 10000)
        # 两侧参与计算的都是可定位声段
        self.assertEqual(before["located_segment_count"], 2)
        self.assertEqual(after["located_segment_count"], 2)

        comp = body["comparison"]
        self.assertEqual(comp["regression_count"], 1)
        self.assertEqual(comp["regression_ms"], 4000)
        self.assertEqual(comp["regressions"], [
            {"start_ms": 0, "end_ms": 4000, "duration_ms": 4000,
             "before_max_device_count": 2, "after_max_device_count": 1,
             "before_device_ids": ["D1", "D2"],
             "after_device_ids": ["D2"]}])
        self.assertEqual(comp["gap_ms_before"], 0)
        self.assertEqual(comp["gap_ms_after"], 4000)
        self.assertEqual(comp["gap_ms_delta"], 4000)
        self.assertEqual(comp["covered_ms_delta"], -4000)

    def test_before_side_equals_live_coverage_audit(self):
        """预演"更正前"一侧必须等于同一数据上的当前覆盖审计结论。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 5000, 15000)
        proposed = [{"local_ms": 0, "utc_ms": 1000},
                    {"local_ms": 100000, "utc_ms": 101000}]

        status, body = self.rehearse("D1", proposed, 0, 15000, 2)
        self.assertEqual(status, 200)
        status, audit = self.req("POST", "/coverage/audit",
                                 {"start_ms": 0, "end_ms": 15000, "min_devices": 2})
        self.assertEqual(status, 200)
        for key in ("ok", "covered_ms", "gap_ms", "located_segment_count",
                    "fragments", "gaps"):
            self.assertEqual(body["before"][key], audit[key])

    def test_identical_anchors_no_regression(self):
        """拟替换锚点与当前锚点完全相同：两侧一致，无恶化区间。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)

        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 0},
                   {"local_ms": 100000, "utc_ms": 100000}], 0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertFalse(body["worsened"])
        self.assertEqual(body["comparison"]["regressions"], [])
        self.assertEqual(body["comparison"]["regression_ms"], 0)
        self.assertEqual(body["comparison"]["gap_ms_delta"], 0)
        self.assertEqual(body["before"]["fragments"], body["after"]["fragments"])

    def test_already_gap_that_closes_is_not_regression(self):
        """更正后覆盖改善（缺口变短）：不产生恶化区间，delta 为负。"""
        # 更正前 D1 声段在 [4000,14000)，窗口 [0,10000) 前 4000ms 只有 D2
        self.make_device("D1")
        self.add_anchor("D1", 0, 4000)
        self.add_anchor("D1", 100000, 104000)
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)
        # 拟更正为恒等映射：D1 声段回到 [0,10000)，缺口消失
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 0},
                   {"local_ms": 100000, "utc_ms": 100000}], 0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertFalse(body["worsened"])
        self.assertEqual(body["comparison"]["regressions"], [])
        self.assertEqual(body["before"]["gap_ms"], 4000)
        self.assertEqual(body["after"]["gap_ms"], 0)
        self.assertEqual(body["comparison"]["gap_ms_delta"], -4000)

    def test_two_disjoint_regressions(self):
        """D1 声段更正后缩为中间小段：原先达标的两侧分别退化、互不连通。"""
        # 更正前：D1 覆盖 [0,10000)，D2 覆盖 [2000,8000) -> [2000,8000) 达标
        self.make_device("D1")
        # 当前 D1 恒等
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 100000, 100000)
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 2000, 8000)
        # 拟锚点把本地 [0,10000) 映射到 UTC [4000,6000)：斜率 0.2，
        # 即 local=0 -> 4000、local=100000 -> 24000（两点定线）
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 4000},
                   {"local_ms": 100000, "utc_ms": 24000}], 0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertTrue(body["worsened"])
        regs = body["comparison"]["regressions"]
        self.assertEqual([(r["start_ms"], r["end_ms"]) for r in regs],
                         [(2000, 4000), (6000, 8000)])
        for r in regs:
            self.assertEqual(r["before_max_device_count"], 2)
            self.assertEqual(r["after_max_device_count"], 1)
            self.assertEqual(r["before_device_ids"], ["D1", "D2"])
            self.assertEqual(r["after_device_ids"], ["D2"])
        self.assertEqual(body["comparison"]["regression_ms"], 4000)

    def test_adjacent_primitive_regressions_merge(self):
        """更正后设备集合在退化区间内变化，但相邻退化分片合并为一个连续区间，
        设备并集与峰值设备数正确累加。"""
        # 更正前三台设备全程在线；更正后 D1 只剩 [0,2500)
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.make_identity_device("D3")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)
        self.submit("C3", "D3", 0, 5000)
        # D3 在 5000 结束，故更正前 [0,5000) 三台、[5000,10000) 两台，
        # 门槛 2 全程达标。拟把 D1 映射到 [0,2500)：
        #   [0,2500)  D1,D2,D3=3 达标（不退化）
        #   [2500,5000) D2,D3=2 达标（不退化）
        #   [5000,10000) 仅 D2=1 不达标（原先达标）-> 一个连续区间
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 0},
                   {"local_ms": 100000, "utc_ms": 2500}], 0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertTrue(body["worsened"])
        regs = body["comparison"]["regressions"]
        self.assertEqual(len(regs), 1)
        self.assertEqual((regs[0]["start_ms"], regs[0]["end_ms"]), (5000, 10000))
        self.assertEqual(regs[0]["before_max_device_count"], 2)
        self.assertEqual(regs[0]["after_max_device_count"], 1)

        # 再让 D3 也在更正后提前离开：[2500,5000) 与 [5000,10000) 都退化，
        # 相邻，合并为 [2500,10000)，段内设备并集含 D2,D3
        self.req("DELETE", "/segments/C3")
        self.submit("C3", "D3", 0, 4000)  # 更正前：[0,4000) 三台
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 0},
                   {"local_ms": 100000, "utc_ms": 2500}], 0, 10000, 2)
        self.assertEqual(status, 200)
        # 更正前达标区间：[0,4000)（三台）与 [4000,10000)（D1,D2 两台）
        # 更正后：[0,2500) 三台达标；[2500,4000) D2,D3 两台达标；
        # [4000,10000) 仅 D2 一台不达标 -> 退化区间 [4000,10000)
        regs = body["comparison"]["regressions"]
        self.assertEqual([(r["start_ms"], r["end_ms"]) for r in regs],
                         [(4000, 10000)])

    def test_same_device_overlap_counts_once_after(self):
        """同设备重叠声段在更正后仍只算一台（合并规则不因预演改变）。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 100000, 100000)
        self.make_identity_device("D2")
        # D1 两段重叠，更正前覆盖 [0,10000)；更正后整体平移 +3000
        self.submit("C1", "D1", 0, 8000)
        self.submit("C2", "D1", 2000, 10000)
        self.submit("C3", "D2", 0, 10000)
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 3000},
                   {"local_ms": 100000, "utc_ms": 103000}], 0, 10000, 2)
        self.assertEqual(status, 200)
        # 更正后 D1 设备并集为 [3000,13000)，缺口 [0,3000) 内仅 D2
        self.assertEqual(body["after"]["fragments"][0]["device_ids"], ["D2"])
        self.assertEqual(body["comparison"]["regressions"], [
            {"start_ms": 0, "end_ms": 3000, "duration_ms": 3000,
             "before_max_device_count": 2, "after_max_device_count": 1,
             "before_device_ids": ["D1", "D2"], "after_device_ids": ["D2"]}])

    def test_proposed_anchors_can_make_segment_unlocatable(self):
        """拟替换锚点覆盖不足时，原可定位声段变为不可定位：不计入更正后覆盖。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 100000, 100000)
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)
        # 拟锚点只覆盖到本地 5000：C1 终点 local=10000 超出 -> 不可定位
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 0},
                   {"local_ms": 5000, "utc_ms": 5000}], 0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["before"]["located_segment_count"], 2)
        self.assertEqual(body["after"]["located_segment_count"], 1)
        self.assertTrue(body["worsened"])
        self.assertEqual(body["comparison"]["regressions"], [
            {"start_ms": 0, "end_ms": 10000, "duration_ms": 10000,
             "before_max_device_count": 2, "after_max_device_count": 1,
             "before_device_ids": ["D1", "D2"], "after_device_ids": ["D2"]}])

    def test_unlocatable_segments_ignored_before_and_after(self):
        """不可定位声段在两侧都不计入。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 10000, 10000)
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 5000)          # 可定位
        self.submit("C-OUT", "D1", 50000, 60000)  # 超出锚点覆盖，不可定位
        self.submit("C2", "D2", 0, 10000)
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 0},
                   {"local_ms": 10000, "utc_ms": 10000}], 0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["before"]["located_segment_count"], 2)
        self.assertEqual(body["after"]["located_segment_count"], 2)
        self.assertFalse(body["worsened"])

    def test_other_cycles_and_devices_unchanged(self):
        """预演只替换目标 (设备,周期)：同设备其他周期与其他设备保持当前值。"""
        # D1 default 当前 +6000；D1 boot-2 与 D2 均为正常覆盖
        self.make_device("D1")
        self.add_anchor("D1", 0, 6000)
        self.add_anchor("D1", 100000, 106000)
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.add_anchor("D1", 0, 0, cycle_id="boot-2")
        self.add_anchor("D1", 100000, 100000, cycle_id="boot-2")
        self.make_identity_device("D2")
        # default 周期声段更正前在 [6000,16000)
        self.submit("C-DEF", "D1", 0, 10000, cycle_id="default")
        # boot-2 声段覆盖 [0,10000)
        self.submit("C-BOOT", "D1", 0, 10000, cycle_id="boot-2")
        self.submit("C2", "D2", 0, 10000)
        # 拟把 default 锚点改为恒等：[0,10000) 达标区间从 [6000,10000)
        # （boot-2 + D2）扩大到整段；boot-2 与 D2 投影不变
        fp = self.fingerprint("D1", "default")
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 0},
                   {"local_ms": 100000, "utc_ms": 100000}],
            0, 10000, 2, fingerprint=fp, cycle_id="default")
        self.assertEqual(status, 200)
        self.assertFalse(body["worsened"])
        # 更正前：[0,6000) boot-2+D2=2 达标；[6000,10000) 三台；全程达标
        self.assertTrue(body["before"]["ok"])
        self.assertTrue(body["after"]["ok"])

        # 反向：拟把 default 改成更糟不影响 boot-2 的覆盖事实
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 50000},
                   {"local_ms": 100000, "utc_ms": 150000}],
            0, 10000, 2, fingerprint=fp, cycle_id="default")
        self.assertEqual(status, 200)
        # C-DEF 更正后投影到 [50000,..)，窗口内无贡献，但 boot-2+D2 仍在
        self.assertFalse(body["worsened"])
        self.assertTrue(body["after"]["ok"])
        self.assertEqual(body["after"]["located_segment_count"], 2)

    def test_rounding_rule_applied(self):
        """预演映射沿用现有线性漂移与毫秒四舍五入规则：与实际更正后的
        当前时间轴审计结果逐字段一致。"""
        self.make_device("D1")
        self.add_anchor("D1", 1000, 1700000000000)
        self.add_anchor("D1", 61000, 1700000060500)
        self.make_device("D2")
        self.add_anchor("D2", 1000, 1700000000000)
        self.add_anchor("D2", 61000, 1700000060500)
        self.submit("C1", "D1", 5000, 20000)
        self.submit("C2", "D2", 5000, 20000)
        proposed = [{"local_ms": 1000, "utc_ms": 1700000000250},
                    {"local_ms": 61000, "utc_ms": 1700000060750}]
        start, end = 1700000004000, 1700000019000

        status, body = self.rehearse("D1", proposed, start, end, 2)
        self.assertEqual(status, 200)
        # 实际执行同一更正后再审计当前时间轴，应与预演 after 完全一致
        status, _ = self.req("PUT", "/devices/D1/anchors", {"anchors": proposed})
        self.assertEqual(status, 200)
        status, actual = self.req("POST", "/coverage/audit",
                                  {"start_ms": start, "end_ms": end,
                                   "min_devices": 2})
        self.assertEqual(status, 200)
        for key in ("ok", "covered_ms", "gap_ms", "located_segment_count",
                    "fragments", "gaps"):
            self.assertEqual(body["after"][key], actual[key])

    # ------------------------------------------------------------ 拒绝场景

    def test_stale_fingerprint_rejected(self):
        """指纹过期（周期详情内容已变化）-> 409 rehearsal_fingerprint_stale。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)
        stale_fp = self.fingerprint("D1")
        # 晚到记录改动了周期内容（补一个锚点）
        self.add_anchor("D1", 50000, 50000)

        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 4000},
                   {"local_ms": 100000, "utc_ms": 104000}],
            0, 10000, 2, fingerprint=stale_fp)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "rehearsal_fingerprint_stale")
        self.assertEqual(body["error"]["details"]["device_id"], "D1")
        self.assertEqual(body["error"]["details"]["cycle_id"], "default")
        self.assertEqual(len(body["error"]["details"]["current_fingerprint"]), 64)

        # 用最新指纹可正常预演
        status, _ = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 4000},
                   {"local_ms": 50000, "utc_ms": 54000},
                   {"local_ms": 100000, "utc_ms": 104000}], 0, 10000, 2)
        self.assertEqual(status, 200)

    def test_malformed_fingerprint_rejected(self):
        """指纹不是 64 位小写十六进制 -> 400 validation_error。"""
        self.make_identity_device("D1")
        for bad in ("abc", "Z" * 64, None, 123):
            payload = {"fingerprint": bad,
                       "anchors": [{"local_ms": 0, "utc_ms": 0}],
                       "start_ms": 0, "end_ms": 1000, "min_devices": 1}
            status, body = self.req(
                "POST", "/devices/D1/cycles/default/coverage/rehearse", payload)
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"]["code"], "validation_error")

    def test_sealed_cycle_rejected(self):
        """周期已封存 -> 409 cycle_sealed（实际更正会被拒绝，预演同样拒绝）。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)
        fp = self.fingerprint("D1")
        status, _ = self.req("POST", "/devices/D1/cycles/default/seal",
                             {"fingerprint": fp})
        self.assertEqual(status, 201)
        status, body = self.rehearse(
            "D1", [{"local_ms": 0, "utc_ms": 4000},
                   {"local_ms": 100000, "utc_ms": 104000}], 0, 10000, 2)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")

    def test_anchors_not_strictly_increasing_rejected(self):
        """拟替换锚点 local_ms 重复或 utc_ms 非严格递增 -> 400。"""
        self.make_identity_device("D1")
        self.submit("C1", "D1", 0, 10000)
        bad_chains = [
            [{"local_ms": 0, "utc_ms": 0}, {"local_ms": 0, "utc_ms": 100}],
            [{"local_ms": 100, "utc_ms": 100}, {"local_ms": 0, "utc_ms": 100}],
            [{"local_ms": 0, "utc_ms": 100}, {"local_ms": 100, "utc_ms": 100}],
            [{"local_ms": 0, "utc_ms": 100}, {"local_ms": 100, "utc_ms": 50}],
        ]
        for chain in bad_chains:
            status, body = self.rehearse("D1", chain, 0, 10000, 1)
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"]["code"], "anchors_not_increasing")

        # 严格递增（乱序提交也行）预演通过
        status, _ = self.rehearse(
            "D1", [{"local_ms": 100000, "utc_ms": 100000},
                   {"local_ms": 0, "utc_ms": 0}], 0, 10000, 1)
        self.assertEqual(status, 200)

    def test_invalid_anchor_times_rejected(self):
        """拟替换锚点时间非法（非整数/负数/越界/布尔）-> 400 invalid_time。"""
        self.make_identity_device("D1")
        cases = [
            ({"local_ms": -1, "utc_ms": 0}, "local"),
            ({"local_ms": 0, "utc_ms": MAX_INT64 + 1}, "utc"),
            ({"local_ms": 1.5, "utc_ms": 2}, "local"),
            ({"local_ms": "0", "utc_ms": 2}, "local"),
            ({"local_ms": True, "utc_ms": 2}, "local"),
            ({"local_ms": None, "utc_ms": 2}, "local"),
        ]
        for anchor, _ in cases:
            status, body = self.rehearse("D1", [anchor], 0, 10000, 1)
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"]["code"], "invalid_time")

    def test_empty_or_malformed_anchors_rejected(self):
        """anchors 必须是非空数组、每项为含 local_ms/utc_ms 的对象。"""
        self.make_identity_device("D1")
        fp = self.fingerprint("D1")
        base = {"fingerprint": fp, "start_ms": 0, "end_ms": 1000,
                "min_devices": 1}
        for anchors in ([], None, {}, "x", 1, [{"local_ms": 0}],
                        ["nope"], [42]):
            payload = {**base, "anchors": anchors}
            status, body = self.req(
                "POST", "/devices/D1/cycles/default/coverage/rehearse", payload)
            self.assertEqual(status, 400, body)
            self.assertEqual(body["error"]["code"], "validation_error")

    def test_invalid_range_threshold_time_rejected(self):
        """区间与门槛沿用覆盖审计错误契约。"""
        self.make_identity_device("D1")
        self.submit("C1", "D1", 0, 10000)
        chain = [{"local_ms": 0, "utc_ms": 0},
                 {"local_ms": 100000, "utc_ms": 100000}]
        cases = [
            ({"start_ms": 1000, "end_ms": 1000, "min_devices": 1},
             400, "invalid_range"),
            ({"start_ms": 2000, "end_ms": 1000, "min_devices": 1},
             400, "invalid_range"),
            ({"start_ms": -1, "end_ms": 1000, "min_devices": 1},
             400, "invalid_time"),
            ({"start_ms": 0, "end_ms": MAX_INT64 + 1, "min_devices": 1},
             400, "invalid_time"),
            ({"start_ms": 0.5, "end_ms": 1000, "min_devices": 1},
             400, "invalid_time"),
            ({"start_ms": 0, "end_ms": 1000, "min_devices": 0},
             400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 1000, "min_devices": -2},
             400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 1000, "min_devices": 1.5},
             400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 1000, "min_devices": "1"},
             400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 1000}, 400, "validation_error"),
            ({"end_ms": 1000, "min_devices": 1}, 400, "validation_error"),
        ]
        for extra, want_status, want_code in cases:
            payload = {"fingerprint": self.fingerprint("D1"),
                       "anchors": chain, **extra}
            status, body = self.req(
                "POST", "/devices/D1/cycles/default/coverage/rehearse", payload)
            self.assertEqual((status, body["error"]["code"]),
                             (want_status, want_code), extra)

        # 缺 fingerprint / anchors
        status, body = self.req(
            "POST", "/devices/D1/cycles/default/coverage/rehearse",
            {"anchors": chain, "start_ms": 0, "end_ms": 1000, "min_devices": 1})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "validation_error"))
        status, body = self.req(
            "POST", "/devices/D1/cycles/default/coverage/rehearse",
            {"fingerprint": self.fingerprint("D1"),
             "start_ms": 0, "end_ms": 1000, "min_devices": 1})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "validation_error"))

        # 请求体非对象 / 空请求体
        status, body = self.req(
            "POST", "/devices/D1/cycles/default/coverage/rehearse", [1, 2])
        self.assertEqual((status, body["error"]["code"]),
                         (400, "validation_error"))
        status, body = self.req(
            "POST", "/devices/D1/cycles/default/coverage/rehearse")
        self.assertEqual((status, body["error"]["code"]),
                         (400, "validation_error"))

    def test_device_or_cycle_not_found(self):
        """设备不存在 -> 404 device_not_found；周期不存在 -> 404 cycle_not_found。"""
        self.make_identity_device("D1")
        chain = [{"local_ms": 0, "utc_ms": 0}]
        # 设备不存在
        status, body = self.req(
            "POST", "/devices/NOPE/cycles/default/coverage/rehearse",
            {"fingerprint": "0" * 64, "anchors": chain,
             "start_ms": 0, "end_ms": 1000, "min_devices": 1})
        self.assertEqual((status, body["error"]["code"]),
                         (404, "device_not_found"))
        # 周期不存在（路径校验先于指纹校验）
        status, body = self.req(
            "POST", "/devices/D1/cycles/boot-x/coverage/rehearse",
            {"fingerprint": "0" * 64, "anchors": chain,
             "start_ms": 0, "end_ms": 1000, "min_devices": 1})
        self.assertEqual((status, body["error"]["code"]),
                         (404, "cycle_not_found"))

    # ------------------------------------------------------------ 只读

    def test_rehearsal_is_read_only(self):
        """成功预演与各类被拒绝预演都不改动当前时间轴与已发布版本。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)
        self.req("POST", "/versions", {"note": "baseline"})
        self.add_anchor("D1", 50000, 50000)  # 快照基线包含该晚到锚点
        _, before_tl = self.req("GET", "/timeline/current")
        _, before_vs = self.req("GET", "/versions")

        good_chain = [{"local_ms": 0, "utc_ms": 4000},
                      {"local_ms": 50000, "utc_ms": 54000},
                      {"local_ms": 100000, "utc_ms": 104000}]
        for _ in range(2):
            status, _ = self.rehearse("D1", good_chain, 0, 10000, 2)
            self.assertEqual(status, 200)
        # 被拒绝的预演：指纹过期（晚到补锚点后旧指纹失效）/ 递增失败 / 区间非法 / 封存后
        stale = self.fingerprint("D1")
        self.add_anchor("D1", 60000, 60000)
        self.rehearse("D1", good_chain, 0, 10000, 2, fingerprint=stale)
        self.rehearse("D1",
                      [{"local_ms": 0, "utc_ms": 0}, {"local_ms": 0, "utc_ms": 1}],
                      0, 10000, 2)
        self.rehearse("D1", good_chain, 10000, 0, 2)
        fp = self.fingerprint("D1")
        self.req("POST", "/devices/D1/cycles/default/seal", {"fingerprint": fp})
        self.rehearse("D1", good_chain, 0, 10000, 2)

        _, after_tl = self.req("GET", "/timeline/current")
        _, after_vs = self.req("GET", "/versions")
        # 基线之后只有"补锚点"这一真实写入；封存不改内容；预演不产生任何写入
        self.assertEqual(after_tl["segments"], before_tl["segments"])
        self.assertEqual(after_vs, before_vs)
        # 当前锚点仍是预演前的锚点（含后补的 50000/60000，无 4000 平移）
        _, anchors_body = self.req("GET", "/devices/D1/anchors")
        utcs = sorted(a["utc_ms"] for a in anchors_body["anchors"])
        self.assertEqual(utcs, [0, 50000, 60000, 100000])

    # ------------------------------------------------------------ 并发一致性

    def test_concurrent_rehearsal_with_corrections_and_publish(self):
        """预演与实际锚点更正/发布并发：每次响应的两侧片段都恰好拼满查询
        区间，恶化区间与两侧统计自洽。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("C1", "D1", 0, 10000)
        self.submit("C2", "D2", 0, 10000)

        errors, responses = [], []

        def fingerprint_get():
            _, b = self.req("GET", "/devices/D1/cycles/default")
            return b["cycle"]["fingerprint"]

        def rehearse(k):
            try:
                # 并发更正会使指纹过期：重试若干轮，确保至少拿到基于某一
                # 一致快照的成功预演
                for _ in range(10):
                    fp = fingerprint_get()
                    status, body = self.rehearse(
                        "D1", [{"local_ms": 0, "utc_ms": k * 100},
                               {"local_ms": 100000, "utc_ms": 100000 + k * 100}],
                        0, 12000, 2, fingerprint=fp)
                    if status == 200:
                        responses.append(body)
                        return
                    if status != 409:
                        errors.append((status, body))
                        return
                errors.append("rehearsal exhausted retries")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def correct(k):
            try:
                self.req("PUT", "/devices/D1/anchors",
                         {"anchors": [{"local_ms": 0, "utc_ms": k * 100},
                                      {"local_ms": 100000,
                                       "utc_ms": 100000 + k * 100}]})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                self.req("POST", "/versions")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for k in range(1, 6):
            threads.append(threading.Thread(target=correct, args=(k,)))
            threads.append(threading.Thread(target=rehearse, args=(k,)))
            threads.append(threading.Thread(target=publish))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(responses)
        for body in responses:
            self.assert_tiles(body["before"], 0, 12000)
            self.assert_tiles(body["after"], 0, 12000)
            comp = body["comparison"]
            self.assertEqual(comp["gap_ms_delta"],
                             comp["gap_ms_after"] - comp["gap_ms_before"])
            self.assertEqual(comp["regression_ms"],
                             sum(r["duration_ms"] for r in comp["regressions"]))
            # 恶化区间并集必须落在"更正前达标"的区域内
            before_gap = [(g["start_ms"], g["end_ms"])
                          for g in body["before"]["gaps"]]
            for r in comp["regressions"]:
                self.assertLess(r["start_ms"], r["end_ms"])
                for gs, ge in before_gap:
                    self.assertFalse(r["start_ms"] < ge and gs < r["end_ms"],
                                     (r, before_gap))


if __name__ == "__main__":
    unittest.main()
