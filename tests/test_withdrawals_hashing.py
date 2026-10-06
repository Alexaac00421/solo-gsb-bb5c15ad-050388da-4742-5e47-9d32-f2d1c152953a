"""撤回链哈希与请求指纹单测（纯标准库，无数据库依赖）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.hashing import (
    WITHDRAWALS_GENESIS_HASH,
    withdrawal_hash,
    withdrawal_request_hash,
)


BID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
WID = uuid.UUID("11111111-2222-3333-4444-555555555555")
AT = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)


def _calc(**overrides):
    kw = dict(
        prev_hash=WITHDRAWALS_GENESIS_HASH,
        withdrawal_id=WID,
        exhibit_id="EX-0001",
        withdrawal_seq=1,
        batch_id=BID,
        reason="发现凭证造假，撤回",
        anchor_batch_record_hash="a" * 64,
        anchor_active_head_hash="a" * 64,
        restore_chain_seq=0,
        restore_head_hash="GENESIS",
        restore_custodian="张伟",
        restore_seal="SEAL-1",
        created_at=AT,
    )
    kw.update(overrides)
    return withdrawal_hash(**kw)


def test_withdrawal_hash_deterministic_and_hex():
    h1 = _calc()
    h2 = _calc()
    assert h1 == h2
    assert len(h1) == 64 and int(h1, 16) >= 0


def test_new_ring_chains_to_previous_hash():
    first = _calc()
    second = _calc(prev_hash=first, withdrawal_seq=2, restore_chain_seq=1,
                   restore_head_hash="b" * 64, restore_custodian="李静")
    assert first != second
    assert second != WITHDRAWALS_GENESIS_HASH


def test_changing_any_field_breaks_hash():
    base = _calc()
    assert _calc(reason="另一个原因") != base
    assert _calc(batch_id=uuid.uuid4()) != base
    assert _calc(withdrawal_id=uuid.uuid4()) != base
    assert _calc(anchor_batch_record_hash="c" * 64) != base
    assert _calc(anchor_active_head_hash="c" * 64) != base
    assert _calc(restore_chain_seq=2) != base
    assert _calc(restore_head_hash="GENESISX") != base
    assert _calc(restore_custodian="王芳") != base
    assert _calc(restore_seal="SEAL-2") != base
    assert _calc(exhibit_id="EX-0002") != base


def test_kind_distinguishes_from_correction_payload():
    # kind 字段区分不同链类型，避免与补正链载荷同形碰撞
    from app.hashing import correction_hash, CORRECTIONS_GENESIS_HASH
    wh = _calc()
    ch = correction_hash(
        prev_hash=CORRECTIONS_GENESIS_HASH,
        correction_id=WID, exhibit_id="EX-0001", correction_seq=1,
        target_chain_seq=1, expected_digest="sha256:" + "ab" * 32,
        old_digest="sha256:" + "ab" * 32, new_digest="sha256:" + "cd" * 32,
        reason="x", anchor_record_hash="a" * 64, created_at=AT,
    )
    assert wh != ch


def test_request_hash_detects_payload_drift():
    h1 = withdrawal_request_hash("EX-0001", BID, "原因甲")
    assert h1 == withdrawal_request_hash("EX-0001", BID, "  原因甲  ")  # 去空白
    assert h1 != withdrawal_request_hash("EX-0001", BID, "原因乙")
    assert h1 != withdrawal_request_hash("EX-0002", BID, "原因甲")
    assert h1 != withdrawal_request_hash("EX-0001", uuid.uuid4(), "原因甲")
    assert len(h1) == 64
