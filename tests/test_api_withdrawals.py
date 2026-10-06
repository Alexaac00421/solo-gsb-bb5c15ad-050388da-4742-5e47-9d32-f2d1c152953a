"""已受理交接批次撤回的端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 成功撤回：201 + 撤回链推进；原交接记录/批次报告/record_hash 不改写、
  原链长度与链头不变；保管人/封签/last_time/有效链视图回滚
- 多批次链上撤回最新后再撤次新（链式回滚到登记基线）
- 拒绝：404（展品/批次/跨展品批次）、422（原因空）、
  409（REJECTED 批次 / 更晚受理批次 / 已被撤回 / 被补正引用）
- 幂等：同键同载荷重放（200），同键异载荷 409；重放优先于业务校验
- 撤回后可重新提交新批次：原链在尾部继续追加，物理链不断，有效视图正确
- 查询：交接列表 withdrawn 标记与 active_chain_length；单环节标记；
  批次历史 withdrawn 标记；撤回链列表
- 审计：三链复算；篡改原记录/撤回记录/直接改写展品状态均可被定位

数据库不可用时整个模块自动 skip。
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid

import psycopg

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dev 依赖缺失
    TestClient = None

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/seals"
)


def _db_available() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


D1 = "sha256:" + "ab" * 32
D2 = "sha256:" + "cd" * 32
D3 = "sha256:" + "ef" * 32
D4 = "sha256:" + "12" * 32


def _rec(exhibit_id, frm, to, digest, when, seal="SEAL-1", home=None, reason=None):
    body = {
        "location": home or "展厅", "from_person": frm, "to_person": to,
        "seal_no": seal, "occurred_at": when, "voucher_digest": digest,
    }
    if reason:
        body["seal_change_reason"] = reason
    return {"exhibit_id": exhibit_id, "records": [body]}


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestWithdrawalsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app.db as db
        db.DATABASE_URL = DATABASE_URL
        import app.main as main
        main.DATABASE_URL = DATABASE_URL
        cls.main = main
        cls.client_cm = TestClient(main.app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def setUp(self):
        # 每个用例一件新展品 + 两个已受理批次：
        # 批1 张伟->李静（库房A，2026-09-01）；批2 李静->王强（2026-09-02）
        self.ex_id = f"EX-WD-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex_id, "name": "撤回测试展品",
            "home_location": "库房A", "custodian": "张伟",
            "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.b1 = self._submit(_rec(
            self.ex_id, "张伟", "李静", D1, "2026-09-01T09:00:00+08:00", home="库房A"))
        self.b2 = self._submit(_rec(
            self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"))

    # -- 辅助 ----------------------------------------------------------------

    def _submit(self, body, key=None, token=None):
        headers = {"Idempotency-Key": key or f"k-{uuid.uuid4()}"}
        if token:
            headers["Handover-Token"] = token
        r = self.client.post("/api/v1/handover-batches", json=body, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ACCEPTED", r.text
        return uuid.UUID(r.json()["batch_id"])

    def _withdraw(self, batch_id, reason="发现批次凭证造假，撤回交接（批文 WD-2026-0001）",
                  key=None, exhibit_id=None):
        # 幂等键全局唯一；固定键自动加上展品后缀，避免跨用例/跨展品复用
        if key:
            key = f"{key}-{exhibit_id or self.ex_id}"
        headers = {"Idempotency-Key": key or f"w-{uuid.uuid4()}"}
        return self.client.post(
            f"/api/v1/exhibits/{exhibit_id or self.ex_id}/handover-batches/{batch_id}/withdraw",
            json={"reason": reason}, headers=headers,
        )

    def _state(self):
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _chain(self):
        return self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers").json()

    # -- 成功路径 ------------------------------------------------------------

    def test_withdraw_latest_batch_201_and_state_rollback(self):
        before = self._state()
        self.assertEqual(before["custodian"], "王强")
        self.assertEqual(before["current_seal"], "SEAL-1")
        self.assertEqual(before["chain_length"], 2)
        self.assertEqual(before["active_chain_length"], 2)
        self.assertEqual(before["withdrawal_chain_length"], 0)

        # 撤前记录原哈希快照
        chain_before = self._chain()
        rec1_before, rec2_before = chain_before["records"]

        r = self._withdraw(self.b2)
        self.assertEqual(r.status_code, 201, r.text)
        w = r.json()
        self.assertFalse(w["idempotent_replay"])
        self.assertEqual(w["exhibit_id"], self.ex_id)
        self.assertEqual(uuid.UUID(w["batch_id"]), self.b2)
        self.assertEqual(w["withdrawal_seq"], 1)
        self.assertEqual(w["prev_hash"], "WITHDRAWALS_GENESIS")
        self.assertEqual(len(w["record_hash"]), 64)
        # 锚点 = 被撤回批次（单记录批次）最后一环 record_hash
        self.assertEqual(w["anchor_batch_record_hash"], rec2_before["record_hash"])
        self.assertEqual(w["anchor_active_head_hash"], rec2_before["record_hash"])
        # 回滚快照 = 批次2 之前一环（批1）
        self.assertEqual(w["restore_chain_seq"], 1)
        self.assertEqual(w["restore_head_hash"], rec1_before["record_hash"])
        self.assertEqual(w["restore_custodian"], "李静")
        self.assertEqual(w["restore_seal"], "SEAL-1")

        after = self._state()
        # 保管人/封签/时间/有效视图回滚
        self.assertEqual(after["custodian"], "李静")
        self.assertEqual(after["current_seal"], "SEAL-1")
        self.assertEqual(after["active_chain_length"], 1)
        self.assertEqual(after["active_head_hash"], rec1_before["record_hash"])
        # 物理原链长度/链头保持不变
        self.assertEqual(after["chain_length"], 2)
        self.assertEqual(after["chain_head_hash"], before["chain_head_hash"])
        # 撤回链推进
        self.assertEqual(after["withdrawal_chain_length"], 1)
        self.assertEqual(after["withdrawal_head_hash"], w["record_hash"])

        # 原交接记录与 record_hash 不改写、不删除
        chain_after = self._chain()
        self.assertEqual(chain_after["chain_length"], 2)       # 原链历史长度不变
        self.assertEqual(chain_after["active_chain_length"], 1)
        (rec1_after, rec2_after) = chain_after["records"]
        self.assertEqual(rec1_after["record_hash"], rec1_before["record_hash"])
        self.assertEqual(rec2_after["record_hash"], rec2_before["record_hash"])
        self.assertFalse(rec1_after["withdrawn"])
        self.assertIsNone(rec1_after["withdrawal_id"])
        self.assertTrue(rec2_after["withdrawn"])
        self.assertEqual(uuid.UUID(rec2_after["withdrawal_id"]),
                         uuid.UUID(w["withdrawal_id"]))

        # 单环节查询也带撤回标记
        single = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers/2").json()
        self.assertTrue(single["withdrawn"])
        self.assertEqual(single["record_hash"], rec2_before["record_hash"])

    def test_withdraw_chain_drains_to_registration_baseline(self):
        # 连撤两批：最新的批2 先撤，再撤批1，最终回到登记基线
        r2 = self._withdraw(self.b2, key="wd-b2", reason="撤批2")
        self.assertEqual(r2.status_code, 201, r2.text)
        self.assertEqual(self._state()["custodian"], "李静")

        r1 = self._withdraw(self.b1, key="wd-b1", reason="撤批1")
        self.assertEqual(r1.status_code, 201, r1.text)
        w1 = r1.json()
        self.assertEqual(w1["withdrawal_seq"], 2)
        self.assertEqual(w1["prev_hash"], r2.json()["record_hash"])
        self.assertEqual(w1["restore_chain_seq"], 0)
        self.assertEqual(w1["restore_head_hash"], "GENESIS")
        self.assertEqual(w1["restore_custodian"], "张伟")
        self.assertEqual(w1["restore_seal"], "SEAL-1")

        st = self._state()
        self.assertEqual(st["custodian"], "张伟")
        self.assertEqual(st["current_seal"], "SEAL-1")
        self.assertIsNone(st["last_time"])
        self.assertEqual(st["active_chain_length"], 0)
        self.assertEqual(st["active_head_hash"], "GENESIS")
        # 物理链仍在
        self.assertEqual(st["chain_length"], 2)
        self.assertEqual(st["withdrawal_chain_length"], 2)

    def test_withdraw_preserves_seal_and_custodian_rollback_with_seal_change(self):
        # 批3 换签：李静持 SEAL-1 -> 赵磊持 SEAL-9（带授权）；撤回后封签回到 SEAL-1
        body = _rec(self.ex_id, "王强", "赵磊", D3, "2026-09-03T09:00:00+08:00",
                    seal="SEAL-9", reason="运输磨损，馆长书面授权换签")
        b3 = self._submit(body)
        r = self._withdraw(b3)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["restore_seal"], "SEAL-1")
        st = self._state()
        self.assertEqual(st["current_seal"], "SEAL-1")
        self.assertEqual(st["custodian"], "王强")

    # -- 拒绝规则 ------------------------------------------------------------

    def test_unknown_exhibit_404(self):
        r = self.client.post(
            f"/api/v1/exhibits/EX-NOPE/handover-batches/{self.b2}/withdraw",
            json={"reason": "x"}, headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 404)

    def test_unknown_batch_404(self):
        r = self._withdraw(uuid.uuid4())
        self.assertEqual(r.status_code, 404, r.text)

    def test_batch_belonging_to_other_exhibit_404(self):
        # 另一件展品的批次对本展品而言不存在
        other = f"EX-WD-{uuid.uuid4().hex[:12]}"
        self.assertEqual(self.client.post("/api/v1/exhibits", json={
            "exhibit_id": other, "name": "另一件", "home_location": "库房B",
            "custodian": "陈晨", "initial_seal": "SEAL-2",
        }).status_code, 201)
        other_batch = self._submit(
            _rec(other, "陈晨", "周明", D1, "2026-09-01T10:00:00+08:00",
                 seal="SEAL-2", home="库房B"))
        r = self._withdraw(other_batch, exhibit_id=self.ex_id)
        self.assertEqual(r.status_code, 404)

    def test_empty_reason_422(self):
        for bad in ["", "   ", "\t\n"]:
            r = self._withdraw(self.b2, reason=bad, key=f"w-{uuid.uuid4()}")
            self.assertEqual(r.status_code, 422, f"reason={bad!r} -> {r.status_code}")
        self.assertEqual(self._state()["withdrawal_chain_length"], 0)

    def test_rejected_batch_cannot_withdraw_409(self):
        # 违规批次（人员断裂）-> REJECTED，不产生交接记录；撤回 -> 409
        bad = _rec(self.ex_id, "陌生人", "赵磊", D3, "2026-09-03T09:00:00+08:00")
        r = self.client.post("/api/v1/handover-batches", json=bad,
                             headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "REJECTED")
        rejected = uuid.UUID(r.json()["batch_id"])
        wr = self._withdraw(rejected)
        self.assertEqual(wr.status_code, 409, wr.text)

    def test_not_latest_accepted_batch_409(self):
        # 批2 是最新批次时撤批1 -> 409（存在更晚已受理批次）
        r = self._withdraw(self.b1)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("更晚", r.json()["detail"])
        self.assertEqual(self._state()["withdrawal_chain_length"], 0)

    def test_withdraw_already_withdrawn_409(self):
        self.assertEqual(self._withdraw(self.b2, key="wd-1").status_code, 201)
        again = self._withdraw(self.b2, key=f"w-{uuid.uuid4()}")
        self.assertEqual(again.status_code, 409, again.text)
        self.assertIn("已被撤回", again.json()["detail"])

    def test_batch_referenced_by_correction_409(self):
        # 对批2 的第 2 环补正，再撤批2 -> 409
        cr = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/handovers/2/digest-corrections",
            json={"expected_digest": D2, "new_digest": D3, "reason": "笔误"},
            headers={"Idempotency-Key": f"c-{uuid.uuid4()}"},
        )
        self.assertEqual(cr.status_code, 201, cr.text)
        wr = self._withdraw(self.b2)
        self.assertEqual(wr.status_code, 409, wr.text)
        self.assertIn("补正", wr.json()["detail"])

    def test_correcting_record_of_withdrawn_batch_409(self):
        # 先撤批2，再尝试补正第 2 环 -> 409（不变式：撤回批次无补正引用）
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        cr = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/handovers/2/digest-corrections",
            json={"expected_digest": D2, "new_digest": D3, "reason": "笔误"},
            headers={"Idempotency-Key": f"c-{uuid.uuid4()}"},
        )
        self.assertEqual(cr.status_code, 409, cr.text)

    # -- 幂等 ----------------------------------------------------------------

    def test_same_key_same_payload_replays_200(self):
        key = f"w-{uuid.uuid4()}"
        first = self._withdraw(self.b2, key=key)
        self.assertEqual(first.status_code, 201)
        wid = first.json()["withdrawal_id"]

        replay = self._withdraw(self.b2, key=key)
        self.assertEqual(replay.status_code, 200, replay.text)
        rb = replay.json()
        self.assertTrue(rb["idempotent_replay"])
        self.assertEqual(rb["withdrawal_id"], wid)
        self.assertEqual(self._state()["withdrawal_chain_length"], 1)

    def test_replay_takes_precedence_over_business_rules(self):
        # 键 K 撤批2 成功；之后批1 也被另一键撤回（此时批2 已撤，批1 成为最新）；
        # 再用键 K 重放撤批2：幂等重放优先，返回原记录而非 409（已撤回）
        key = f"w-{uuid.uuid4()}"
        first = self._withdraw(self.b2, key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self._withdraw(self.b1).status_code, 201)

        replay = self._withdraw(self.b2, key=key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["withdrawal_id"], first.json()["withdrawal_id"])

    def test_same_key_different_payload_409(self):
        key = f"w-{uuid.uuid4()}"
        self.assertEqual(self._withdraw(self.b2, reason="原因甲", key=key).status_code, 201)
        # 原因不同 -> 异载荷（即便目标批次已撤，同键异载荷仍 409）
        self.assertEqual(self._withdraw(self.b2, reason="原因乙", key=key).status_code, 409)
        # 批次不同 -> 异载荷：键先合法撤下当前最新的批1，再以同键撤批2（已撤）-> 409
        key2 = f"w-{uuid.uuid4()}"
        self.assertEqual(self._withdraw(self.b1, reason="原因甲", key=key2).status_code, 201)
        self.assertEqual(self._withdraw(self.b2, reason="原因甲", key=key2).status_code, 409)

    # -- 撤回后重新提交 ------------------------------------------------------

    def test_resubmit_after_withdraw_appends_to_original_chain(self):
        before = self._state()
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        mid = self._state()
        self.assertEqual(mid["active_chain_length"], 1)
        self.assertEqual(mid["chain_length"], 2)

        # 有效视图重新从批1 尾继续：李静 -> 赵磊，时间晚于批1
        body = _rec(self.ex_id, "李静", "赵磊", D4, "2026-09-10T09:00:00+08:00")
        token = self.client.post(
            "/api/v1/handover-batches/rehearse", json=body).json()["state_token"]["token"]
        r = self.client.post("/api/v1/handover-batches", json=body, headers={
            "Idempotency-Key": f"k-{uuid.uuid4()}", "Handover-Token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")

        st = self._state()
        self.assertEqual(st["custodian"], "赵磊")
        self.assertEqual(st["chain_length"], 3)           # 物理原链尾继续
        self.assertEqual(st["active_chain_length"], 2)    # 有效：批1 + 新批
        self.assertEqual(st["active_head_hash"], st["chain_head_hash"])
        self.assertEqual(st["withdrawal_chain_length"], 1)

        chain = self._chain()
        seqs = [(x["chain_seq"], x["withdrawn"], x["to_person"]) for x in chain["records"]]
        self.assertEqual(seqs, [
            (1, False, "李静"),
            (2, True, "王强"),    # 被撤回批次的记录仍在原链
            (3, False, "赵磊"),
        ])
        self.assertEqual(chain["active_chain_length"], 2)

    def test_resubmit_after_full_withdraw_revalidates_first_station(self):
        # 两批全撤 -> 有效链清零，新批次必须重新满足首站规则
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        self.assertEqual(self._withdraw(self.b1).status_code, 201)
        good = _rec(self.ex_id, "张伟", "李静", D1, "2026-10-01T09:00:00+08:00",
                    home="库房A")
        r = self.client.post("/api/v1/handover-batches", json=good,
                             headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")
        self.assertEqual(self._state()["active_chain_length"], 1)

        # 首站地点不符 -> REJECTED（链清零后首站规则重新生效）
        bad = _rec(self.ex_id, "张伟", "李静", D1, "2026-10-02T09:00:00+08:00",
                   home="错误地点")
        r2 = self.client.post("/api/v1/handover-batches", json=bad,
                              headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
        self.assertEqual(r2.json()["status"], "REJECTED")

    # -- 并发 ----------------------------------------------------------------

    def test_concurrent_withdraw_same_key_one_201_one_200(self):
        import threading
        barrier = threading.Barrier(2)
        results = []
        key = f"ck-{uuid.uuid4()}-{self.ex_id}"

        def worker(out):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                f"/api/v1/exhibits/{self.ex_id}/handover-batches/{self.b2}/withdraw",
                json={"reason": "同键并发撤回"}, headers={"Idempotency-Key": key})
            out.append((r.status_code, r.json()["withdrawal_id"]))
            client.close()

        t1 = threading.Thread(target=worker, args=(results,))
        t2 = threading.Thread(target=worker, args=(results,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(s for s, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self._state()["withdrawal_chain_length"], 1)

    def test_concurrent_withdraw_different_keys_only_one_succeeds(self):
        import threading
        barrier = threading.Barrier(2)
        results = []

        def worker(out):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                f"/api/v1/exhibits/{self.ex_id}/handover-batches/{self.b2}/withdraw",
                json={"reason": "异键并发撤回"},
                headers={"Idempotency-Key": f"dk-{uuid.uuid4()}"})
            out.append(r.status_code)
            client.close()

        t1 = threading.Thread(target=worker, args=(results,))
        t2 = threading.Thread(target=worker, args=(results,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertCountEqual(results, [201, 409])
        self.assertEqual(self._state()["withdrawal_chain_length"], 1)

    # -- 查询 ----------------------------------------------------------------

    def test_list_withdrawals(self):
        w2 = self._withdraw(self.b2, key="w2")
        w1 = self._withdraw(self.b1, key="w1")
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertEqual([x["withdrawal_seq"] for x in rows], [1, 2])
        self.assertEqual(uuid.UUID(rows[0]["batch_id"]), self.b2)
        self.assertEqual(uuid.UUID(rows[1]["batch_id"]), self.b1)
        self.assertFalse(rows[0]["idempotent_replay"])

        nf = self.client.get("/api/v1/exhibits/EX-NOPE/batch-withdrawals")
        self.assertEqual(nf.status_code, 404)

    def test_batches_listing_marks_withdrawn(self):
        self.assertEqual(self._withdraw(self.b2).status_code, 201)
        rows = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches").json()
        by_id = {r["batch_id"]: r for r in rows}
        self.assertFalse(by_id[str(self.b1)]["withdrawn"])
        self.assertTrue(by_id[str(self.b2)]["withdrawn"])
        self.assertTrue(by_id[str(self.b2)]["withdrawal_id"])

    # -- 审计 ----------------------------------------------------------------

    def test_audit_valid_three_chains(self):
        self._withdraw(self.b2)
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertTrue(body["valid"], body["detail"])
        self.assertEqual(body["chain_length"], 2)
        self.assertEqual(body["active_chain_length"], 1)
        self.assertEqual(body["withdrawal_chain_length"], 1)
        self.assertTrue(body["handover_chain"]["valid"])
        self.assertTrue(body["corrections_chain"]["valid"])
        self.assertTrue(body["withdrawals_chain"]["valid"])

    def test_audit_valid_after_chain_drained(self):
        self._withdraw(self.b2, key="a")
        self._withdraw(self.b1, key="b")
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertTrue(body["valid"], body["detail"])
        self.assertEqual(body["active_chain_length"], 0)
        self.assertEqual(body["active_head_hash"], "GENESIS")

    def test_audit_detects_tampered_handover_record_after_withdrawal(self):
        self._withdraw(self.b2)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE handover_records SET voucher_digest = %s "
                "WHERE exhibit_id = %s AND chain_seq = 2",
                (D3, self.ex_id))
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertFalse(body["valid"])
        # 交接链断裂 + 撤回锚定失效
        self.assertFalse(body["handover_chain"]["valid"])
        wb = body["withdrawals_chain"]
        self.assertFalse(wb["valid"])
        self.assertEqual(wb["first_invalid_seq"], 1)
        self.assertIn("锚定失效", wb["detail"])

    def test_audit_detects_tampered_withdrawal_record(self):
        self._withdraw(self.b2)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE batch_withdrawals SET reason = '被改写的原因' "
                "WHERE exhibit_id = %s AND withdrawal_seq = 1",
                (self.ex_id,))
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertFalse(body["valid"])
        self.assertTrue(body["handover_chain"]["valid"])
        wb = body["withdrawals_chain"]
        self.assertFalse(wb["valid"])
        self.assertEqual(wb["first_invalid_seq"], 1)

    def test_audit_detects_direct_state_overwrite(self):
        # 撤回成功后直接把保管人改成别人（绕过撤回链）：审计须识别有效视图不一致
        self._withdraw(self.b2)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE exhibits SET custodian = '非法保管人' WHERE exhibit_id = %s",
                (self.ex_id,))
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertFalse(body["valid"])
        self.assertTrue(body["handover_chain"]["valid"])
        self.assertTrue(body["corrections_chain"]["valid"])
        wb = body["withdrawals_chain"]
        self.assertFalse(wb["valid"])
        self.assertIn("有效视图", wb["detail"])

    def test_audit_detects_deleted_withdrawal_ring(self):
        self._withdraw(self.b2, key="a")
        self._withdraw(self.b1, key="b")
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "DELETE FROM batch_withdrawals "
                "WHERE exhibit_id = %s AND withdrawal_seq = 1",
                (self.ex_id,))
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertFalse(body["valid"])
        self.assertEqual(body["withdrawals_chain"]["first_invalid_seq"], 2)

    def test_audit_detects_withdrawal_that_had_later_batch(self):
        # 直接伪造一条序号 1、指向批1 的撤回记录（当时批2 仍为有效最新批次）：
        # 自洽的哈希/锚点/回滚快照可越过其它检查，但审计业务复算须识别
        # 「撤回时批1 并非最新受理批次（批2 仍有效）」。
        from app.hashing import withdrawal_hash, WITHDRAWALS_GENESIS_HASH
        forged_key = f"forged-{uuid.uuid4()}"
        forged_id = uuid.uuid4()
        with psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn:
            rec1 = conn.execute(
                "SELECT record_hash FROM handover_records "
                "WHERE exhibit_id=%s AND chain_seq=1", (self.ex_id,)).fetchone()
            ins = conn.execute(
                """
                INSERT INTO batch_withdrawals
                  (withdrawal_id, idempotency_key, exhibit_id, batch_id, reason,
                   anchor_batch_record_hash, anchor_active_head_hash,
                   restore_chain_seq, restore_head_hash, restore_custodian, restore_seal,
                   withdrawal_seq, prev_hash, record_hash, request_hash, created_at)
                VALUES (%s, %s, %s, %s, '伪造撤批1', %s, %s,
                        0, 'GENESIS', '张伟', 'SEAL-1',
                        1, %s, %s, 'reqhash', now())
                RETURNING created_at
                """,
                (forged_id, forged_key, self.ex_id, str(self.b1),
                 rec1["record_hash"], rec1["record_hash"],
                 WITHDRAWALS_GENESIS_HASH, "0" * 64),
            ).fetchone()
            h = withdrawal_hash(
                prev_hash=WITHDRAWALS_GENESIS_HASH, withdrawal_id=forged_id,
                exhibit_id=self.ex_id, withdrawal_seq=1, batch_id=self.b1,
                reason="伪造撤批1", anchor_batch_record_hash=rec1["record_hash"],
                anchor_active_head_hash=rec1["record_hash"],
                restore_chain_seq=0, restore_head_hash="GENESIS",
                restore_custodian="张伟", restore_seal="SEAL-1",
                created_at=ins["created_at"],
            )
            conn.execute(
                "UPDATE batch_withdrawals SET record_hash=%s WHERE idempotency_key=%s",
                (h, forged_key))
            conn.execute(
                "UPDATE exhibits SET withdrawal_head_hash=%s, withdrawal_chain_length=1 "
                "WHERE exhibit_id=%s",
                (h, self.ex_id))
            conn.commit()
        body = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertFalse(body["valid"])
        wb = body["withdrawals_chain"]
        self.assertFalse(wb["valid"])
        self.assertEqual(wb["first_invalid_seq"], 1)
        self.assertIn("并非最新受理批次", wb["detail"])


if __name__ == "__main__":
    unittest.main()
