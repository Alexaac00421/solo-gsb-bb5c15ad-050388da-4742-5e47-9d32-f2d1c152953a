"""请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HandoverIn(BaseModel):
    """单条交接记录（按时间排列提交，序号由提交顺序决定）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    location: str = Field(min_length=1, max_length=200, description="交接地点")
    from_person: str = Field(min_length=1, max_length=100, description="交出人")
    to_person: str = Field(min_length=1, max_length=100, description="接收人")
    seal_no: str = Field(min_length=1, max_length=100, description="封签号")
    occurred_at: datetime = Field(description="交接时间（必须带时区）")
    voucher_digest: str = Field(min_length=1, max_length=200, description="凭证摘要，如 sha256:<64hex>")
    seal_change_reason: Optional[str] = Field(default=None, max_length=500, description="换签授权理由（换签时必填）")

    @field_validator("occurred_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at 必须携带时区，如 2026-09-01T09:00:00+08:00 或 2026-09-01T01:00:00Z")
        return v


class BatchRequest(BaseModel):
    exhibit_id: str = Field(min_length=1, max_length=64, description="展品编号")
    records: list[HandoverIn] = Field(min_length=1, max_length=1000, description="按时间排列的交接记录")


# ---------------------------------------------------------------- 凭证摘要补正


class CorrectionRequest(BaseModel):
    """凭证摘要补正单（原交接记录与其 record_hash 永不改写，只追加补正）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    expected_digest: str = Field(min_length=1, max_length=200, description="预期当前有效摘要（与现状不符 -> 409 冲突）")
    new_digest: str = Field(min_length=1, max_length=200, description="新摘要（须为 算法:十六进制；与当前有效摘要相同 -> 422 拒绝）")
    reason: str = Field(min_length=1, max_length=500, description="补正原因（非空；空缺 -> 422 拒绝）")


class WithdrawalRequest(BaseModel):
    """交接批次撤回请求（原交接记录与哈希永不改写，只追加撤回记录）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=500, description="撤回原因（非空；空缺 -> 422 拒绝）")


class ReasonOut(BaseModel):
    rule: str
    message: str


class ItemOut(BaseModel):
    seq: int
    ok: bool
    reasons: list[ReasonOut]


class FirstBreakOut(BaseModel):
    seq: int
    rule: str
    message: str


class BatchReport(BaseModel):
    """核验报告（POST 提交与 GET 查询共用）。"""

    batch_id: str
    exhibit_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]
    idempotent_replay: bool
    created_at: datetime


class ExhibitCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    exhibit_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    home_location: str = Field(min_length=1, max_length=200, description="登记馆藏地（首站校验基准）")
    custodian: str = Field(min_length=1, max_length=100, description="登记保管人")
    initial_seal: str = Field(min_length=1, max_length=100, description="初始封签号")


class ExhibitOut(BaseModel):
    exhibit_id: str
    name: str
    home_location: str
    custodian: str
    current_seal: str
    last_time: Optional[datetime]
    chain_length: int
    chain_head_hash: str
    created_at: datetime
    # 登记基线（撤回首批次时回滚到此）
    registered_custodian: Optional[str] = None
    registered_seal: Optional[str] = None
    # 有效链视图：排除已撤回批次后的链长/链尾（新批次校验以此为参照；
    # 物理 chain_length/chain_head_hash 恒为原链全部记录，撤回不改写）
    active_chain_length: Optional[int] = 0
    active_head_hash: Optional[str] = None
    # 补正链状态（新增字段；补正不推进交接链，故上面 chain_length/chain_head_hash 不变）
    correction_chain_length: Optional[int] = 0
    correction_head_hash: Optional[str] = None
    # 撤回链状态（撤回只追加，不改动原交接链字段）
    withdrawal_chain_length: Optional[int] = 0
    withdrawal_head_hash: Optional[str] = None


class HandoverRecordOut(BaseModel):
    chain_seq: int
    batch_id: str
    seq_in_batch: int
    location: str
    from_person: str
    to_person: str
    seal_no: str
    seal_changed: bool
    seal_change_reason: Optional[str]
    occurred_at: datetime
    voucher_digest: str
    prev_hash: str
    record_hash: str
    created_at: datetime
    # 补正引入的新字段（原字段保持不变，向后兼容）：
    # 当前有效摘要（无补正则等于原 voucher_digest）与该环节的补正轨迹
    current_voucher_digest: Optional[str] = None
    correction_trail: list["CorrectionTrailItem"] = Field(default_factory=list)
    # 撤回引入的新字段（原字段保持不变，向后兼容）：
    # 该记录所属批次是否已被撤回；撤回后该环不属于有效交接列表
    withdrawn: bool = False
    withdrawal_id: Optional[str] = None


class CorrectionTrailItem(BaseModel):
    """交接查询中某环节的补正轨迹条目（按补正时序）。"""

    correction_id: str
    correction_seq: int
    target_chain_seq: int
    expected_digest: str
    old_digest: str
    new_digest: str
    reason: str
    anchor_record_hash: str
    record_hash: str
    idempotent_replay: bool
    created_at: datetime


class CorrectionOut(BaseModel):
    """补正单（首次提交 201；同键同载荷重放 200）。"""

    correction_id: str
    exhibit_id: str
    target_chain_seq: int
    expected_digest: str
    old_digest: str
    new_digest: str
    reason: str
    anchor_record_hash: str
    correction_seq: int
    prev_hash: str
    record_hash: str
    idempotent_replay: bool
    created_at: datetime


class WithdrawalOut(BaseModel):
    """批次撤回记录（首次受理 201；同键同载荷重放 200）。"""

    withdrawal_id: str
    exhibit_id: str
    batch_id: str
    reason: str
    anchor_batch_record_hash: str = Field(description="锚点：被撤回批次最后一环的 record_hash")
    anchor_active_head_hash: str = Field(description="撤回受理时的有效链尾（= 被撤回批次末环 record_hash）")
    restore_chain_seq: int = Field(description="撤回后有效链回滚到的链序号")
    restore_head_hash: str = Field(description="撤回后有效链回滚到的链头哈希")
    restore_custodian: str = Field(description="撤回后当前保管人（批次前一环的接收人/登记保管人）")
    restore_seal: str = Field(description="撤回后当前封签（批次前一环封签/登记初始封签）")
    withdrawal_seq: int
    prev_hash: str
    record_hash: str
    idempotent_replay: bool
    created_at: datetime


class ChainAuditBlock(BaseModel):
    """审计复算结果中的一条链（交接链 / 补正链）。"""

    chain_length: int
    head_hash: Optional[str] = Field(default=None, description="复算得到的链尾哈希")
    valid: bool
    first_invalid_seq: Optional[int] = None
    detail: str


class ChainOut(BaseModel):
    exhibit_id: str
    chain_length: int
    # 有效链长度：排除已撤回批次记录后的环节数（原链长度见 chain_length，保持不变）
    active_chain_length: Optional[int] = None
    records: list[HandoverRecordOut]


class AuditVerifyOut(BaseModel):
    exhibit_id: str
    chain_length: int
    valid: bool
    first_invalid_seq: Optional[int]
    detail: str
    # 补正引入：同时复算交接链与补正链（新增字段，不改动原有字段，向后兼容）
    correction_chain_length: Optional[int] = 0
    corrections_chain: Optional[ChainAuditBlock] = None
    handover_chain: Optional[ChainAuditBlock] = None
    # 撤回引入：再复算撤回链并比对有效视图回滚结果（三链任一失效则 valid=false）
    withdrawal_chain_length: Optional[int] = 0
    withdrawals_chain: Optional[ChainAuditBlock] = None
    active_chain_length: Optional[int] = 0
    active_head_hash: Optional[str] = None


class BatchSummary(BaseModel):
    batch_id: str
    status: str
    record_count: int
    first_break: Optional[FirstBreakOut]
    created_at: datetime
    # 撤回引入（向后兼容新增）：批次是否已被撤回及撤回单号
    withdrawn: bool = False
    withdrawal_id: Optional[str] = None


class HandoverTokenOut(BaseModel):
    """预演签发的展品状态令牌（正式提交可通过 Handover-Token 头携带）。"""

    token: str = Field(description="自包含 HMAC 令牌，绑定展品与签发时的哈希链状态")
    exhibit_id: str
    chain_length: int
    chain_head_hash: str
    issued_at: datetime


class RehearsalOut(BaseModel):
    """预演核验结果：只读，不写批次报告/交接记录/展品状态。"""

    exhibit_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    record_count: int
    first_break: Optional[FirstBreakOut]
    items: list[ItemOut]
    rehearsed_at: datetime
    state_token: HandoverTokenOut


# HandoverRecordOut 前向引用了稍后定义的 CorrectionTrailItem，显式重绑模型
HandoverRecordOut.model_rebuild()
