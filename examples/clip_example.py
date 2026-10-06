#!/usr/bin/env python3
"""回放裁剪（POST /segments/clip）最小复现示例。

演示报告中的场景：输入半开 UTC 窗口 [1700000004000,1700000019000)，
已存在与窗口相交的可定位声段 CAP-0001，接口按既有裁剪规则返回该声段
（保留 capture_id/device_id/cycle_id、本地裁剪起止与映射后的 UTC 起止，
segment_count 与结果一致）；无相交/不可定位/裁后零长度不返回；
非法窗口按现有错误契约拒绝。

用法：
    1) 先启动服务：  python3 -m app
    2) 再运行本例：  python3 examples/clip_example.py [base_url]
默认连接 http://127.0.0.1:8000，仅依赖标准库。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

WINDOW_START = 1700000004000
WINDOW_END = 1700000019000


def call(base: str, method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} (HTTP {status}) ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

    # 1. 注册设备 + 录入两个同步锚点（带线性漂移）
    call(base, "POST", "/devices", {"device_id": "D1", "name": "录音笔-甲"})
    call(base, "POST", "/devices/D1/anchors",
         {"local_ms": 1000, "utc_ms": 1700000000000})
    call(base, "POST", "/devices/D1/anchors",
         {"local_ms": 61000, "utc_ms": 1700000060500})

    # 2. 提交与窗口相交的可定位声段 CAP-0001（本地 [5000,20000]，
    #    投影 [1700000004033,1700000019158]，跨窗口右端 1700000019000）
    call(base, "POST", "/segments",
         {"capture_id": "CAP-0001", "device_id": "D1",
          "local_start_ms": 5000, "local_end_ms": 20000, "note": "鸟鸣"})
    # 一条不可定位声段（超出锚点覆盖），裁剪时不应返回
    call(base, "POST", "/segments",
         {"capture_id": "CAP-UNLOC", "device_id": "D1",
          "local_start_ms": 90000, "local_end_ms": 95000})

    # 3. 当前时间轴裁剪：报告窗口 [1700000004000,1700000019000)
    #    应返回 CAP-0001：本地 [5000,19843]，UTC [1700000004033,1700000019000]，
    #    segment_count=1；不可定位声段不返回。
    show("当前时间轴裁剪（应返回相交的 CAP-0001）",
         *call(base, "POST", "/segments/clip",
               {"start_ms": WINDOW_START, "end_ms": WINDOW_END}))

    # 4. 窗口完全在声段之后 -> 无相交，segments=[]
    show("无相交窗口（segments 为空）",
         *call(base, "POST", "/segments/clip",
               {"start_ms": 1700000020000, "end_ms": 1700000030000}))

    # 5. 历史版本裁剪（需要先发布；这里演示参数用法，未发布则 404）
    #    call(base, "POST", "/versions")
    #    call(base, "POST", "/segments/clip",
    #         {"start_ms": WINDOW_START, "end_ms": WINDOW_END, "version_no": 1})

    # 6. 非法窗口继续按现有错误契约拒绝（400，且不改动数据）
    show("起点不小于终点 -> 400 invalid_range",
         *call(base, "POST", "/segments/clip",
               {"start_ms": WINDOW_END, "end_ms": WINDOW_START}))
    show("非法时间（负数） -> 400 invalid_time",
         *call(base, "POST", "/segments/clip",
               {"start_ms": -1, "end_ms": WINDOW_END}))


if __name__ == "__main__":
    main()
