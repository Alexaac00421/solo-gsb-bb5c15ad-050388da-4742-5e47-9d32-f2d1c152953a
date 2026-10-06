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
    correction_hash,
    correction_request_hash,
    record_hash,
    request_hash,
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
                "幂等写入，失败批次不落任何交接记录，保留可查询的哈希审计链。",
)

# ---------------------------------------------------------------- 工具


def _exhibit_state(row: dict) -> ExhibitState:
    return ExhibitState(
        exhibit_id=row["exhibit_id"],
        home_location=row["home_location"],
        custodian=row["custodian"],
        current_seal=row["current_seal"],
        last_time=row["last_time"],
        chain_length=row["chain_length"],
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
                INSERT INTO exhibits (exhibit_id, name, home_location, custodian, current_seal)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (payload.exhibit_id, payload.name, payload.home_location, payload.custodian, payload.initial_seal),
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
                conn.execute(
                    """
                    UPDATE exhibits
                       SET custodian = %s, current_seal = %s, last_time = %s,
                           chain_length = %s, chain_head_hash = %s
                     WHERE exhibit_id = %s
                    """,
                    (last.to_person, last.seal_no, last.occurred_at, chain_seq,
                     prev_hash, payload.exhibit_id),
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
    """查询单个交接环节：原摘要、当前有效摘要与补正轨迹一并呈现。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM handover_records WHERE exhibit_id = %s AND chain_seq = %s",
            (exhibit_id, chain_seq),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"展品「{exhibit_id}」不存在链序号 {chain_seq} 的交接记录")
        trail_map = _load_corrections_by_target(conn, exhibit_id)

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
    }


# ---------------------------------------------------------------- 审计链


@app.get("/api/v1/exhibits/{exhibit_id}/handovers", response_model=ChainOut, tags=["audit"])
def list_handovers(exhibit_id: str):
    """查询展品的完整交接链（含每条记录的链式哈希）。

    每条记录同时呈现：
    - `voucher_digest`：原交接记录上的凭证摘要（永不改写）；
    - `current_voucher_digest`：当前有效摘要（经补正链覆盖后的最新值）；
    - `correction_trail`：该环节按补正时序排列的完整补正轨迹。
    """
    with get_conn() as conn:
        exhibit = conn.execute(
            "SELECT 1 FROM exhibits WHERE exhibit_id = %s", (exhibit_id,)
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

    records = []
    for r in rows:
        trail = trail_map.get(r["chain_seq"], [])
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
        })
    return {"exhibit_id": exhibit_id, "chain_length": len(records), "records": records}


@app.get("/api/v1/exhibits/{exhibit_id}/audit/verify", response_model=AuditVerifyOut, tags=["audit"])
def verify_audit_chain(exhibit_id: str):
    """重放两条哈希链并校验完整性：

    - 交接链：逐条复算 `record_hash = sha256(prev_hash ‖ 规范化业务字段)`，
      比对 prev_hash 衔接与展品链头（补正永不改写原记录与链头）；
    - 补正链：按展品独立成链复算，并校验每环锚点仍等于被补正原记录的
      `record_hash`。

    任一链被篡改、删除、乱序，都会在对应链块给出首个失效环；
    顶层 `valid/first_invalid_seq/detail` 保持原语义（以交接链为准），
    补正链结果见 `corrections_chain`，交接链块见 `handover_chain`。
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

    handover_block = handover_result["block"]
    both_valid = handover_block["valid"] and corrections_block["valid"]
    if both_valid:
        detail = "交接链与补正链均完整；补正链各环锚点与原交接记录一致"
    elif not handover_block["valid"]:
        detail = handover_block["detail"]
    else:
        detail = corrections_block["detail"]

    return {
        "exhibit_id": exhibit_id,
        "chain_length": handover_block["chain_length"],
        "valid": both_valid,
        "first_invalid_seq": handover_block["first_invalid_seq"],
        "detail": detail,
        "correction_chain_length": corrections_block["chain_length"],
        "handover_chain": handover_block,
        "corrections_chain": corrections_block,
    }


@app.get("/api/v1/exhibits/{exhibit_id}/batches", response_model=list[BatchSummary], tags=["audit"])
def list_batches(
    exhibit_id: str,
    status: str | None = Query(default=None, pattern="^(ACCEPTED|REJECTED)$"),
):
    """查询展品的批次历史（含被驳回批次，供审计追溯）。"""
    sql = """
        SELECT batch_id, status, report, created_at
          FROM batches
         WHERE exhibit_id = %s
    """
    params: list = [exhibit_id]
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY created_at, batch_id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "batch_id": str(r["batch_id"]),
            "status": r["status"],
            "record_count": r["report"]["record_count"],
            "first_break": r["report"]["first_break"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
