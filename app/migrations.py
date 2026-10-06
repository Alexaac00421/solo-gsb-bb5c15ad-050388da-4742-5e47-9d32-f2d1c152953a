"""启动时增量迁移（幂等）：让旧数据卷平滑升级到最新结构。

全新库由 db/init.sql 经 Postgres 容器 entrypoint 初始化；
旧卷不会重跑 init.sql，故启动时在此补齐结构。
所有语句均为 IF NOT EXISTS / 仅在列缺失时回填，重复执行无副作用。

若连接到的库连基础表都不存在（例如本地开发库未执行 init.sql），
则直接执行 db/init.sql 完成全量建表与种子写入，再做增量语句兜底。
"""
from __future__ import annotations

from pathlib import Path

from .db import get_conn

_INIT_SQL = Path(__file__).resolve().parent.parent / "db" / "init.sql"

# 补正链创世前驱哈希（与交接链 GENESIS 区分，避免补正首环误接到交接链尾）
CORRECTIONS_GENESIS_HASH = "CORRECTIONS_GENESIS"
# 撤回链创世前驱哈希（撤回链独立成链，只追加）
WITHDRAWALS_GENESIS_HASH = "WITHDRAWALS_GENESIS"

_STATEMENTS = (
    # ---- 补正链（历史迁移，保留）-----------------------------------------
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

    # ---- 批次撤回链 -------------------------------------------------------
    # 登记基线（撤回首批次时回滚到此）。旧卷两列先以可空加入，再按现有链回填
    # （取首环的交出人/参照封签作为登记基线），最后收紧为 NOT NULL。
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS registered_custodian TEXT
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS registered_seal TEXT
    """,
    """
    UPDATE exhibits
       SET registered_custodian = (
               SELECT h.from_person FROM handover_records h
                WHERE h.exhibit_id = exhibits.exhibit_id
                ORDER BY h.chain_seq LIMIT 1
           )
     WHERE registered_custodian IS NULL AND chain_length > 0
    """,
    """
    UPDATE exhibits
       SET registered_custodian = custodian
     WHERE registered_custodian IS NULL
    """,
    """
    UPDATE exhibits
       SET registered_seal = (
               -- 首环封签：换签则取换签理由记录前的参照封签已不可得，
               -- 旧卷无独立初始封签列，此时退化为首环当前封签（仅影响撤回首环回滚目标）
               SELECT h.seal_no FROM handover_records h
                WHERE h.exhibit_id = exhibits.exhibit_id
                ORDER BY h.chain_seq LIMIT 1
           )
     WHERE registered_seal IS NULL AND chain_length > 0
    """,
    """
    UPDATE exhibits
       SET registered_seal = current_seal
     WHERE registered_seal IS NULL
    """,
    """
    ALTER TABLE exhibits
        ALTER COLUMN registered_custodian SET NOT NULL
    """,
    """
    ALTER TABLE exhibits
        ALTER COLUMN registered_seal SET NOT NULL
    """,
    # 有效链视图：初始等于物理链（旧卷尚无撤回）
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS active_chain_length BIGINT NOT NULL DEFAULT 0
    """,
    """
    UPDATE exhibits SET active_chain_length = chain_length
     WHERE active_chain_length = 0 AND chain_length > 0
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS active_head_hash TEXT NOT NULL DEFAULT 'GENESIS'
    """,
    """
    UPDATE exhibits SET active_head_hash = chain_head_hash
     WHERE active_head_hash = 'GENESIS' AND chain_head_hash <> 'GENESIS'
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS withdrawal_chain_length BIGINT NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE exhibits
        ADD COLUMN IF NOT EXISTS withdrawal_head_hash TEXT NOT NULL DEFAULT 'WITHDRAWALS_GENESIS'
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_withdrawals (
        withdrawal_id            UUID        PRIMARY KEY,
        idempotency_key          TEXT        NOT NULL,
        exhibit_id               TEXT        NOT NULL REFERENCES exhibits (exhibit_id),
        batch_id                 UUID        NOT NULL REFERENCES batches (batch_id),
        reason                   TEXT        NOT NULL,
        anchor_batch_record_hash TEXT        NOT NULL,
        anchor_active_head_hash  TEXT        NOT NULL,
        restore_chain_seq        BIGINT      NOT NULL,
        restore_head_hash        TEXT        NOT NULL,
        restore_custodian        TEXT        NOT NULL,
        restore_seal             TEXT        NOT NULL,
        withdrawal_seq           BIGINT      NOT NULL,
        prev_hash                TEXT        NOT NULL,
        record_hash              TEXT        NOT NULL,
        request_hash             TEXT        NOT NULL,
        created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (exhibit_id, withdrawal_seq),
        UNIQUE (batch_id),
        UNIQUE (idempotency_key),
        UNIQUE (record_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wd_exhibit_seq ON batch_withdrawals (exhibit_id, withdrawal_seq)",
)


def ensure_schema() -> None:
    """幂等地保证库结构为最新（全新库全量建表；旧卷增量升级，可重复执行）。"""
    with get_conn() as conn:
        has_base = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'exhibits'"
        ).fetchone()
        if not has_base:
            # 基础表缺失：执行全量初始化脚本（脚本本身幂等，含种子数据 ON CONFLICT）
            conn.execute(_INIT_SQL.read_text(encoding="utf-8"))
        for stmt in _STATEMENTS:
            conn.execute(stmt)
