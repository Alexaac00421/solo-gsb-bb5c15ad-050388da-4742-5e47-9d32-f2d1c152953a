"""HTTP API：文物借展封签交接核验服务。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query, Response
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from . import __version__
from .db import get_conn, wait_for_db
from .hashing import (
    CORRECTIONS_GENESIS_HASH,
    GENESIS_HASH,
    WITHDRAWALS_GENESIS_HASH,
    correction_hash,
    correction_request_hash,
    record_hash,
    request_hash,
    withdrawal_hash,
    withdrawal_request_hash,
)
from .migrations import ensure_schema
from .schemas import (
    AuditVerifyOut,
    BatchReport,
    BatchRequest,
    BatchSummary,
    ChainOut,
    CorrectionOut,
    CorrectionRequest,
    ExhibitCreate,
    ExhibitOut,
    HandoverRecordOut,
    RehearsalOut,
    WithdrawalOut,
    WithdrawalRequest,
)
from .tokens import (
    InvalidTokenError,
    TOKEN_SECRET,
    issue_token,
    parse_token,
    state_matches,
)
from .validation import ExhibitState, is_valid_voucher_digest, validate_batch


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    ensure_schema()
    yield


app = FastAPI(
    title="文物借展封签交接核验服务",
    version=__version__,
    lifespan=lifespan,
    description="校验借展交接链的首站、人员衔接、时间递增与封签连续性；"
                "幂等写入，失败批次不落任何交接记录，保留可查询的哈希审计链；"
                "支持凭证摘要补正（补正链）与已受理批次撤回（撤回链），"
                "原交接记录与哈希永不改写。",
)

# ---------------------------------------------------------------- 工具


def _exhibit_state(row: dict) -> ExhibitState:
    # 核验参照系取「有效视图」：撤回后 custodian/current_seal/last_time 已回滚，
    # 链长取有效链长（撤回全部批次后重新按首站规则校验）。
    return ExhibitState(
        exhibit_id=row["exhibit_id"],
        home_location=row["home_location"],
        custodian=row["custodian"],
        current_seal=row["current_seal"],
        last_time=row["last_time"],
        chain_length=row["active_chain_length"],
    )


def _serialize_items(result) -> list[dict]:
    return [
        {
            "seq": item.seq,
            "ok": item.ok,
            "reasons": [{"rule": r.rule, "message": r.message} for r in item.reasons],
        }
        for item in result.items
    ]


def _serialize_first_break(result) -> dict | None:
    fb = result.first_break
    if fb is None:
        return None
    seq, reason = fb
    return {"seq": seq, "rule": reason.rule, "message": reason.message}


def _build_report(batch_id: uuid.UUID, exhibit_id: str, result, record_count: int, now: datetime) -> dict:
    return {
        "batch_id": str(batch_id),
        "exhibit_id": exhibit_id,
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "record_count": record_count,
        "first_break": _serialize_first_break(result),
        "items": _serialize_items(result),
        "idempotent_replay": False,
        "created_at": now.isoformat(),
    }


def _replay_report(row: dict) -> dict:
    report = dict(row["report"])
    report["idempotent_replay"] = True
    return report


# ---------------------------------------------------------------- 补正


def _serialize_correction(row: dict, *, replay: bool) -> dict:
    return {
        "correction_id": str(row["correction_id"]),
        "exhibit_id": row["exhibit_id"],
        "target_chain_seq": row["target_chain_seq"],
        "expected_digest": row["expected_digest"],
        "old_digest": row["old_digest"],
        "new_digest": row["new_digest"],
        "reason": row["reason"],
        "anchor_record_hash": row["anchor_record_hash"],
        "correction_seq": row["correction_seq"],
        "prev_hash": row["prev_hash"],
        "record_hash": row["record_hash"],
        "idempotent_replay": replay,
        "created_at": row["created_at"].isoformat(),
    }


def _load_corrections_by_target(conn, exhibit_id: str) -> dict[int, list[dict]]:
    """按展品取全部补正单（补正时序），索引到目标链序号。"""
    rows = conn.execute(
        """
        SELECT correction_id, target_chain_seq, correction_seq, expected_digest,
               old_digest, new_digest, reason, anchor_record_hash, prev_hash,
               record_hash, created_at
          FROM digest_corrections
         WHERE exhibit_id = %s
         ORDER BY correction_seq
        """,
        (exhibit_id,),
    ).fetchall()
    trail: dict[int, list[dict]] = {}
    for row in rows:
        trail.setdefault(row["target_chain_seq"], []).append({
            "correction_id": str(row["correction_id"]),
            "correction_seq": row["correction_seq"],
            "target_chain_seq": row["target_chain_seq"],
            "expected_digest": row["expected_digest"],
            "old_digest": row["old_digest"],
            "new_digest": row["new_digest"],
            "reason": row["reason"],
            "anchor_record_hash": row["anchor_record_hash"],
            "record_hash": row["record_hash"],
            "idempotent_replay": False,
            "created_at": row["created_at"].isoformat(),
        })
    return trail


def _current_digest(original_digest: str, trail: list[dict] | None) -> str:
    """当前有效摘要：该环节最后一笔补正的 new_digest；无补正即原摘要。"""
    return trail[-1]["new_digest"] if trail else original_digest


# ---------------------------------------------------------------- 批次撤回


def _load_withdrawals_map(conn, exhibit_id: str) -> dict[str, dict]:
    """按展品取全部撤回记录，以 batch_id 为索引（供交接记录标记所属批次撤回状态）。"""
    rows = conn.execute(
        "SELECT * FROM batch_withdrawals WHERE exhibit_id = %s", (exhibit_id,)
    ).fetchall()
    return {str(row["batch_id"]): row for row in rows}


def _serialize_withdrawal(row: dict, *, replay: bool) -> dict:
    return {
        "withdrawal_id": str(row["withdrawal_id"]),
        "exhibit_id": row["exhibit_id"],
        "batch_id": str(row["batch_id"]),
        "reason": row["reason"],
        "anchor_batch_record_hash": row["anchor_batch_record_hash"],
        "anchor_active_head_hash": row["anchor_active_head_hash"],
        "restore_chain_seq": row["restore_chain_seq"],
        "restore_head_hash": row["restore_head_hash"],
        "restore_custodian": row["restore_custodian"],
        "restore_seal": row["restore_seal"],
        "withdrawal_seq": row["withdrawal_seq"],
        "prev_hash": row["prev_hash"],
        "record_hash": row["record_hash"],
        "idempotent_replay": replay,
        "created_at": row["created_at"].isoformat(),
    }


def _verify_handover_chain(conn, exhibit: dict, records: list[dict]) -> dict:
    """复算交接链（不改动任何数据）。

    返回审计块、计算链尾，以及按各环**存储的 prev_hash** 独立重算出的
    「诚实」record_hash（全量，不因链路在某环断裂而中断）——供补正链
    锚点校验逐环比对，即使交接链前段断裂，后段记录的内容篡改仍可定位。
    """
    honest: dict[int, str] = {}
    for row in records:
        honest[row["chain_seq"]] = record_hash(
            prev_hash=row["prev_hash"],
            exhibit_id=row["exhibit_id"],
            chain_seq=row["chain_seq"],
            batch_id=row["batch_id"],
            location=row["location"],
            from_person=row["from_person"],
            to_person=row["to_person"],
            seal_no=row["seal_no"],
            seal_change_reason=row["seal_change_reason"],
            occurred_at=row["occurred_at"],
            voucher_digest=row["voucher_digest"],
        )

    prev = GENESIS_HASH
    for row in records:
        expected = honest[row["chain_seq"]]
        if row["prev_hash"] != prev or row["record_hash"] != expected:
            return {
                "block": {
                    "chain_length": len(records),
                    "head_hash": prev,
                    "valid": False,
                    "first_invalid_seq": row["chain_seq"],
                    "detail": f"交接链第 {row['chain_seq']} 环哈希校验失败："
                              "记录被篡改、删除或顺序被调换",
                },
                "head": prev,
                "honest": honest,
            }
        prev = row["record_hash"]

    if prev != exhibit["chain_head_hash"] or len(records) != exhibit["chain_length"]:
        return {
            "block": {
                "chain_length": len(records),
                "head_hash": prev,
                "valid": False,
                "first_invalid_seq": None,
                "detail": "交接链尾哈希或链长与展品当前状态不一致",
            },
            "head": prev,
            "honest": honest,
        }
    return {
        "block": {
            "chain_length": len(records),
            "head_hash": prev,
            "valid": True,
            "first_invalid_seq": None,
            "detail": "交接哈希链完整，与展品当前状态一致",
        },
        "head": prev,
        "honest": honest,
    }


def _verify_corrections_chain(conn, exhibit: dict, handover_by_seq: dict[int, dict],
                              honest_hashes: dict[int, str]) -> dict:
    """复算补正链：逐环重算哈希并校验锚点仍诚实地指向被补正原记录。

    锚点（anchor_record_hash）必须同时：
    - 等于补正单落库时记录在案的原 record_hash（历史事实）；
    - 等于按当前交接记录业务字段复算出的「诚实」record_hash。
    因此原记录一旦被改写（哪怕有人连带伪造存储哈希列），
    首个引用它的补正环即判锚定失效。
    """
    rows = conn.execute(
        "SELECT * FROM digest_corrections WHERE exhibit_id = %s ORDER BY correction_seq",
        (exhibit["exhibit_id"],),
    ).fetchall()

    prev = CORRECTIONS_GENESIS_HASH
    for row in rows:
        expected = correction_hash(
            prev_hash=prev,
            correction_id=row["correction_id"],
            exhibit_id=row["exhibit_id"],
            correction_seq=row["correction_seq"],
            target_chain_seq=row["target_chain_seq"],
            expected_digest=row["expected_digest"],
            old_digest=row["old_digest"],
            new_digest=row["new_digest"],
            reason=row["reason"],
            anchor_record_hash=row["anchor_record_hash"],
            created_at=row["created_at"],
        )
        if row["prev_hash"] != prev or row["record_hash"] != expected:
            return {
                "chain_length": len(rows),
                "head_hash": prev,
                "valid": False,
                "first_invalid_seq": row["correction_seq"],
                "detail": f"补正链第 {row['correction_seq']} 环哈希校验失败："
                          "补正单被篡改、删除或顺序被调换",
            }

        stored = handover_by_seq.get(row["target_chain_seq"])
        honest = honest_hashes.get(row["target_chain_seq"])
        if (
            stored is None
            or honest is None
            or stored["record_hash"] != row["anchor_record_hash"]
            or honest != row["anchor_record_hash"]
        ):
            return {
                "chain_length": len(rows),
                "head_hash": prev,
                "valid": False,
                "first_invalid_seq": row["correction_seq"],
                "detail": f"补正链第 {row['correction_seq']} 环锚定失效："
                          f"被补正交接记录（链序号 {row['target_chain_seq']}）"
                          "不存在或其内容/record_hash 已被改写",
            }
        prev = row["record_hash"]

    if prev != exhibit["correction_head_hash"] or len(rows) != exhibit["correction_chain_length"]:
        return {
            "chain_length": len(rows),
            "head_hash": prev,
            "valid": False,
            "first_invalid_seq": None,
            "detail": "补正链尾哈希或链长与展品当前补正状态不一致",
        }
    return {
        "chain_length": len(rows),
        "head_hash": prev,
        "valid": True,
        "first_invalid_seq": None,
        "detail": "补正哈希链完整，各环锚点与原交接记录一致",
    }


def _verify_withdrawals_chain(
    conn,
    exhibit: dict,
    records: list[dict],
    handover_by_seq: dict[int, dict],
    honest_hashes: dict[int, str],
    withdrawn_batch_ids: set[str] | None = None,
) -> tuple[dict, set[str]]:
    """复算撤回链并校验其与原交接链/有效视图的一致性。

    每环撤回记录须同时满足：
    - 撤回哈希链衔接（prev_hash/record_hash 复算一致、链尾与展品状态一致）；
    - 批次锚点诚实：被撤回批次最后一环的存储 record_hash 与诚实重算一致，
      且等于记录在案的锚点——原交接记录被改写/删除即判锚定失效；
    - 撤回时该批次确为当前最新受理批次（其后不得存在任何受理批次，
      无论该更晚批次后来是否也被撤回）；
    - 被撤回批次各环节均未被补正引用；
    - 回滚快照与「该批次之前最近一环（或登记基线）」一致，
      且撤回时有效链尾正是被撤回批次末环；
    - 全部撤回环顺序应用后，由原链历史复算出的有效视图
      （有效链长/链尾/保管人/封签）与展品当前状态一致。

    原交接记录与批次报告全程只读；校验不依赖任何 UPDATE/DELETE。
    返回审计块与「已撤回批次 id 集合」（供交接列表标记与有效视图呈现）。
    """
    rows = conn.execute(
        "SELECT * FROM batch_withdrawals WHERE exhibit_id = %s ORDER BY withdrawal_seq",
        (exhibit["exhibit_id"],),
    ).fetchall()

    by_seq = {r["chain_seq"]: r for r in records}

    def block_invalid(seq, detail, *, head):
        return {
            "chain_length": len(rows),
            "head_hash": head,
            "valid": False,
            "first_invalid_seq": seq,
            "detail": detail,
        }

    # 已应用撤回的批次集合（按撤回时序逐步扩大）
    applied: set[str] = set()
    prev = WITHDRAWALS_GENESIS_HASH
    for row in rows:
        batch_id = str(row["batch_id"])

        expected = withdrawal_hash(
            prev_hash=prev,
            withdrawal_id=row["withdrawal_id"],
            exhibit_id=row["exhibit_id"],
            withdrawal_seq=row["withdrawal_seq"],
            batch_id=row["batch_id"],
            reason=row["reason"],
            anchor_batch_record_hash=row["anchor_batch_record_hash"],
            anchor_active_head_hash=row["anchor_active_head_hash"],
            restore_chain_seq=row["restore_chain_seq"],
            restore_head_hash=row["restore_head_hash"],
            restore_custodian=row["restore_custodian"],
            restore_seal=row["restore_seal"],
            created_at=row["created_at"],
        )
        if row["prev_hash"] != prev or row["record_hash"] != expected:
            return block_invalid(
                row["withdrawal_seq"],
                f"撤回链第 {row['withdrawal_seq']} 环哈希校验失败："
                "撤回记录被篡改、删除或顺序被调换",
                head=prev,
            ), applied

        # 1) 批次锚点：被撤回批次的交接记录必须存在且未被改写
        batch_records = sorted(
            (r for r in records if str(r["batch_id"]) == batch_id),
            key=lambda r: r["chain_seq"],
        )
        if not batch_records:
            return block_invalid(
                row["withdrawal_seq"],
                f"撤回链第 {row['withdrawal_seq']} 环锚定失效："
                f"被撤回批次「{batch_id}」不含任何交接记录（记录被删除或批次报告被改写）",
                head=prev,
            ), applied
        last_rec = batch_records[-1]
        if (
            last_rec["record_hash"] != row["anchor_batch_record_hash"]
            or honest_hashes.get(last_rec["chain_seq"]) != row["anchor_batch_record_hash"]
        ):
            return block_invalid(
                row["withdrawal_seq"],
                f"撤回链第 {row['withdrawal_seq']} 环锚定失效："
                f"被撤回批次「{batch_id}」的交接记录内容/record_hash 已被改写",
                head=prev,
            ), applied

        # 2) 撤回时该批次必须是最新**有效**受理批次：撤回受理时刻（created_at）
        #    之后才落库的交接记录（典型：撤后重新提交的新批次）不参与判定；
        #    时刻之前其后仍存在未撤回的受理批次即非法。本环之前已撤回的批次
        #    （applied）不再阻挡——先撤更晚批次、再撤次新批次是合法链式回滚。
        later_accepted = [
            r for r in records
            if (
                r["chain_seq"] > last_rec["chain_seq"]
                and str(r["batch_id"]) not in applied
                and r["created_at"] <= row["created_at"]
            )
        ]
        if later_accepted:
            first_later = min(later_accepted, key=lambda r: r["chain_seq"])
            return block_invalid(
                row["withdrawal_seq"],
                f"撤回链第 {row['withdrawal_seq']} 环业务失效："
                f"撤回时批次「{batch_id}」并非最新受理批次"
                f"（其后存在受理批次「{first_later['batch_id']}」）",
                head=prev,
            ), applied

        # 3) 被撤回批次各环节均未被补正引用
        corrected_seq = conn.execute(
            """
            SELECT target_chain_seq FROM digest_corrections
             WHERE exhibit_id = %s
               AND target_chain_seq = ANY(%s)
             ORDER BY target_chain_seq LIMIT 1
            """,
            (exhibit["exhibit_id"], [r["chain_seq"] for r in batch_records]),
        ).fetchone()
        if corrected_seq:
            return block_invalid(
                row["withdrawal_seq"],
                f"撤回链第 {row['withdrawal_seq']} 环业务失效："
                f"批次「{batch_id}」已被补正引用（链序号 "
                f"{corrected_seq['target_chain_seq']} 存在补正单），不得撤回",
                head=prev,
            ), applied

        # 4) 回滚快照复算：被撤回批次之前最近一环（或登记基线）
        prior = [
            r for r in records
            if r["chain_seq"] < batch_records[0]["chain_seq"]
            and str(r["batch_id"]) not in applied
        ]
        if prior:
            prev_rec = max(prior, key=lambda r: r["chain_seq"])
            exp_seq = prev_rec["chain_seq"]
            exp_head = prev_rec["record_hash"]
            exp_cust = prev_rec["to_person"]
            exp_seal = prev_rec["seal_no"]
        else:
            exp_seq = 0
            exp_head = GENESIS_HASH
            exp_cust = exhibit["registered_custodian"]
            exp_seal = exhibit["registered_seal"]

        if (
            row["restore_chain_seq"] != exp_seq
            or row["restore_head_hash"] != exp_head
            or row["restore_custodian"] != exp_cust
            or row["restore_seal"] != exp_seal
        ):
            return block_invalid(
                row["withdrawal_seq"],
                f"撤回链第 {row['withdrawal_seq']} 环回滚快照与原链历史不一致："
                f"记录在案回滚点（链序号 {row['restore_chain_seq']}/"
                f"保管人「{row['restore_custodian']}」/封签「{row['restore_seal']}」）"
                f"与按原链复算结果（链序号 {exp_seq}/"
                f"保管人「{exp_cust}」/封签「{exp_seal}」）不符，"
                "撤回记录或展品登记基线被篡改",
                head=prev,
            ), applied

        # 5) 撤回时有效链尾必须正是被撤回批次末环
        if row["anchor_active_head_hash"] != last_rec["record_hash"]:
            return block_invalid(
                row["withdrawal_seq"],
                f"撤回链第 {row['withdrawal_seq']} 环业务失效："
                "撤回受理时有效链尾与被撤回批次末环不一致",
                head=prev,
            ), applied

        applied.add(batch_id)
        prev = row["record_hash"]

    if prev != exhibit["withdrawal_head_hash"] or len(rows) != exhibit["withdrawal_chain_length"]:
        return {
            "chain_length": len(rows),
            "head_hash": prev,
            "valid": False,
            "first_invalid_seq": None,
            "detail": "撤回链尾哈希或链长与展品当前撤回状态不一致",
        }, applied

    # 6) 按时间线重放「交接/撤回」事件，复算当前有效视图，比对展品状态。
    #    交接记录按物理链序到达；撤回事件在 created_at 时刻把其批次的全部
    #    环节从有效视图移除。撤后重新提交的批次会在物理尾部再次延伸有效链。
    # 交接记录、撤回记录统一按落库时刻排序（同刻时交接先于撤回）
    events: list[tuple] = [
        (r["created_at"], 0, r["chain_seq"], r) for r in records
    ]
    events.extend((wd["created_at"], 1, None, wd) for wd in rows)
    events.sort(key=lambda e: (e[0], e[1]))

    # 批次 -> 其物理链序号集合
    batch_seqs: dict[str, list[int]] = {}
    for r in records:
        batch_seqs.setdefault(str(r["batch_id"]), []).append(r["chain_seq"])

    active_seq_set: set[int] = set()
    tail_rec = None
    for _, kind, _, payload in events:
        if kind == 0:
            active_seq_set.add(payload["chain_seq"])
            tail_rec = payload
        else:
            for s in batch_seqs.get(str(payload["batch_id"]), []):
                active_seq_set.discard(s)
            remaining = [by_seq[s] for s in sorted(active_seq_set)]
            tail_rec = remaining[-1] if remaining else None

    if tail_rec is not None:
        act_seq = len(active_seq_set)          # 有效链长 = 有效环节数
        act_head = tail_rec["record_hash"]
        act_cust = tail_rec["to_person"]
        act_seal = tail_rec["seal_no"]
        act_time = tail_rec["occurred_at"]
    else:
        act_seq = 0
        act_head = GENESIS_HASH
        act_cust = exhibit["registered_custodian"]
        act_seal = exhibit["registered_seal"]
        act_time = None

    state_mismatch = (
        exhibit["active_chain_length"] != act_seq
        or exhibit["active_head_hash"] != act_head
        or exhibit["custodian"] != act_cust
        or exhibit["current_seal"] != act_seal
        or exhibit["last_time"] != act_time
    )
    if state_mismatch:
        return {
            "chain_length": len(rows),
            "head_hash": prev,
            "valid": False,
            "first_invalid_seq": None,
            "detail": "撤回链完整，但展品当前有效视图（保管人/封签/有效链长/链尾）"
                      "与按原链及撤回记录复算结果不一致，展品状态被直接改写",
        }, applied

    return {
        "chain_length": len(rows),
        "head_hash": prev,
        "valid": True,
        "first_invalid_seq": None,
        "detail": "撤回哈希链完整，各环锚点与原交接记录一致，展品有效视图与回滚结果一致",
    }, applied


# ---------------------------------------------------------------- 健康检查


@app.get("/health", tags=["meta"])
def health():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------- 展品


@app.post("/api/v1/exhibits", response_model=ExhibitOut, status_code=201, tags=["exhibits"])
def create_exhibit(payload: ExhibitCreate):
    """登记展品（馆藏地/保管人/初始封签为首站与连续性校验的基准）。"""
    try:
        with get_conn() as conn:
            row = conn.execute(
                """
                INSERT INTO exhibits (exhibit_id, name, home_location, custodian, current_seal,
                                      registered_custodian, registered_seal)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (payload.exhibit_id, payload.name, payload.home_location, payload.custodian,
                 payload.initial_seal, payload.custodian, payload.initial_seal),
            ).fetchone()
            return row
    except UniqueViolation:
        raise HTTPException(status_code=409, detail=f"展品「{payload.exhibit_id}」已存在")


@app.get("/api/v1/exhibits/{exhibit_id}", response_model=ExhibitOut, tags=["exhibits"])
def get_exhibit(exhibit_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
    return row


# ---------------------------------------------------------------- 交接批次（核心）


@app.post("/api/v1/handover-batches/rehearse", response_model=RehearsalOut, tags=["handovers"])
def rehearse_handover_batch(payload: BatchRequest):
    """预演一批交接：只读核验，不写入批次报告、交接记录，也不推进展品状态。

    - 接受与正式提交完全相同的展品编号与按序记录，记录格式仍按现有规则校验
      （载荷格式非法时返回 422）。
    - 返回逐条核验结果、首个断点，以及绑定展品当前链状态的 `state_token`。
    - 未知展品返回 404。
    - 正式提交时可在 `Handover-Token` 头携带令牌；链状态未变化才被受理。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s",
            (payload.exhibit_id,),
        ).fetchone()
    if not exhibit:
        raise HTTPException(status_code=404, detail=f"展品「{payload.exhibit_id}」不存在")

    result = validate_batch(_exhibit_state(exhibit), payload.records)
    now = datetime.now(timezone.utc)
    token = issue_token(
        TOKEN_SECRET,
        payload.exhibit_id,
        exhibit["chain_length"],
        exhibit["chain_head_hash"],
        issued_at=int(now.timestamp()),
    )
    return {
        "exhibit_id": payload.exhibit_id,
        "status": "ACCEPTED" if result.accepted else "REJECTED",
        "record_count": len(payload.records),
        "first_break": _serialize_first_break(result),
        "items": _serialize_items(result),
        "rehearsed_at": now.isoformat(),
        "state_token": {
            "token": token,
            "exhibit_id": payload.exhibit_id,
            "chain_length": exhibit["chain_length"],
            "chain_head_hash": exhibit["chain_head_hash"],
            "issued_at": now.isoformat(),
        },
    }


@app.post("/api/v1/handover-batches", response_model=BatchReport, tags=["handovers"])
def submit_handover_batch(
    payload: BatchRequest,
    idempotency_key: str = Header(min_length=1, max_length=128),
    handover_token: str | None = Header(
        default=None,
        alias="Handover-Token",
        max_length=2048,
        description="可选：预演（/rehearse）签发的展品状态令牌",
    ),
):
    """提交一批交接记录并核验。

    - 幂等：携带 `Idempotency-Key` 请求头；同键同载荷重试返回同一报告
      （`idempotent_replay=true`），同键不同载荷返回 409。
      携带 `Handover-Token` 时令牌计入载荷身份；同键成功重试即使链已推进，
      也直接返回原报告。
    - 预演令牌（可选）：令牌格式无效 -> 400；跨展品或链状态已变化 -> 409，
      冲突时不生成批次报告，调用方须重新预演。不带令牌的提交行为不变。
    - 事务：核验通过则批次报告与全部交接记录一并写入并推进链上状态；
      核验失败仅落核验报告（供幂等重试与审计），不写入任何交接记录。
    """
    # 1) 令牌先验格式/签名（纯本地校验，400 参数错误），再在展品行锁上比对状态
    parsed_token = None
    if handover_token and handover_token.strip():
        try:
            parsed_token = parse_token(TOKEN_SECRET, handover_token)
        except InvalidTokenError as exc:
            raise HTTPException(status_code=400, detail=f"Handover-Token 无效：{exc}")

    records_for_hash = [r.model_dump(mode="json") for r in payload.records]
    req_hash = request_hash(payload.exhibit_id, records_for_hash, handover_token)

    try:
        with get_conn() as conn:
            # 2) 幂等预检（同键成功重试即使链已推进/令牌已过期，也返回原报告）
            existing = conn.execute(
                "SELECT * FROM batches WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的请求载荷",
                    )
                return _replay_report(existing)

            # 3) 锁定展品行，串行化同一展品的批次，保证链式状态一致
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (payload.exhibit_id,),
            ).fetchone()
            if not exhibit:
                if parsed_token is not None:
                    # 令牌签名有效但展品对不上：按跨展品状态冲突处理
                    raise HTTPException(
                        status_code=409,
                        detail=f"令牌属于展品「{parsed_token.exhibit_id}」，"
                               f"不能用于展品「{payload.exhibit_id}」，请重新预演",
                    )
                raise HTTPException(status_code=404, detail=f"展品「{payload.exhibit_id}」不存在")

            # 4) 令牌归属与链状态比对（行锁内完成，与链推进互斥）
            if parsed_token is not None:
                if parsed_token.exhibit_id != payload.exhibit_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"令牌属于展品「{parsed_token.exhibit_id}」，"
                               f"不能用于展品「{payload.exhibit_id}」，请重新预演",
                    )
                if not state_matches(
                    parsed_token, exhibit["chain_length"], exhibit["chain_head_hash"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="展品链状态自预演后已变化（哈希链已推进），"
                               "令牌失效，请重新预演后再提交",
                    )

            # 5) 核验
            result = validate_batch(_exhibit_state(exhibit), payload.records)
            batch_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            report = _build_report(batch_id, payload.exhibit_id, result, len(payload.records), now)

            conn.execute(
                """
                INSERT INTO batches (batch_id, idempotency_key, exhibit_id, request_hash, status, report, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (batch_id, idempotency_key, payload.exhibit_id, req_hash,
                 report["status"], Jsonb(report), now),
            )

            # 6) 通过才写交接记录并推进链上状态；失败则只有批次报告落库
            if result.accepted:
                # 物理链：原链历史只追加，链序号恒从物理链尾继续（撤回不删不改）
                prev_hash = exhibit["chain_head_hash"]
                prev_seal = exhibit["current_seal"]
                chain_seq = exhibit["chain_length"]
                for seq_in_batch, rec in enumerate(payload.records, start=1):
                    chain_seq += 1
                    h = record_hash(
                        prev_hash=prev_hash,
                        exhibit_id=payload.exhibit_id,
                        chain_seq=chain_seq,
                        batch_id=batch_id,
                        location=rec.location,
                        from_person=rec.from_person,
                        to_person=rec.to_person,
                        seal_no=rec.seal_no,
                        seal_change_reason=rec.seal_change_reason,
                        occurred_at=rec.occurred_at,
                        voucher_digest=rec.voucher_digest,
                    )
                    conn.execute(
                        """
                        INSERT INTO handover_records
                            (batch_id, exhibit_id, chain_seq, seq_in_batch, location,
                             from_person, to_person, seal_no, seal_changed, seal_change_reason,
                             occurred_at, voucher_digest, prev_hash, record_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (batch_id, payload.exhibit_id, chain_seq, seq_in_batch, rec.location,
                         rec.from_person, rec.to_person, rec.seal_no, rec.seal_no != prev_seal,
                         rec.seal_change_reason, rec.occurred_at, rec.voucher_digest, prev_hash, h),
                    )
                    prev_hash = h
                    prev_seal = rec.seal_no

                last = payload.records[-1]
                # 物理链长/链头恒等于原链全部记录（历史长度不变）；
                # active_* 为有效视图：在当前有效链长上累加本批环节数，
                # 有效链尾即本批末环哈希（跳过中间已撤回批次的记录）
                new_active_length = exhibit["active_chain_length"] + len(payload.records)
                conn.execute(
                    """
                    UPDATE exhibits
                       SET custodian = %s, current_seal = %s, last_time = %s,
                           chain_length = %s, chain_head_hash = %s,
                           active_chain_length = %s, active_head_hash = %s
                     WHERE exhibit_id = %s
                    """,
                    (last.to_person, last.seal_no, last.occurred_at,
                     chain_seq, prev_hash,
                     new_active_length, prev_hash, payload.exhibit_id),
                )

            return report
    except UniqueViolation:
        # 并发下同键插入：回滚后按已存在的批次返回（等价于后到的重试）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM batches WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            return _replay_report(existing)
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的请求载荷")


@app.get("/api/v1/handover-batches/{batch_id}", response_model=BatchReport, tags=["handovers"])
def get_handover_batch(batch_id: uuid.UUID):
    """按批次号查询核验报告。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM batches WHERE batch_id = %s", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"批次「{batch_id}」不存在")
    return row["report"]


# ---------------------------------------------------------------- 交接批次撤回


@app.post(
    "/api/v1/exhibits/{exhibit_id}/handover-batches/{batch_id}/withdraw",
    response_model=WithdrawalOut,
    status_code=201,
    tags=["withdrawals"],
)
def withdraw_handover_batch(
    exhibit_id: str,
    batch_id: uuid.UUID,
    payload: WithdrawalRequest,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128),
):
    """撤回一件展品**最新且已受理（ACCEPTED）、未被补正引用**的交接批次。

    调用方提交：展品编号（路径）、批次号（路径）、撤回原因（请求体）、幂等键（头）。

    - 撤回**不删除、不改写**任何原交接记录、批次报告及其 `record_hash`
      （原链长度/链头保持不变）；只向按展品独立成链的撤回哈希链追加一环。
    - 撤回受理后，展品当前保管人/封签/有效交接列表按撤回后状态计算
      （回滚到该批次之前最近一环或登记基线）。
    - 拒绝规则（写入前中止，不留撤回记录）：
      - 展品不存在 / 批次不存在 / 批次不属于该展品 -> 404；
      - 撤回原因为空（含纯空白）-> 422；
      - 批次状态为 REJECTED（被驳回，本就未入链）-> 409；
      - 存在更晚的已受理批次（目标不是最新受理批次）-> 409；
      - 批次已被撤回（撤回不可撤销）-> 409；
      - 批次任一环节已被补正引用 -> 409。
    - 幂等（先于一切业务校验按幂等键重放）：
      - 同键同载荷重试 -> 返回原撤回记录，`idempotent_replay=true`（HTTP 200），
        即使其后状态已变化也先按幂等键重放；
      - 同键异载荷 -> 409。
    """
    reason = payload.reason.strip()
    req_hash = withdrawal_request_hash(exhibit_id, batch_id, reason)

    try:
        with get_conn() as conn:
            # 1) 幂等预检：先按键重放，优先于一切业务校验
            existing = conn.execute(
                "SELECT * FROM batch_withdrawals WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的撤回请求载荷",
                    )
                response.status_code = 200
                return _serialize_withdrawal(existing, replay=True)

            # 2) 锁定展品行：与批次提交、补正、其他撤回互斥
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (exhibit_id,),
            ).fetchone()
            if not exhibit:
                raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")

            # 2b) 行锁内再查一次幂等（串行化同键并发）
            existing = conn.execute(
                "SELECT * FROM batch_withdrawals WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的撤回请求载荷",
                    )
                response.status_code = 200
                return _serialize_withdrawal(existing, replay=True)

            # 3) 目标批次必须存在且属于该展品
            batch = conn.execute(
                "SELECT * FROM batches WHERE batch_id = %s",
                (batch_id,),
            ).fetchone()
            if not batch or batch["exhibit_id"] != exhibit_id:
                raise HTTPException(
                    status_code=404,
                    detail=f"展品「{exhibit_id}」不存在批次「{batch_id}」",
                )

            # 4) 被驳回批次本就未入链，无链可撤
            if batch["status"] != "ACCEPTED":
                raise HTTPException(
                    status_code=409,
                    detail=f"批次「{batch_id}」状态为 REJECTED（已被驳回、未写入交接记录），不得撤回",
                )

            # 5) 已撤回的批次不得重复撤回（撤回不可撤销）
            already = conn.execute(
                "SELECT 1 FROM batch_withdrawals WHERE batch_id = %s",
                (batch_id,),
            ).fetchone()
            if already:
                raise HTTPException(
                    status_code=409,
                    detail=f"批次「{batch_id}」已被撤回；撤回记录不可撤销或覆盖",
                )

            batch_records = conn.execute(
                """
                SELECT * FROM handover_records
                 WHERE exhibit_id = %s AND batch_id = %s
                 ORDER BY chain_seq
                """,
                (exhibit_id, batch_id),
            ).fetchall()
            # ACCEPTED 批次必有交接记录；防御性处理（数据异常 -> 409，不留撤回）
            if not batch_records:
                raise HTTPException(
                    status_code=409,
                    detail=f"批次「{batch_id}」无交接记录，无法撤回",
                )
            last_rec = batch_records[-1]
            first_seq = batch_records[0]["chain_seq"]
            last_seq = last_rec["chain_seq"]

            # 6) 仅允许撤回当前最新的**有效**受理批次：其后不得存在未撤回的受理批次
            #    （更晚批次若也已撤回则不再阻挡；REJECTED 批次无交接记录）
            later = conn.execute(
                """
                SELECT b.batch_id
                  FROM handover_records h
                  JOIN batches b ON b.batch_id = h.batch_id
                  LEFT JOIN batch_withdrawals w ON w.batch_id = b.batch_id
                 WHERE h.exhibit_id = %s AND h.chain_seq > %s
                   AND b.status = 'ACCEPTED'
                   AND w.withdrawal_id IS NULL
                 ORDER BY h.chain_seq LIMIT 1
                """,
                (exhibit_id, last_seq),
            ).fetchone()
            if later:
                raise HTTPException(
                    status_code=409,
                    detail=f"批次「{batch_id}」不是展品「{exhibit_id}」最新的已受理批次"
                           f"（其后存在已受理批次「{later['batch_id']}」），请先撤回更晚的批次",
                )

            # 7) 批次任一环节被补正引用即不得撤回（保证补正锚点永远指向有效环节）
            referenced = conn.execute(
                """
                SELECT target_chain_seq FROM digest_corrections
                 WHERE exhibit_id = %s AND target_chain_seq BETWEEN %s AND %s
                 ORDER BY target_chain_seq LIMIT 1
                """,
                (exhibit_id, first_seq, last_seq),
            ).fetchone()
            if referenced:
                raise HTTPException(
                    status_code=409,
                    detail=f"批次「{batch_id}」链序号 {referenced['target_chain_seq']} "
                           "的交接记录已被补正单引用，不得撤回",
                )

            # 8) 复算回滚快照：被撤回批次之前最近一环（仅取未撤回批次的记录），
            #    无更早有效环节则回到登记基线
            prior = conn.execute(
                """
                SELECT h.* FROM handover_records h
                 LEFT JOIN batch_withdrawals w ON w.batch_id = h.batch_id
                 WHERE h.exhibit_id = %s AND h.chain_seq < %s AND w.withdrawal_id IS NULL
                 ORDER BY h.chain_seq DESC LIMIT 1
                """,
                (exhibit_id, first_seq),
            ).fetchone()
            if prior:
                restore_seq = prior["chain_seq"]
                restore_head = prior["record_hash"]
                restore_custodian = prior["to_person"]
                restore_seal = prior["seal_no"]
                restore_time = prior["occurred_at"]
            else:
                restore_seq = 0
                restore_head = GENESIS_HASH
                restore_custodian = exhibit["registered_custodian"]
                restore_seal = exhibit["registered_seal"]
                restore_time = None

            # 9) 追加撤回链一环（绝不触碰 handover_records/batches）
            withdrawal_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            withdrawal_seq = exhibit["withdrawal_chain_length"] + 1
            prev_hash = exhibit["withdrawal_head_hash"]
            anchor_head = last_rec["record_hash"]
            h = withdrawal_hash(
                prev_hash=prev_hash,
                withdrawal_id=withdrawal_id,
                exhibit_id=exhibit_id,
                withdrawal_seq=withdrawal_seq,
                batch_id=batch_id,
                reason=reason,
                anchor_batch_record_hash=anchor_head,
                anchor_active_head_hash=exhibit["active_head_hash"],
                restore_chain_seq=restore_seq,
                restore_head_hash=restore_head,
                restore_custodian=restore_custodian,
                restore_seal=restore_seal,
                created_at=now,
            )
            conn.execute(
                """
                INSERT INTO batch_withdrawals
                    (withdrawal_id, idempotency_key, exhibit_id, batch_id, reason,
                     anchor_batch_record_hash, anchor_active_head_hash,
                     restore_chain_seq, restore_head_hash,
                     restore_custodian, restore_seal,
                     withdrawal_seq, prev_hash, record_hash, request_hash, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (withdrawal_id, idempotency_key, exhibit_id, batch_id, reason,
                 anchor_head, exhibit["active_head_hash"],
                 restore_seq, restore_head, restore_custodian, restore_seal,
                 withdrawal_seq, prev_hash, h, req_hash, now),
            )
            # 有效视图回滚；物理 chain_length/chain_head_hash 不在 SET 中（原链历史长度不变）
            conn.execute(
                """
                UPDATE exhibits
                   SET custodian = %s, current_seal = %s, last_time = %s,
                       active_chain_length = %s, active_head_hash = %s,
                       withdrawal_chain_length = %s, withdrawal_head_hash = %s
                 WHERE exhibit_id = %s
                """,
                (restore_custodian, restore_seal, restore_time,
                 restore_seq, restore_head, withdrawal_seq, h, exhibit_id),
            )

            row = conn.execute(
                "SELECT * FROM batch_withdrawals WHERE withdrawal_id = %s",
                (withdrawal_id,),
            ).fetchone()
            return _serialize_withdrawal(row, replay=False)
    except UniqueViolation:
        # 并发同键/同批次：回滚后按已存在记录返回（等价后到的重试），异载荷 409
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM batch_withdrawals WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            response.status_code = 200
            return _serialize_withdrawal(existing, replay=True)
        if existing:
            raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的撤回请求载荷")
        raise HTTPException(status_code=409, detail="该批次已被并发撤回（撤回不可重复）")


@app.get(
    "/api/v1/exhibits/{exhibit_id}/batch-withdrawals",
    response_model=list[WithdrawalOut],
    tags=["withdrawals"],
)
def list_batch_withdrawals(exhibit_id: str):
    """查询展品的撤回链（按撤回时序返回；原交接记录与批次报告仍可经既有接口查询）。"""
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT 1 FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        rows = conn.execute(
            "SELECT * FROM batch_withdrawals WHERE exhibit_id = %s ORDER BY withdrawal_seq",
            (exhibit_id,),
        ).fetchall()
    return [_serialize_withdrawal(r, replay=False) for r in rows]


# ---------------------------------------------------------------- 凭证摘要补正


@app.post(
    "/api/v1/exhibits/{exhibit_id}/handovers/{chain_seq}/digest-corrections",
    response_model=CorrectionOut,
    status_code=201,
    tags=["corrections"],
)
def submit_digest_correction(
    exhibit_id: str,
    chain_seq: int,
    payload: CorrectionRequest,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128),
):
    """对已入链交接记录的凭证摘要提交补正单。

    - 原交接记录与其 `record_hash` 永不改写；补正按展品另成一条哈希链，
      每环锚定被补正原记录的 `record_hash`。
    - 拒绝规则：
      - 目标记录不存在（展品不存在 / 链序号越界）-> 404；
      - 目标记录所属批次已被撤回（不属于有效交接）-> 409；
      - 补正原因为空、新旧摘要格式/相同性不满足要求 -> 422；
      - `expected_digest` 与当前有效摘要不符 -> 409 冲突；
      - 不同幂等键以同一预期值并发补正，仅一笔生效，其余 409。
    - 幂等（先于一切业务校验按幂等键重放）：
      - 同键同载荷重试 -> 返回原补正单，`idempotent_replay=true`（HTTP 200），
        即使其后已发生新的补正也先按幂等键重放；
      - 同键异载荷 -> 409。
    - 补正不推进交接链、不改变交接链头，既有预演令牌继续有效。
    """
    if chain_seq < 1:
        raise HTTPException(status_code=422, detail="链序号 chain_seq 必须为正整数")
    if not is_valid_voucher_digest(payload.expected_digest):
        raise HTTPException(status_code=422, detail="expected_digest 格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>")
    if not is_valid_voucher_digest(payload.new_digest):
        raise HTTPException(status_code=422, detail="new_digest 格式非法，应为「算法:十六进制摘要」，如 sha256:<64位hex>")

    req_hash = correction_request_hash(
        exhibit_id, chain_seq, payload.expected_digest, payload.new_digest, payload.reason
    )

    try:
        with get_conn() as conn:
            # 1) 幂等预检：即使后续补正已发生，也先按幂等键重放原补正单
            existing = conn.execute(
                "SELECT * FROM digest_corrections WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的补正请求载荷",
                    )
                response.status_code = 200
                return _serialize_correction(existing, replay=True)

            # 2) 锁定展品行：串行化同一展品的补正（也与交接批次链推进互斥）
            exhibit = conn.execute(
                "SELECT * FROM exhibits WHERE exhibit_id = %s FOR UPDATE",
                (exhibit_id,),
            ).fetchone()
            if not exhibit:
                raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")

            # 2b) 行锁内再查一次幂等：同键并发请求在此被串行化，
            #     先到者已落单则后到者按重试重放（同载荷 200 / 异载荷 409）
            existing = conn.execute(
                "SELECT * FROM digest_corrections WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != req_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="同一 Idempotency-Key 提交了不同的补正请求载荷",
                    )
                response.status_code = 200
                return _serialize_correction(existing, replay=True)

            target = conn.execute(
                """
                SELECT * FROM handover_records
                 WHERE exhibit_id = %s AND chain_seq = %s
                """,
                (exhibit_id, chain_seq),
            ).fetchone()
            if not target:
                raise HTTPException(
                    status_code=404,
                    detail=f"展品「{exhibit_id}」不存在链序号 {chain_seq} 的交接记录，无可补正目标",
                )

            # 2c) 已撤回批次的环节不属于有效交接，不得再补正
            #     （写入即会使该批次永久不可撤回；撤回校验也拒绝已被引用的批次）
            withdrawn = conn.execute(
                "SELECT withdrawal_id FROM batch_withdrawals WHERE batch_id = %s",
                (target["batch_id"],),
            ).fetchone()
            if withdrawn:
                raise HTTPException(
                    status_code=409,
                    detail=f"链序号 {chain_seq} 所属批次「{target['batch_id']}」已被撤回，"
                           "其交接记录不属于有效交接，不得补正",
                )

            # 3) 当前有效摘要 = 该环节最后一笔补正的 new_digest，否则为原摘要
            last_corr = conn.execute(
                """
                SELECT * FROM digest_corrections
                 WHERE exhibit_id = %s AND target_chain_seq = %s
                 ORDER BY correction_seq DESC
                 LIMIT 1
                """,
                (exhibit_id, chain_seq),
            ).fetchone()
            current_digest = last_corr["new_digest"] if last_corr else target["voucher_digest"]

            # 4) CAS：预期值不符 -> 冲突（并发同预期补正仅一笔生效，其余在此落败）
            if payload.expected_digest != current_digest:
                raise HTTPException(
                    status_code=409,
                    detail=f"预期当前有效摘要与现状不符：预期「{payload.expected_digest}」，"
                           f"当前有效摘要「{current_digest}」",
                )

            # 5) 新旧相同 -> 拒绝（无实质补正）
            if payload.new_digest == current_digest:
                raise HTTPException(
                    status_code=422,
                    detail="新摘要与当前有效摘要相同，未构成补正",
                )

            # 6) 追加补正链一环：锚定被补正原记录的 record_hash（绝不改写原记录）
            correction_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            correction_seq = exhibit["correction_chain_length"] + 1
            prev_hash = exhibit["correction_head_hash"]
            h = correction_hash(
                prev_hash=prev_hash,
                correction_id=correction_id,
                exhibit_id=exhibit_id,
                correction_seq=correction_seq,
                target_chain_seq=chain_seq,
                expected_digest=payload.expected_digest,
                old_digest=current_digest,
                new_digest=payload.new_digest,
                reason=payload.reason,
                anchor_record_hash=target["record_hash"],
                created_at=now,
            )
            conn.execute(
                """
                INSERT INTO digest_corrections
                    (correction_id, idempotency_key, exhibit_id, target_chain_seq,
                     expected_digest, old_digest, new_digest, reason,
                     anchor_record_hash, correction_seq, prev_hash, record_hash,
                     request_hash, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (correction_id, idempotency_key, exhibit_id, chain_seq,
                 payload.expected_digest, current_digest, payload.new_digest, payload.reason,
                 target["record_hash"], correction_seq, prev_hash, h, req_hash, now),
            )
            conn.execute(
                """
                UPDATE exhibits
                   SET correction_chain_length = %s, correction_head_hash = %s
                 WHERE exhibit_id = %s
                """,
                (correction_seq, h, exhibit_id),
            )

            row = conn.execute(
                "SELECT * FROM digest_corrections WHERE correction_id = %s",
                (correction_id,),
            ).fetchone()
            return _serialize_correction(row, replay=False)
    except UniqueViolation:
        # 并发下同键插入：回滚后按已存在补正单返回（等价于后到的重试）
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM digest_corrections WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if existing and existing["request_hash"] == req_hash:
            response.status_code = 200
            return _serialize_correction(existing, replay=True)
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 提交了不同的补正请求载荷")


@app.get(
    "/api/v1/exhibits/{exhibit_id}/digest-corrections",
    response_model=list[CorrectionOut],
    tags=["corrections"],
)
def list_digest_corrections(
    exhibit_id: str,
    chain_seq: int | None = Query(default=None, ge=1, description="仅返回指定链序号环节的补正轨迹"),
):
    """查询展品的补正链（可按被补正链序号过滤），按时序返回。"""
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT 1 FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        if chain_seq is None:
            rows = conn.execute(
                "SELECT * FROM digest_corrections WHERE exhibit_id = %s ORDER BY correction_seq",
                (exhibit_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM digest_corrections
                 WHERE exhibit_id = %s AND target_chain_seq = %s
                 ORDER BY correction_seq
                """,
                (exhibit_id, chain_seq),
            ).fetchall()
    return [_serialize_correction(r, replay=False) for r in rows]


@app.get(
    "/api/v1/exhibits/{exhibit_id}/handovers/{chain_seq}",
    response_model=HandoverRecordOut,
    tags=["corrections"],
)
def get_handover_record(exhibit_id: str, chain_seq: int):
    """查询单个交接环节：原摘要、当前有效摘要与补正轨迹一并呈现。

    所属批次被撤回时，记录本身仍可查（`withdrawn=true`，并回带撤回单号），
    但其不属于有效交接列表。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM handover_records WHERE exhibit_id = %s AND chain_seq = %s",
            (exhibit_id, chain_seq),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在链序号 {chain_seq} 的交接记录")
        trail_map = _load_corrections_by_target(conn, exhibit_id)
        wd = conn.execute(
            "SELECT withdrawal_id FROM batch_withdrawals WHERE batch_id = %s",
            (row["batch_id"],),
        ).fetchone()

    trail = trail_map.get(chain_seq, [])
    return {
        "chain_seq": row["chain_seq"],
        "batch_id": str(row["batch_id"]),
        "seq_in_batch": row["seq_in_batch"],
        "location": row["location"],
        "from_person": row["from_person"],
        "to_person": row["to_person"],
        "seal_no": row["seal_no"],
        "seal_changed": row["seal_changed"],
        "seal_change_reason": row["seal_change_reason"],
        "occurred_at": row["occurred_at"],
        "voucher_digest": row["voucher_digest"],
        "prev_hash": row["prev_hash"],
        "record_hash": row["record_hash"],
        "created_at": row["created_at"],
        "current_voucher_digest": _current_digest(row["voucher_digest"], trail),
        "correction_trail": trail,
        "withdrawn": wd is not None,
        "withdrawal_id": str(wd["withdrawal_id"]) if wd else None,
    }


# ---------------------------------------------------------------- 审计链


@app.get("/api/v1/exhibits/{exhibit_id}/handovers", response_model=ChainOut, tags=["audit"])
def list_handovers(exhibit_id: str):
    """查询展品的完整交接链（含每条记录的链式哈希）。

    每条记录同时呈现：
    - `voucher_digest`：原交接记录上的凭证摘要（永不改写）；
    - `current_voucher_digest`：当前有效摘要（经补正链覆盖后的最新值）；
    - `correction_trail`：该环节按补正时序排列的完整补正轨迹；
    - `withdrawn`/`withdrawal_id`：所属批次是否已被撤回（记录与哈希仍在原链中保留）。

    顶层 `chain_length` 恒为原链历史长度（撤回不删不改）；
    `active_chain_length` 为排除已撤回批次后的有效交接环节数。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        rows = conn.execute(
            """
            SELECT chain_seq, batch_id, seq_in_batch, location, from_person, to_person,
                   seal_no, seal_changed, seal_change_reason, occurred_at, voucher_digest,
                   prev_hash, record_hash, created_at
              FROM handover_records
             WHERE exhibit_id = %s
             ORDER BY chain_seq
            """,
            (exhibit_id,),
        ).fetchall()
        trail_map = _load_corrections_by_target(conn, exhibit_id)
        wd_map = _load_withdrawals_map(conn, exhibit_id)

    records = []
    for r in rows:
        trail = trail_map.get(r["chain_seq"], [])
        wd = wd_map.get(str(r["batch_id"]))
        records.append({
            "chain_seq": r["chain_seq"],
            "batch_id": str(r["batch_id"]),
            "seq_in_batch": r["seq_in_batch"],
            "location": r["location"],
            "from_person": r["from_person"],
            "to_person": r["to_person"],
            "seal_no": r["seal_no"],
            "seal_changed": r["seal_changed"],
            "seal_change_reason": r["seal_change_reason"],
            "occurred_at": r["occurred_at"],
            "voucher_digest": r["voucher_digest"],
            "prev_hash": r["prev_hash"],
            "record_hash": r["record_hash"],
            "created_at": r["created_at"],
            "current_voucher_digest": _current_digest(r["voucher_digest"], trail),
            "correction_trail": trail,
            "withdrawn": wd is not None,
            "withdrawal_id": str(wd["withdrawal_id"]) if wd else None,
        })
    return {
        "exhibit_id": exhibit_id,
        "chain_length": len(records),
        "active_chain_length": exhibit["active_chain_length"],
        "records": records,
    }


@app.get("/api/v1/exhibits/{exhibit_id}/audit/verify", response_model=AuditVerifyOut, tags=["audit"])
def verify_audit_chain(exhibit_id: str):
    """重放三条哈希链并校验完整性：

    - 交接链：逐条复算 `record_hash = sha256(prev_hash ‖ 规范化业务字段)`，
      比对 prev_hash 衔接与展品物理链头（撤回/补正永不改写原记录与链头）；
    - 补正链：按展品独立成链复算，并校验每环锚点仍等于被补正原记录的
      `record_hash`；
    - 撤回链：按展品独立成链复算，校验每环的批次锚点、「最新受理批次」
      业务前提、未被补正引用、回滚快照与原链历史一致，并比对复算出的
      有效视图与展品当前保管人/封签/有效链长/链尾。

    任一链被篡改、删除、乱序，或展品有效视图被直接改写，都会在对应链块
    给出首个失效环；顶层 `valid` 要求三链同时有效，
    `first_invalid_seq/detail` 仍以交接链为准，
    三条链结果分别见 `handover_chain` / `corrections_chain` / `withdrawals_chain`。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT * FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
        ).fetchone()
        if not exhibit:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在")
        records = conn.execute(
            "SELECT * FROM handover_records WHERE exhibit_id = %s ORDER BY chain_seq",
            (exhibit_id,),
        ).fetchall()
        handover_by_seq = {r["chain_seq"]: r for r in records}
        handover_result = _verify_handover_chain(conn, exhibit, records)
        corrections_block = _verify_corrections_chain(
            conn, exhibit, handover_by_seq, handover_result["honest"]
        )
        withdrawals_block, _ = _verify_withdrawals_chain(
            conn, exhibit, records, handover_by_seq, handover_result["honest"]
        )

    handover_block = handover_result["block"]
    all_valid = (
        handover_block["valid"]
        and corrections_block["valid"]
        and withdrawals_block["valid"]
    )
    if handover_block["valid"] and corrections_block["valid"] and withdrawals_block["valid"]:
        detail = "交接链、补正链与撤回链均完整；各环锚点与原交接记录一致，展品有效视图与撤回回滚结果一致"
    elif not handover_block["valid"]:
        detail = handover_block["detail"]
    elif not corrections_block["valid"]:
        detail = corrections_block["detail"]
    else:
        detail = withdrawals_block["detail"]

    return {
        "exhibit_id": exhibit_id,
        "chain_length": handover_block["chain_length"],
        "valid": all_valid,
        "first_invalid_seq": handover_block["first_invalid_seq"],
        "detail": detail,
        "correction_chain_length": corrections_block["chain_length"],
        "withdrawal_chain_length": withdrawals_block["chain_length"],
        "active_chain_length": exhibit["active_chain_length"],
        "active_head_hash": exhibit["active_head_hash"],
        "handover_chain": handover_block,
        "corrections_chain": corrections_block,
        "withdrawals_chain": withdrawals_block,
    }


@app.get("/api/v1/exhibits/{exhibit_id}/batches", response_model=list[BatchSummary], tags=["audit"])
def list_batches(
    exhibit_id: str,
    status: str | None = Query(default=None, pattern="^(ACCEPTED|REJECTED)$"),
):
    """查询展品的批次历史（含被驳回批次与已撤回批次标记，供审计追溯）。"""
    sql = """
        SELECT b.batch_id, b.status, b.report, b.created_at,
               w.withdrawal_id
          FROM batches b
          LEFT JOIN batch_withdrawals w ON w.batch_id = b.batch_id
         WHERE b.exhibit_id = %s
    """
    params: list = [exhibit_id]
    if status:
        sql += " AND b.status = %s"
        params.append(status)
    sql += " ORDER BY b.created_at, b.batch_id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "batch_id": str(r["batch_id"]),
            "status": r["status"],
            "record_count": r["report"]["record_count"],
            "first_break": r["report"]["first_break"],
            "created_at": r["created_at"],
            "withdrawn": r["withdrawal_id"] is not None,
            "withdrawal_id": str(r["withdrawal_id"]) if r["withdrawal_id"] else None,
        }
        for r in rows
    ]
