#!/usr/bin/env python3
"""锚点更正覆盖预演（POST /devices/{id}/cycles/{cycle}/coverage-rehearsal）最小示例。

现场人员在实际更正某设备周期锚点前，携带周期详情中的内容指纹与拟整体
替换的非空锚点列表，预演指定 UTC 半开时段的录音覆盖会否恶化：服务在
同一当前快照内分别计算更正前（before）与假设更正后（after）的覆盖
（其他周期和设备保持当前值），输出两侧覆盖结论、连续缺口，以及原先
达标而预演后不达标的各个连续区间（regressions）。预演只读，不影响
当前时间轴或已发布版本。本脚本自动建设备/锚点/声段并演示：

  1) 预演"锚点整体平移"：覆盖恶化，regressions 给出恶化区间；
  2) 预演"按当前锚点原样替换"：before 与 after 一致，无恶化；
  3) 指纹过期 / 周期已封存 / 锚点不严格递增 / 设备或周期不存在的明确错误；
  4) 预演后当前时间轴不变（只读）。

用法：
    1) 先启动服务：  python3 -m app
    2) 再运行本例：  python3 examples/rehearsal_example.py [base_url]
默认连接 http://127.0.0.1:8000，仅依赖标准库。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


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

    # 1. 两台恒等映射设备（本地毫秒即 UTC 毫秒），全程 [0,15000) 共同覆盖
    for dev in ("D1", "D2"):
        call(base, "POST", "/devices", {"device_id": dev})
        call(base, "POST", f"/devices/{dev}/anchors",
             {"local_ms": 0, "utc_ms": 0})
        call(base, "POST", f"/devices/{dev}/anchors",
             {"local_ms": 100000, "utc_ms": 100000})
    call(base, "POST", "/segments",
         {"capture_id": "CAP-1", "device_id": "D1",
          "local_start_ms": 0, "local_end_ms": 15000})
    call(base, "POST", "/segments",
         {"capture_id": "CAP-2", "device_id": "D2",
          "local_start_ms": 0, "local_end_ms": 15000})

    # 2. 取 D1/default 周期详情中的内容指纹（预演请求必须携带）
    _, detail = call(base, "GET", "/devices/D1/cycles/default")
    fp = detail["cycle"]["fingerprint"]
    print(f"D1/default 当前内容指纹: {fp}")

    # 3. 预演：拟把 D1/default 锚点整体平移 +20000ms
    #    -> CAP-1 将映射到 [20000,35000)，[0,15000) 内只剩 D2，
    #       整段成为"原先达标而预演后不达标"的恶化区间
    show("预演：D1/default 锚点整体平移 +20000（覆盖恶化）",
         *call(base, "POST", "/devices/D1/cycles/default/coverage-rehearsal",
               {"fingerprint": fp,
                "anchors": [{"local_ms": 0, "utc_ms": 20000},
                            {"local_ms": 100000, "utc_ms": 120000}],
                "start_ms": 0, "end_ms": 15000, "min_devices": 2}))

    # 4. 预演：按当前锚点原样替换 -> before 与 after 一致，无恶化区间
    show("预演：锚点原样替换（无恶化）",
         *call(base, "POST", "/devices/D1/cycles/default/coverage-rehearsal",
               {"fingerprint": fp,
                "anchors": [{"local_ms": 0, "utc_ms": 0},
                            {"local_ms": 100000, "utc_ms": 100000}],
                "start_ms": 0, "end_ms": 15000, "min_devices": 2}))

    # 5. 预演只读：当前时间轴不变（CAP-1 仍映射在 [0,15000)）
    _, seg = call(base, "GET", "/segments/CAP-1")
    show("预演后 CAP-1 投影不变（只读）", 200, seg)

    # 6. 明确错误（均不改动任何数据）
    call(base, "POST", "/devices/D1/anchors",
         {"local_ms": 50000, "utc_ms": 50000})  # 晚到记录改动周期内容
    show("指纹过期 -> 409 fingerprint_stale",
         *call(base, "POST", "/devices/D1/cycles/default/coverage-rehearsal",
               {"fingerprint": fp,
                "anchors": [{"local_ms": 0, "utc_ms": 0},
                            {"local_ms": 100000, "utc_ms": 100000}],
                "start_ms": 0, "end_ms": 15000, "min_devices": 2}))
    _, detail = call(base, "GET", "/devices/D1/cycles/default")
    fp = detail["cycle"]["fingerprint"]  # 重新获取指纹
    show("锚点 UTC 不严格递增 -> 400 anchors_not_increasing",
         *call(base, "POST", "/devices/D1/cycles/default/coverage-rehearsal",
               {"fingerprint": fp,
                "anchors": [{"local_ms": 0, "utc_ms": 5000},
                            {"local_ms": 100000, "utc_ms": 5000}],
                "start_ms": 0, "end_ms": 15000, "min_devices": 2}))
    show("空锚点列表 -> 400 validation_error",
         *call(base, "POST", "/devices/D1/cycles/default/coverage-rehearsal",
               {"fingerprint": fp, "anchors": [],
                "start_ms": 0, "end_ms": 15000, "min_devices": 2}))
    show("设备不存在 -> 404 device_not_found",
         *call(base, "POST", "/devices/NOPE/cycles/default/coverage-rehearsal",
               {"fingerprint": fp,
                "anchors": [{"local_ms": 0, "utc_ms": 0},
                            {"local_ms": 100000, "utc_ms": 100000}],
                "start_ms": 0, "end_ms": 15000, "min_devices": 2}))
    show("周期不存在 -> 404 cycle_not_found",
         *call(base, "POST", "/devices/D1/cycles/boot-x/coverage-rehearsal",
               {"fingerprint": fp,
                "anchors": [{"local_ms": 0, "utc_ms": 0},
                            {"local_ms": 100000, "utc_ms": 100000}],
                "start_ms": 0, "end_ms": 15000, "min_devices": 2}))
    # 封存周期后预演 -> 409 cycle_sealed（封存不可撤销，放到最后演示）
    call(base, "POST", "/devices/D1/cycles/default/seal", {"fingerprint": fp})
    show("周期已封存 -> 409 cycle_sealed",
         *call(base, "POST", "/devices/D1/cycles/default/coverage-rehearsal",
               {"fingerprint": fp,
                "anchors": [{"local_ms": 0, "utc_ms": 0},
                            {"local_ms": 100000, "utc_ms": 100000}],
                "start_ms": 0, "end_ms": 15000, "min_devices": 2}))


if __name__ == "__main__":
    main()
