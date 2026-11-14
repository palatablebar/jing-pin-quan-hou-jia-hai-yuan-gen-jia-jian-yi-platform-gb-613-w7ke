"""坏快照隔离单：券条款非法、无法通过算法 A 的快照被隔离，等待更正后重试同一测算。

设计要点：
- 原输入不丢失：raw_snapshot 完整保存抓价原文，snapshot 行本身也保留（不覆盖、不删除）。
- 可处理：隔离单携带失败原因、字段级错误与快照定位，更正接口据此重算。
- 可追溯：记录每次重试结果（outcome）、关联运行与补出的建议，形成「原因 → 更正 → 结果」链路。
- 同货号当天只允许一条未闭环隔离单（open / corrected / re_isolated 互斥），避免重复堆积。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class SnapshotQuarantine(Base):
    __tablename__ = "snapshot_quarantines"
    __table_args__ = (
        UniqueConstraint("snapshot_id", name="uq_quarantine_snapshot"),
        UniqueConstraint("sku_id", "captured_on", name="uq_quarantine_sku_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), index=True
    )
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"), index=True)
    competitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    captured_on: Mapped[date] = mapped_column(Date, index=True)

    # 隔离时的原始输入快照（与 snapshots 行同值的独立留档，更正不覆盖它）
    raw_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # 算法 A 失败结构：{error, message, warnings[]}
    failure_reason: Mapped[dict] = mapped_column(JSON, default=dict)
    # 触发隔离的运行（可空：手工补录阶段隔离时尚无运行）
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 最近一次评估该隔离单的重试运行（更正结果可追溯到具体运行）
    last_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    # open / corrected / resolved / re_isolated
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    # pending / advice_filled / no_advice_needed / still_invalid
    outcome: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # 最近一次更正后的输入（留痕，便于对照原输入与更正内容）
    corrected_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    corrected_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 闭环时补出的建议（自然键 run+sku 唯一，幂等重试只会指向同一条）
    advice_id: Mapped[int | None] = mapped_column(
        ForeignKey("advices.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
