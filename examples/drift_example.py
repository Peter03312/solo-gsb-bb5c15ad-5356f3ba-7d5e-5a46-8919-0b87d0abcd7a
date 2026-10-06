#!/usr/bin/env python3
"""现场时钟漂移审计（POST /devices/{id}/cycles/{cycle}/drift-audit）最小示例。

现场校准后，运维人员提交设备编号、周期编号、非负最大时钟漂移 ppm（可选
已发布版本号），服务读取当前（或该版本冻结的）锚点，按本地时间相邻配对，
逐段计算 UTC/本地速率与漂移 ppm，标出超过阈值的区间，并给出首尾锚点、
区间跨度与异常原因。本脚本自动建设备/锚点并演示：

  1) 正常审计（含两段漂移，一段偏快一段偏慢，均超阈值）；
  2) 指定历史版本审计（冻结锚点，后续更正不影响）；
  3) 锚点少于两个 / 阈值非法 / 设备、周期、版本不存在的明确错误。

用法：
    1) 先启动服务：  python3 -m app
    2) 再运行本例：  python3 examples/drift_example.py [base_url]
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

    # 1. 注册设备并录入三个现场校准锚点（default 周期）
    call(base, "POST", "/devices", {"device_id": "D1", "name": "录音笔-甲"})
    call(base, "POST", "/devices/D1/anchors",
         {"local_ms": 1000, "utc_ms": 1700000000000})
    # 第 1 段：本地走 60000ms，UTC 走 60500ms -> +8333.333 ppm（本地时钟偏慢）
    call(base, "POST", "/devices/D1/anchors",
         {"local_ms": 61000, "utc_ms": 1700000060500})
    # 第 2 段：本地走 60000ms，UTC 走 59500ms -> -8333.333 ppm（本地时钟偏快）
    call(base, "POST", "/devices/D1/anchors",
         {"local_ms": 121000, "utc_ms": 1700000120000})

    # 2. 当前锚点漂移审计：阈值 100 ppm，两段都会被标为异常区间
    show("当前锚点漂移审计（阈值 100 ppm）",
         *call(base, "POST", "/devices/D1/cycles/default/drift-audit",
               {"max_drift_ppm": 100}))

    # 3. 放宽到 10000 ppm：同样数据不再报异常（ok=true）
    show("放宽阈值到 10000 ppm（无异常）",
         *call(base, "POST", "/devices/D1/cycles/default/drift-audit",
               {"max_drift_ppm": 10000}))

    # 4. 发布版本后更正锚点；历史版本审计仍使用发布时冻结的锚点
    call(base, "POST", "/versions", {"note": "现场校准完成"})
    call(base, "PUT", "/devices/D1/anchors",
         {"anchors": [{"local_ms": 1000, "utc_ms": 1700000000000},
                      {"local_ms": 61000, "utc_ms": 1700000060500},
                      {"local_ms": 121000, "utc_ms": 1700000120120}]})
    show("审计已发布版本 v1（冻结锚点，不受更正影响）",
         *call(base, "POST", "/devices/D1/cycles/default/drift-audit",
               {"max_drift_ppm": 100, "version_no": 1}))

    # 5. 明确错误（只读，不改动任何数据）
    call(base, "POST", "/devices", {"device_id": "D2"})
    show("D2/default 锚点不足两个 -> 400 insufficient_anchors",
         *call(base, "POST", "/devices/D2/cycles/default/drift-audit",
               {"max_drift_ppm": 100}))
    show("阈值为负 -> 400 invalid_threshold",
         *call(base, "POST", "/devices/D1/cycles/default/drift-audit",
               {"max_drift_ppm": -5}))
    show("设备不存在 -> 404 device_not_found",
         *call(base, "POST", "/devices/NOPE/cycles/default/drift-audit",
               {"max_drift_ppm": 100}))
    show("周期不存在 -> 404 cycle_not_found",
         *call(base, "POST", "/devices/D1/cycles/boot-x/drift-audit",
               {"max_drift_ppm": 100}))
    show("版本不存在 -> 404 version_not_found",
         *call(base, "POST", "/devices/D1/cycles/default/drift-audit",
               {"max_drift_ppm": 100, "version_no": 999}))


if __name__ == "__main__":
    main()
