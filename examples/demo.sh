#!/usr/bin/env bash
# 时间轴对齐服务 curl 调用示例。
# 用法：先启动服务，再执行 bash examples/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
pp() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }

echo "== 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 注册设备 =="
curl -s -X POST "$BASE/devices" -H 'Content-Type: application/json' \
  -d '{"device_id":"D1","name":"录音笔-甲"}' | pp
curl -s -X POST "$BASE/devices" -H 'Content-Type: application/json' \
  -d '{"device_id":"D2","name":"录音笔-乙"}' | pp

echo; echo "== 录入同步锚点（本地毫秒 <-> UTC 毫秒）=="
curl -s -X POST "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":1000,"utc_ms":1700000000000}' | pp
curl -s -X POST "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":61000,"utc_ms":1700000060500}' | pp
curl -s -X POST "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":0,"utc_ms":1699999997000}' | pp
curl -s -X POST "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":60000,"utc_ms":1700000057000}' | pp

echo; echo "== 锚点 UTC 不递增 -> 400 拒绝 =="
curl -s -X POST "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":30000,"utc_ms":1700000000000}' | pp

echo; echo "== 提交声段 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20000,"note":"鸟鸣"}' | pp
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0002","device_id":"D2","local_start_ms":3000,"local_end_ms":9000}' | pp
echo "-- 越界声段 -> 无法定位 --"
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0003","device_id":"D1","local_start_ms":90000,"local_end_ms":95000}' | pp

echo; echo "== 同编号同内容重传 -> 幂等 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20000,"note":"鸟鸣"}' | pp

echo; echo "== 同编号异内容 -> 409 冲突 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20001}' | pp

echo; echo "== 起止倒置 -> 400 拒绝 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0004","device_id":"D1","local_start_ms":9000,"local_end_ms":1000}' | pp

echo; echo "== 当前时间轴（按 UTC 起点、设备、采集编号排序）=="
curl -s "$BASE/timeline/current" | pp

echo; echo "== 发布版本 v1 =="
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' -d '{"note":"初始对齐"}' | pp

echo; echo "== 锚点更正（D1 UTC 基准 +250ms，整体替换）=="
curl -s -X PUT "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"anchors":[{"local_ms":1000,"utc_ms":1700000000250},{"local_ms":61000,"utc_ms":1700000060750}]}' | pp

echo; echo "== 发布版本 v2（changes 中含位移）=="
curl -s -X POST "$BASE/versions" | pp

echo; echo "== 回看 v1：已发布版本保持不变 =="
curl -s "$BASE/versions/1" | pp

echo; echo "== 版本列表 =="
curl -s "$BASE/versions" | pp

# ----------------------------------------------------------------------
# 时钟周期（cycle）：设备重启后本地毫秒归零，可登记新周期复用 local_ms
# ----------------------------------------------------------------------

echo; echo "== 登记新周期（设备 D2 重启后的新一轮，本地毫秒重新从 0 起算）=="
curl -s -X POST "$BASE/devices/D2/cycles" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","note":"第二次开机"}' | pp
echo "-- 周期列表（注册设备时自动带 default 周期）--"
curl -s "$BASE/devices/D2/cycles" | pp

echo; echo "== 新周期录入锚点：local_ms 与 default 周期可重复 =="
curl -s -X POST "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","local_ms":0,"utc_ms":1700010000000}' | pp
curl -s -X POST "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","local_ms":60000,"utc_ms":1700010060000}' | pp

echo; echo "== 同设备同 local_ms 在两个周期各有一个锚点 =="
curl -s "$BASE/devices/D2/anchors" | pp
echo "-- 只看某一周期：?cycle_id=boot-2 --"
curl -s "$BASE/devices/D2/anchors?cycle_id=boot-2" | pp

echo; echo "== 声段指定周期：相同 local 起止，只用所属周期锚点映射 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0101","device_id":"D2","cycle_id":"boot-2","local_start_ms":3000,"local_end_ms":9000,"note":"重启后录制"}' | pp

echo; echo "== 同一采集编号换周期重传 -> 409（周期计入内容）=="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0002","device_id":"D2","cycle_id":"boot-2","local_start_ms":3000,"local_end_ms":9000}' | pp

echo; echo "== 周期不存在 -> 404 cycle_not_found，且不写入 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-9999","device_id":"D2","cycle_id":"not-registered","local_start_ms":1,"local_end_ms":2}' | pp

echo; echo "== 当前时间轴（cycles/anchors/segments 均带周期归属）=="
curl -s "$BASE/timeline/current" | pp

echo; echo "== 发布 v3：原子冻结各周期锚点与声段投影（format_rev=2）=="
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' -d '{"note":"加入重启周期"}' | pp

echo; echo "== 只更正 boot-2 周期锚点（default 等其他周期不动）=="
curl -s -X PUT "$BASE/devices/D2/anchors" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","anchors":[{"local_ms":0,"utc_ms":1700010000300},{"local_ms":60000,"utc_ms":1700010060300}]}' | pp

echo; echo "== 发布 v4：changes 仅列出 boot-2 周期声段位移 =="
curl -s -X POST "$BASE/versions" | pp

echo; echo "== 回看 v3：各周期冻结不变 =="
curl -s "$BASE/versions/3" | pp

# ----------------------------------------------------------------------
# 周期归属批量纠正（reassign）：现场整理时发现声段记错时钟周期，按采集编号批量移动
# ----------------------------------------------------------------------

echo; echo "== 为 D1 登记第二周期（后续演示封存后纠正被拒）=="
curl -s -X POST "$BASE/devices/D1/cycles" -H 'Content-Type: application/json' \
  -d '{"cycle_id":"boot-2","note":"D1 第二次开机"}' | pp

echo; echo "== 批量纠正：CAP-0101 从 boot-2 移回 default（同设备已登记周期之间，整批原子）=="
curl -s -X POST "$BASE/segments/reassign-cycle" -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-0101","from_cycle_id":"boot-2","to_cycle_id":"default"}]}' | pp

echo; echo "== 纠正后同编号重传：新周期同内容 -> 幂等；按旧周期 -> 409 冲突 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0101","device_id":"D2","local_start_ms":3000,"local_end_ms":9000,"note":"重启后录制"}' | pp
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0101","device_id":"D2","cycle_id":"boot-2","local_start_ms":3000,"local_end_ms":9000,"note":"重启后录制"}' | pp

echo; echo "== 任一项无效 -> 整批拒绝不改动数据（编号不存在 / 原周期不符 / 目标未登记 / 原=目标）=="
curl -s -X POST "$BASE/segments/reassign-cycle" -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-0002","from_cycle_id":"default","to_cycle_id":"boot-2"},
                {"capture_id":"CAP-9999","from_cycle_id":"default","to_cycle_id":"boot-2"}]}' | pp
curl -s -X POST "$BASE/segments/reassign-cycle" -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-0002","from_cycle_id":"boot-2","to_cycle_id":"default"}]}' | pp
curl -s -X POST "$BASE/segments/reassign-cycle" -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-0002","from_cycle_id":"default","to_cycle_id":"ghost"}]}' | pp
curl -s -X POST "$BASE/segments/reassign-cycle" -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-0002","from_cycle_id":"default","to_cycle_id":"default"}]}' | pp

echo; echo "== 发布 v5：之后发布的版本反映新归属（已发布版本保持原快照）=="
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' -d '{"note":"纠正 CAP-0101 周期归属"}' | pp

# ----------------------------------------------------------------------
# 周期封存（seal）：现场核对完成后冻结单个周期，防止晚到记录改动已确认时间线
# ----------------------------------------------------------------------

echo; echo "== 周期详情：内容稳定指纹 + 封存状态 =="
curl -s "$BASE/devices/D1/cycles/default" | pp
FP=$(curl -s "$BASE/devices/D1/cycles/default" | python3 -c "import sys,json;print(json.load(sys.stdin)['cycle']['fingerprint'])")

echo; echo "== 存在不可定位声段（CAP-0003 越界）-> 409 拒绝封存，状态不变 =="
curl -s -X POST "$BASE/devices/D1/cycles/default/seal" -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP\"}" | pp

echo; echo "== 移除不可定位声段后，旧指纹即过期 -> 409 seal_fingerprint_stale =="
curl -s -X DELETE "$BASE/segments/CAP-0003" | pp
curl -s -X POST "$BASE/devices/D1/cycles/default/seal" -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP\"}" | pp

echo; echo "== 重新获取指纹并封存 -> 返回封存时间与最终指纹 =="
FP=$(curl -s "$BASE/devices/D1/cycles/default" | python3 -c "import sys,json;print(json.load(sys.stdin)['cycle']['fingerprint'])")
curl -s -X POST "$BASE/devices/D1/cycles/default/seal" -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP\"}" | pp

echo; echo "== 重复相同封存请求 -> 返回首次结果（幂等，封存时间不变）=="
curl -s -X POST "$BASE/devices/D1/cycles/default/seal" -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP\"}" | pp

echo; echo "== 封存后该周期锚点新增/更正/删除 -> 409 cycle_sealed =="
curl -s -X POST "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"local_ms":30000,"utc_ms":1700000030000}' | pp
curl -s -X PUT "$BASE/devices/D1/anchors" -H 'Content-Type: application/json' \
  -d '{"anchors":[{"local_ms":1000,"utc_ms":1700000000250}]}' | pp
curl -s -X DELETE "$BASE/devices/D1/anchors/1000" | pp

echo; echo "== 封存后该周期声段新增/删除 -> 409 cycle_sealed =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0005","device_id":"D1","local_start_ms":1000,"local_end_ms":2000}' | pp
curl -s -X DELETE "$BASE/segments/CAP-0001" | pp

echo; echo "== 封存后该周期声段的周期归属纠正 -> 409 cycle_sealed =="
curl -s -X POST "$BASE/segments/reassign-cycle" -H 'Content-Type: application/json' \
  -d '{"items":[{"capture_id":"CAP-0001","from_cycle_id":"default","to_cycle_id":"boot-2"}]}' | pp

echo; echo "== 同内容声段重传 -> 仍按幂等规则返回；异内容 -> 仍按冲突规则处理 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20000,"note":"鸟鸣"}' | pp
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0001","device_id":"D1","local_start_ms":5000,"local_end_ms":20001}' | pp

echo; echo "== 未封存周期（D2/boot-2）不受影响；封存后发布版本行为不变 =="
curl -s -X POST "$BASE/segments" -H 'Content-Type: application/json' \
  -d '{"capture_id":"CAP-0102","device_id":"D2","cycle_id":"boot-2","local_start_ms":10000,"local_end_ms":20000}' | pp
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' -d '{"note":"default 周期已封存"}' | pp

# ----------------------------------------------------------------------
# 覆盖审计（coverage audit）：判断指定 UTC 区间内是否始终有足够不同设备在录音
# ----------------------------------------------------------------------

echo; echo "== 覆盖审计：当前时间轴（半开区间 [start_ms,end_ms) + 最少设备数）=="
echo "-- 只计可定位声段；同设备重叠只算一台；按设备集合变化切最大片段；相邻缺口合并 --"
curl -s -X POST "$BASE/coverage/audit" -H 'Content-Type: application/json' \
  -d '{"start_ms":1699999997000,"end_ms":1700000062000,"min_devices":2}' | pp

echo; echo "== 覆盖审计：门槛 1 台（整段是否始终至少一台设备在录）=="
curl -s -X POST "$BASE/coverage/audit" -H 'Content-Type: application/json' \
  -d '{"start_ms":1699999997000,"end_ms":1700000062000,"min_devices":1}' | pp

echo; echo "== 覆盖审计：指定已发布版本（历史审计只依据冻结声段，后续变更不影响）=="
curl -s -X POST "$BASE/coverage/audit" -H 'Content-Type: application/json' \
  -d '{"start_ms":1699999997000,"end_ms":1700000062000,"min_devices":2,"version_no":1}' | pp

echo; echo "== 非法审计请求：起点不小于终点 / 门槛非正整数 / 版本不存在（均拒绝且不改数据）=="
curl -s -X POST "$BASE/coverage/audit" -H 'Content-Type: application/json' \
  -d '{"start_ms":2000,"end_ms":1000,"min_devices":1}' | pp
curl -s -X POST "$BASE/coverage/audit" -H 'Content-Type: application/json' \
  -d '{"start_ms":0,"end_ms":1000,"min_devices":0}' | pp
curl -s -X POST "$BASE/coverage/audit" -H 'Content-Type: application/json' \
  -d '{"start_ms":0,"end_ms":1000,"min_devices":1,"version_no":999}' | pp

# ----------------------------------------------------------------------
# 回放裁剪（playback clip）：按 UTC 半开窗口裁出可交给录音设备回放的片段
# ----------------------------------------------------------------------

echo; echo "== 回放裁剪：当前时间轴（半开窗口 [start_ms,end_ms)）=="
echo "-- 每条与窗口相交且可定位的声段返回本地裁剪起止与映射 UTC 起止；"
echo "-- 本地边界取'映射 UTC 首次不小于窗口边界'的整数毫秒，不存在则截在声段末端；"
echo "-- 裁后起止相同的不返回；同设备重叠声段分别返回 --"
curl -s -X POST "$BASE/segments/clip" -H 'Content-Type: application/json' \
  -d '{"start_ms":1699999997000,"end_ms":1700000062000}' | pp

echo; echo "== 回放裁剪：指定已发布版本（只使用发布时冻结的锚点与声段）=="
curl -s -X POST "$BASE/segments/clip" -H 'Content-Type: application/json' \
  -d '{"start_ms":1699999997000,"end_ms":1700000062000,"version_no":1}' | pp

echo; echo "== 非法裁剪请求：起点不小于终点 / 版本不存在（均拒绝且不改数据）=="
curl -s -X POST "$BASE/segments/clip" -H 'Content-Type: application/json' \
  -d '{"start_ms":2000,"end_ms":1000}' | pp
curl -s -X POST "$BASE/segments/clip" -H 'Content-Type: application/json' \
  -d '{"start_ms":0,"end_ms":1000,"version_no":999}' | pp

# ----------------------------------------------------------------------
# 现场时钟漂移审计（drift audit）：相邻锚点配对算 UTC/本地速率与漂移 ppm
# ----------------------------------------------------------------------

echo; echo "== 漂移审计：当前锚点（设备 D1 / 周期 default / 阈值 100 ppm）=="
echo "-- 逐段返回本地与 UTC 跨度、UTC/本地速率、有符号漂移 ppm；"
echo "-- |漂移| 超过阈值的区间进入 anomalies（含原因与偏快/偏慢方向）；"
echo "-- 并给出首尾锚点、总跨度与 max_abs_drift_ppm --"
curl -s -X POST "$BASE/devices/D1/cycles/default/drift-audit" \
  -H 'Content-Type: application/json' -d '{"max_drift_ppm":100}' | pp

echo; echo "== 漂移审计：放宽到 10000 ppm（同样数据 ok=true）=="
curl -s -X POST "$BASE/devices/D1/cycles/default/drift-audit" \
  -H 'Content-Type: application/json' -d '{"max_drift_ppm":10000}' | pp

echo; echo "== 漂移审计：指定已发布版本（只用发布时冻结的锚点，旧版周期归属按 default）=="
curl -s -X POST "$BASE/devices/D1/cycles/default/drift-audit" \
  -H 'Content-Type: application/json' \
  -d '{"max_drift_ppm":100,"version_no":1}' | pp

echo; echo "== 非法漂移审计请求（只读，均不改数据）=="
echo "-- 锚点少于两个 -> 400 insufficient_anchors --"
curl -s -X POST "$BASE/devices" -H 'Content-Type: application/json' \
  -d '{"device_id":"D3"}' >/dev/null
curl -s -X POST "$BASE/devices/D3/cycles/default/drift-audit" \
  -H 'Content-Type: application/json' -d '{"max_drift_ppm":100}' | pp
echo "-- 阈值为负/非数字 -> 400 invalid_threshold --"
curl -s -X POST "$BASE/devices/D1/cycles/default/drift-audit" \
  -H 'Content-Type: application/json' -d '{"max_drift_ppm":-5}' | pp
curl -s -X POST "$BASE/devices/D1/cycles/default/drift-audit" \
  -H 'Content-Type: application/json' -d '{"max_drift_ppm":"100"}' | pp
echo "-- 设备/周期/版本不存在 -> 404 --"
curl -s -X POST "$BASE/devices/NOPE/cycles/default/drift-audit" \
  -H 'Content-Type: application/json' -d '{"max_drift_ppm":100}' | pp
curl -s -X POST "$BASE/devices/D1/cycles/boot-x/drift-audit" \
  -H 'Content-Type: application/json' -d '{"max_drift_ppm":100}' | pp
curl -s -X POST "$BASE/devices/D1/cycles/default/drift-audit" \
  -H 'Content-Type: application/json' \
  -d '{"max_drift_ppm":100,"version_no":999}' | pp

# ----------------------------------------------------------------------
# 锚点更正覆盖预演（coverage rehearsal）：更正前预演覆盖会否恶化（只读）
# ----------------------------------------------------------------------

echo; echo "== 覆盖预演：取 D2/default 周期详情中的内容指纹 =="
FP2=$(curl -s "$BASE/devices/D2/cycles/default" | python3 -c "import sys,json;print(json.load(sys.stdin)['cycle']['fingerprint'])")
echo "D2/default fingerprint: $FP2"

echo; echo "== 预演：D2/default 锚点拟整体平移到 1.8e12（输出 before/after 与恶化区间）=="
echo "-- 同一当前快照内分别计算更正前/假设更正后的覆盖（其他周期与设备保持当前值）；"
echo "-- regressions 列出原先达标而预演后不达标的各个连续区间 --"
curl -s -X POST "$BASE/devices/D2/cycles/default/coverage-rehearsal" \
  -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP2\",\"anchors\":[{\"local_ms\":0,\"utc_ms\":1800000000000},{\"local_ms\":60000,\"utc_ms\":1800000060000}],\"start_ms\":1699999997000,\"end_ms\":1700000062000,\"min_devices\":2}" | pp

echo; echo "== 预演只读：当前时间轴不受影响（CAP-0002 投影不变）=="
curl -s "$BASE/segments/CAP-0002" | pp

echo; echo "== 非法预演请求（只读，均不改数据）=="
echo "-- 指纹与当前内容不一致（指纹过期）-> 409 fingerprint_stale --"
curl -s -X POST "$BASE/devices/D2/cycles/default/coverage-rehearsal" \
  -H 'Content-Type: application/json' \
  -d '{"fingerprint":"0000000000000000000000000000000000000000000000000000000000000000","anchors":[{"local_ms":0,"utc_ms":1699999997000},{"local_ms":60000,"utc_ms":1700000057000}],"start_ms":1699999997000,"end_ms":1700000062000,"min_devices":2}' | pp
echo "-- 锚点 UTC 不严格递增 -> 400 anchors_not_increasing --"
curl -s -X POST "$BASE/devices/D2/cycles/default/coverage-rehearsal" \
  -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP2\",\"anchors\":[{\"local_ms\":0,\"utc_ms\":1700000000000},{\"local_ms\":60000,\"utc_ms\":1700000000000}],\"start_ms\":1699999997000,\"end_ms\":1700000062000,\"min_devices\":2}" | pp
echo "-- 空锚点列表 -> 400 validation_error --"
curl -s -X POST "$BASE/devices/D2/cycles/default/coverage-rehearsal" \
  -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP2\",\"anchors\":[],\"start_ms\":1699999997000,\"end_ms\":1700000062000,\"min_devices\":2}" | pp
echo "-- 已封存周期（D1/default）-> 409 cycle_sealed --"
curl -s -X POST "$BASE/devices/D1/cycles/default/coverage-rehearsal" \
  -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP\",\"anchors\":[{\"local_ms\":1000,\"utc_ms\":1700000000250},{\"local_ms\":61000,\"utc_ms\":1700000060750}],\"start_ms\":1699999997000,\"end_ms\":1700000062000,\"min_devices\":2}" | pp
echo "-- 设备/周期不存在 -> 404 --"
curl -s -X POST "$BASE/devices/NOPE/cycles/default/coverage-rehearsal" \
  -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP2\",\"anchors\":[{\"local_ms\":0,\"utc_ms\":0}],\"start_ms\":0,\"end_ms\":1000,\"min_devices\":1}" | pp
curl -s -X POST "$BASE/devices/D2/cycles/boot-x/coverage-rehearsal" \
  -H 'Content-Type: application/json' \
  -d "{\"fingerprint\":\"$FP2\",\"anchors\":[{\"local_ms\":0,\"utc_ms\":0}],\"start_ms\":0,\"end_ms\":1000,\"min_devices\":1}" | pp
