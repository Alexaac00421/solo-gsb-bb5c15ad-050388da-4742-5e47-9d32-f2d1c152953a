#!/usr/bin/env bash
# 端到端调用示例：登记展品 -> 预演 -> 凭令牌提交 -> 幂等重试 -> 冲突检测 ->
# 提交违规批次（不落记录）-> 令牌失效与重新预演 -> 查询交接链 -> 哈希链审计校验 ->
# 凭证摘要补正（拒绝/冲突/幂等/独立补正链）-> 双链审计 ->
# 已受理批次撤回（拒绝规则/幂等/原链不动/撤后重交/三链审计）。
# 用法：BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; echo; fi; }
# 从响应中抽取状态令牌（有 jq 用 jq，无 jq 退化为 sed）
extract_token() {
  if [ -n "$JQ" ]; then jq -r '.state_token.token'; else
    sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\(stv1\.[^"]*\)".*/\1/p' | head -1; fi
}

EXHIBIT_ID="EX-DEMO-$(date +%s)-${RANDOM}"
KEY_OK="demo-ok-${EXHIBIT_ID}"
KEY_BAD="demo-bad-${EXHIBIT_ID}"
KEY_RH="demo-rh-${EXHIBIT_ID}"

# 补正用摘要（均为合法 sha256:<64hex>）
D_ORIG="sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"
D_NEW1="sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f00918273645564738291a0b1c2d3e4f50"
D_NEW2="sha256:5566778899aabbccddeeff00112233445566778899aabbccddeeff0011223344"
KEY_C1="demo-corr1-${EXHIBIT_ID}"
KEY_C2="demo-corr2-${EXHIBIT_ID}"

echo "== 0) 健康检查 =="
curl -s "${BASE}/health" | pp

echo; echo "== 1) 登记展品 ${EXHIBIT_ID}（馆藏地/保管人/初始封签为首站校验基准）=="
curl -s -X POST "${BASE}/api/v1/exhibits" \
  -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"name\":\"宋代青瓷莲花尊\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-NJ-0001\"}" | pp

echo; echo "== 2) 预演合规批次（只读核验：不落报告、不落记录、不推进链；返回状态令牌）=="
TOKEN="$(curl -s -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d @- <<JSON | extract_token
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON
)"
[ -n "${TOKEN}" ] || { echo "未取到预演令牌，终止"; exit 1; }
echo "已取得状态令牌：${TOKEN:0:40}..."

echo; echo "== 2b) 预演未知展品 -> 404 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-NOT-EXIST","records":[{"location":"x","from_person":"a","to_person":"b","seal_no":"S","occurred_at":"2026-09-01T09:00:00+08:00","voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"}]}'

echo; echo "== 3) 携带 Handover-Token 正式提交合规批次（链状态未变，正常受理）=="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -H "Handover-Token: ${TOKEN}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON

echo; echo "== 4) 同键同载荷（含令牌）重试：链已推进但仍返回原报告，idempotent_replay=true =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -H "Handover-Token: ${TOKEN}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON

echo; echo "== 5) 过期令牌再提交（链已推进）-> 409，需重新预演 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_RH}" \
  -H "Handover-Token: ${TOKEN}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院交接区","from_person":"陈晨","to_person":"赵磊","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00","voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"}]}'

echo; echo "== 6) 令牌格式无效 -> 400 参数错误 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_RH}" \
  -H 'Handover-Token: not-a-valid-token' \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院交接区","from_person":"陈晨","to_person":"赵磊","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00","voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"}]}'

echo; echo "== 7) 同幂等键但载荷不同 -> 409（令牌计入载荷身份）=="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"records\":[{\"location\":\"x\",\"from_person\":\"a\",\"to_person\":\"b\",\"seal_no\":\"S\",\"occurred_at\":\"2026-09-05T09:00:00+08:00\",\"voucher_digest\":\"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9\"}]}"

echo; echo "== 8) 提交违规批次（不带令牌，人员断裂+时间倒退+无授权换签+凭证非法）-> REJECTED =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_BAD}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"上海博物馆临时展厅","from_person":"陈晨","to_person":"赵磊",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00",
     "voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"},
    {"location":"西安博物院交接区","from_person":"孙倩","to_person":"周明",
     "seal_no":"SEAL-XA-2001","occurred_at":"2026-09-05T08:30:00+08:00",
     "voucher_digest":"not-a-valid-digest"}
  ]
}
JSON

echo; echo "== 9) 查询交接链（应有 3 环：409/400/REJECTED 均未写入任何记录）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp

echo; echo "== 10) 哈希链审计校验 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 11) 批次历史（含被驳回批次，供审计追溯；409/400 不产生报告）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batches" | pp

echo; echo "== 12) 补正第 3 环凭证摘要（原摘要 -> 新摘要）：201，锚定原记录 record_hash =="
curl -s -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp

echo; echo "== 13) 同键同载荷重试：返回原补正单（200, idempotent_replay=true）=="
curl -s -D /tmp/corr13.hdr -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp
grep -i '^HTTP/' /tmp/corr13.hdr

echo; echo "== 14) 旧预期值再补 -> 409（当前有效摘要已是 ${D_NEW1:0:18}...）=="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-corr-stale-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"再次补正\"}"

echo; echo "== 15) 以当前有效摘要为预期继续补正：201（补正链第 2 环，prev_hash 接第 1 环）=="
curl -s -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C2}" \
  -d "{\"expected_digest\":\"${D_NEW1}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"二次复核再更正（复核单 CR-2026-1002）\"}" | pp

echo; echo "== 16) 拒绝用例：目标不存在(404) / 原因空缺(422) / 新旧相同(422) / 同键异载荷(409) =="
curl -s -o /dev/null -w '目标链序号 999 不存在        -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/999/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-miss-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"x\"}"
curl -s -o /dev/null -w '原因空缺                     -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-empty-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"   \"}"
curl -s -o /dev/null -w '新旧摘要相同                 -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-same-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"x\"}"
curl -s -o /dev/null -w '同键异载荷                   -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"换一个载荷\"}"

echo; echo "== 17) 交接链查询：原摘要 voucher_digest 不变，current_voucher_digest 为最新，补正轨迹两环 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp

echo; echo "== 18) 单环节查询（原摘要 / 当前有效摘要 / 补正轨迹）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3" | pp

echo; echo "== 19) 补正链列表（可按链序号过滤）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/digest-corrections" | pp

echo; echo "== 20) 双链审计复算（交接链 + 补正链；补正不推进交接链，预演令牌不受影响）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 21) 幂等优先于现状：即便当前有效摘要已是 ${D_NEW2:0:14}... ，用 ${KEY_C1} 原载荷仍重放原单 =="
curl -s -D /tmp/corr21.hdr -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp
grep -i '^HTTP/' /tmp/corr21.hdr

# ================================================================
# 已受理交接批次撤回（独立展品，完整演示：撤回/拒绝/幂等/撤后重交/三链审计）
# ================================================================
WEX="EX-WD-DEMO-$(date +%s)-${RANDOM}"
echo; echo "== 22) 撤回演示：登记展品 ${WEX} 并连续提交两个已受理批次 =="
curl -s -X POST "${BASE}/api/v1/exhibits" \
  -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${WEX}\",\"name\":\"撤回演示展品\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-NJ-0001\"}" | pp

curl -s -o /dev/null -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-b1-${WEX}" \
  -d "{\"exhibit_id\":\"${WEX}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"张伟\",\"to_person\":\"李静\",\"seal_no\":\"SEAL-NJ-0001\",\"occurred_at\":\"2026-09-01T09:00:00+08:00\",\"voucher_digest\":\"sha256:$(printf 'ab%.0s' {1..32})\"}]}"
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-b2-${WEX}" \
  -d "{\"exhibit_id\":\"${WEX}\",\"records\":[{\"location\":\"上海博物馆交接区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-0001\",\"occurred_at\":\"2026-09-02T09:00:00+08:00\",\"voucher_digest\":\"sha256:$(printf 'cd%.0s' {1..32})\"}]}" > /tmp/wd-b2.json
# 按批次列表出现顺序取 id（无 jq 时按对象逐个截取，避免贪婪匹配）
WB1="$(curl -s "${BASE}/api/v1/exhibits/${WEX}/batches" | tr '}' '\n' | sed -n 's/.*"batch_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | sed -n '1p')"
WB2="$(cat /tmp/wd-b2.json | sed -n 's/.*"batch_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
echo "批1=${WB1}；批2（最新受理批次）=${WB2}"

echo; echo "== 23) 撤回非最新批次（批1，其后有批2）-> 409 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${WEX}/handover-batches/${WB1}/withdraw" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-w1-${WEX}" \
  -d '{"reason":"先撤早的批次"}'

echo; echo "== 24) 撤回原因为空 -> 422 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${WEX}/handover-batches/${WB2}/withdraw" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-empty-${WEX}" \
  -d '{"reason":"   "}'

echo; echo "== 25) 正式撤回最新受理批次批2：201，原链不动、追加撤回链一环 =="
curl -s -X POST "${BASE}/api/v1/exhibits/${WEX}/handover-batches/${WB2}/withdraw" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-w2-${WEX}" \
  -d @examples/withdraw.json | pp

echo; echo "== 26) 同键同载荷重试：返回原撤回记录（200, idempotent_replay=true）=="
curl -s -D /tmp/wd26.hdr -X POST "${BASE}/api/v1/exhibits/${WEX}/handover-batches/${WB2}/withdraw" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-w2-${WEX}" \
  -d @examples/withdraw.json | pp
grep -i '^HTTP/' /tmp/wd26.hdr

echo; echo "== 27) 重复撤回同批次 -> 409；同键异载荷（换原因）-> 409 =="
curl -s -o /dev/null -w '重复撤回             -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${WEX}/handover-batches/${WB2}/withdraw" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-again-${WEX}" \
  -d '{"reason":"再撤一次"}'
curl -s -o /dev/null -w '同键异载荷           -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${WEX}/handover-batches/${WB2}/withdraw" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-w2-${WEX}" \
  -d '{"reason":"换一个撤回原因"}'

echo; echo "== 28) 展品状态：保管人回滚为李静；物理链长=2 不变，有效链长=1 =="
curl -s "${BASE}/api/v1/exhibits/${WEX}" | pp

echo; echo "== 29) 交接列表：批2 记录仍在原链（withdrawn=true），active_chain_length=1 =="
curl -s "${BASE}/api/v1/exhibits/${WEX}/handovers" | pp

echo; echo "== 30) 撤回后重新提交：新批次在原链尾部继续追加（物理链长=3，有效链长=2）=="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: wd-demo-b3-${WEX}" \
  -d "{\"exhibit_id\":\"${WEX}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"赵磊\",\"seal_no\":\"SEAL-NJ-0001\",\"occurred_at\":\"2026-09-10T09:00:00+08:00\",\"voucher_digest\":\"sha256:$(printf 'ef%.0s' {1..32})\"}]}" > /dev/null
curl -s "${BASE}/api/v1/exhibits/${WEX}" | pp

echo; echo "== 31) 撤回链列表与批次历史（带 withdrawn 标记）=="
curl -s "${BASE}/api/v1/exhibits/${WEX}/batch-withdrawals" | pp
curl -s "${BASE}/api/v1/exhibits/${WEX}/batches" | pp

echo; echo "== 32) 三链审计复算（交接链 + 补正链 + 撤回链；valid=true）=="
curl -s "${BASE}/api/v1/exhibits/${WEX}/audit/verify" | pp

