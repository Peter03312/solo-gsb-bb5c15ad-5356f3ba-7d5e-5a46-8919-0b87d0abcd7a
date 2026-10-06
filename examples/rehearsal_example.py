#!/usr/bin/env python3
"""锚点更正覆盖预演（POST /devices/{id}/cycles/{cycle}/coverage/rehearse）最小示例。

现场人员在实际更正某设备周期锚点"之前"，先预演指定 UTC 半开时段的录音
覆盖会否恶化：提交周期详情中的内容指纹、拟整体替换的非空锚点列表、时段
起止与最少设备数；服务在同一当前快照内分别按现有锚点（更正前）与拟替换
锚点（假设更正后）计算覆盖，返回两侧结论、连续缺口，以及"原先达标而
预演后不达标"的各个连续区间。预演只读，不影响当前时间轴与已发布版本。

本脚本自动建设备/锚点/声段并演示：

  1) 会恶化的预演：D1 锚点整体 +4000ms，窗口开头 4000ms 只剩 D2；
  2) 不恶化的预演：拟替换锚点与当前一致（两侧相同）；
  3) 明确拒绝：指纹过期、锚点非严格递增、时间/门槛非法、
     设备/周期不存在、周期已封存。

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


def fingerprint(base: str, device_id: str, cycle_id: str = "default") -> str:
    _, body = call(base, "GET", f"/devices/{device_id}/cycles/{cycle_id}")
    return body["cycle"]["fingerprint"]


def rehearse(base, device_id, anchors, start_ms, end_ms, min_devices,
             fp=None, cycle_id="default"):
    payload = {
        "fingerprint": fp or fingerprint(base, device_id, cycle_id),
        "anchors": anchors, "start_ms": start_ms, "end_ms": end_ms,
        "min_devices": min_devices}
    return call(base, "POST",
                f"/devices/{device_id}/cycles/{cycle_id}/coverage/rehearse",
                payload)


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

    # 1. 两台恒等映射设备（本地毫秒即 UTC），各录一段 [0,10000)
    call(base, "POST", "/devices", {"device_id": "RH-D1", "name": "录音笔-甲"})
    call(base, "POST", "/devices", {"device_id": "RH-D2", "name": "录音笔-乙"})
    for d in ("RH-D1", "RH-D2"):
        call(base, "POST", f"/devices/{d}/anchors",
             {"local_ms": 0, "utc_ms": 0})
        call(base, "POST", f"/devices/{d}/anchors",
             {"local_ms": 100000, "utc_ms": 100000})
    call(base, "POST", "/segments",
         {"capture_id": "RH-C1", "device_id": "RH-D1",
          "local_start_ms": 0, "local_end_ms": 10000})
    call(base, "POST", "/segments",
         {"capture_id": "RH-C2", "device_id": "RH-D2",
          "local_start_ms": 0, "local_end_ms": 10000})

    # 2. 预演：拟把 RH-D1 锚点整体平移 +4000ms，窗口 [0,10000) 门槛 2
    #    更正前整段两台达标；更正后 RH-C1 投影到 [4000,14000)，
    #    [0,4000) 只剩 RH-D2 -> 连续恶化区间 [0,4000)，worsened=true
    shifted = [{"local_ms": 0, "utc_ms": 4000},
               {"local_ms": 100000, "utc_ms": 104000}]
    show("预演会恶化（+4000ms，门槛 2）",
         *rehearse(base, "RH-D1", shifted, 0, 10000, 2))

    # 3. 预演：拟替换锚点与当前一致 -> 两侧相同，无恶化区间
    identity = [{"local_ms": 0, "utc_ms": 0},
                {"local_ms": 100000, "utc_ms": 100000}]
    show("预演不恶化（锚点不变）",
         *rehearse(base, "RH-D1", identity, 0, 10000, 2))

    # 4. 门槛改为 3：两侧全程只有两台，原本就不达标，不产生恶化区间
    show("门槛 3：原本就不达标，无 regressions",
         *rehearse(base, "RH-D1", shifted, 0, 10000, 3))

    # 5. 明确拒绝（只读，均不改动数据）
    stale_fp = fingerprint(base, "RH-D1")
    call(base, "POST", "/devices/RH-D1/anchors",
         {"local_ms": 50000, "utc_ms": 50000})  # 晚到锚点 -> 指纹过期
    show("指纹过期 -> 409 rehearsal_fingerprint_stale",
         *rehearse(base, "RH-D1",
                   [{"local_ms": 0, "utc_ms": 4000},
                    {"local_ms": 50000, "utc_ms": 54000},
                    {"local_ms": 100000, "utc_ms": 104000}],
                   0, 10000, 2, fp=stale_fp))

    fp = fingerprint(base, "RH-D1")
    show("锚点不严格递增 -> 400 anchors_not_increasing",
         *rehearse(base, "RH-D1",
                   [{"local_ms": 0, "utc_ms": 100},
                    {"local_ms": 5000, "utc_ms": 100}],
                   0, 10000, 2, fp=fp))
    show("空锚点列表 -> 400 validation_error",
         *rehearse(base, "RH-D1", [], 0, 10000, 2, fp=fp))
    show("起点不小于终点 -> 400 invalid_range",
         *rehearse(base, "RH-D1", identity, 10000, 10000, 2, fp=fp))
    show("门槛非正整数 -> 400 invalid_threshold",
         *rehearse(base, "RH-D1", identity, 0, 10000, 0, fp=fp))
    show("非法时间（负数） -> 400 invalid_time",
         *rehearse(base, "RH-D1",
                   [{"local_ms": -1, "utc_ms": 0}], 0, 10000, 1, fp=fp))
    show("设备不存在 -> 404 device_not_found",
         *rehearse(base, "NOPE", identity, 0, 10000, 1, fp="0" * 64))
    show("周期不存在 -> 404 cycle_not_found",
         *call(base, "POST",
               "/devices/RH-D1/cycles/boot-x/coverage/rehearse",
               {"fingerprint": fp, "anchors": identity,
                "start_ms": 0, "end_ms": 10000, "min_devices": 1}))

    # 6. 封存后预演同样拒绝（409 cycle_sealed）
    seal_fp = fingerprint(base, "RH-D1")
    call(base, "POST", "/devices/RH-D1/cycles/default/seal",
         {"fingerprint": seal_fp})
    show("周期已封存 -> 409 cycle_sealed",
         *rehearse(base, "RH-D1", identity, 0, 10000, 2, fp=seal_fp))


if __name__ == "__main__":
    main()
