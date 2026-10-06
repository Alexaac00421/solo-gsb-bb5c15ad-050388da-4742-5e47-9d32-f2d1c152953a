"""启动时增量迁移（幂等）：让旧数据卷（v1.1 库结构）平滑升级到补正链结构。

全新库由 db/init.sql 经 Postgres 容器 entrypoint 初始化；
旧卷不会重跑 init.sql，故启动时在此补齐补正相关结构。
所有语句均为 IF NOT EXISTS，重复执行无副作用，与 init.sql 保持等价。
"""
from __future__ import annotations

from .db import get_conn

# 补正链创世前驱哈希（与交接链 GENESIS 区分，避免补正首环误接到交接链尾）
CORRECTIONS_GENESIS_HASH = "CORRECTIONS_GENESIS"

_STATEMENTS = (
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS correction_chain_length BIGINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS correction_head_hash TEXT NOT NULL DEFAULT 'CORRECTIONS_GENESIS'
    """,
    """
    CREATE TABLE IF NOT EXISTS digest_corrections (
        correction_id      UUID        PRIMARY KEY,
        idempotency_key    TEXT        NOT NULL,
        exhibit_id         TEXT        NOT NULL REFERENCES exhibits (exhibit_id),
        target_chain_seq   BIGINT      NOT NULL,
        expected_digest    TEXT        NOT NULL,
        old_digest         TEXT        NOT NULL,
        new_digest         TEXT        NOT NULL,
        reason             TEXT        NOT NULL,
        anchor_record_hash TEXT        NOT NULL,
        correction_seq     BIGINT      NOT NULL,
        prev_hash          TEXT        NOT NULL,
        record_hash        TEXT        NOT NULL,
        request_hash       TEXT        NOT NULL,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (exhibit_id, correction_seq),
        UNIQUE (idempotency_key),
        UNIQUE (record_hash),
        FOREIGN KEY (exhibit_id, target_chain_seq)
            REFERENCES handover_records (exhibit_id, chain_seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_corr_exhibit_seq ON digest_corrections (exhibit_id, correction_seq)",
    "CREATE INDEX IF NOT EXISTS idx_corr_target ON digest_corrections (exhibit_id, target_chain_seq, correction_seq)",
)


def ensure_schema() -> None:
    """幂等地补齐补正链所需库结构（旧卷升级；新库已是最新结构）。"""
    with get_conn() as conn:
        for stmt in _STATEMENTS:
            conn.execute(stmt)
