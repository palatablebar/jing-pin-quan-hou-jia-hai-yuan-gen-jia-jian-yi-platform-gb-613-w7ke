"""坏快照隔离服务：隔离留档、更正重试、闭环追溯与统计。

职责边界：
- `quarantine_snapshot`：算法 A 失败时落隔离单（幂等，同货号当天只留一条未闭环单），原输入原样保存。
- `correct_snapshot`：按正确券条款更正快照并重算算法 A；更正不通过则拒绝，隔离单保持可处理。
- `evaluate_during_run`：重跑测算时评估隔离单，闭环或重新隔离，并回填关联建议。
- 列表/详情/统计：支撑页面核对「输入 / 隔离 / 已出建议」数量并追溯每条原因到更正结果。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CATEGORY_LABELS,
    PLATFORM_LABELS,
    QUARANTINE_OUTCOME_LABELS,
    QUARANTINE_STATUS_LABELS,
    QuarantineOutcome,
    QuarantineStatus,
)
from src.models.competitor import Competitor
from src.models.quarantine import SnapshotQuarantine
from src.models.run import Run
from src.models.sku import Sku
from src.models.snapshot import Snapshot
from src.services import effective_price, snapshot_service
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, utcnow

# 仍需处理（未闭环）的状态；同货号当天在这些状态下互斥，只能更正不能重复隔离
OPEN_STATUSES = (
    QuarantineStatus.OPEN.value,
    QuarantineStatus.CORRECTED.value,
    QuarantineStatus.RE_ISOLATED.value,
)


def snapshot_raw_spec(snapshot: Snapshot) -> dict:
    """取快照的原始输入（不含算法 A 产物），用于隔离留档与更正回填。"""
    return {
        "page_price": snapshot.page_price,
        "full_reduction": snapshot.full_reduction or [],
        "shop_coupon": snapshot.shop_coupon,
        "platform_coupon": snapshot.platform_coupon,
        "member_discount": snapshot.member_discount,
        "stock": snapshot.stock,
        "campaign": snapshot.campaign,
        "source_url": snapshot.source_url,
    }


def quarantine_snapshot(
    db: Session,
    snapshot: Snapshot,
    failure: dict,
    *,
    run_id: int | None = None,
) -> SnapshotQuarantine:
    """把一条算法 A 失败的快照落隔离单（幂等）。

    已存在未闭环隔离单时复用并刷新失败原因；已闭环（resolved）后再次失败则重新打开。
    原快照行与原输入都保留，不做覆盖或删除。
    """
    record = db.scalar(select(SnapshotQuarantine).where(SnapshotQuarantine.snapshot_id == snapshot.id))
    if record is None:
        record = SnapshotQuarantine(
            snapshot_id=snapshot.id,
            sku_id=snapshot.sku_id,
            competitor_id=snapshot.competitor_id,
            captured_on=snapshot.captured_on,
            raw_snapshot=snapshot_raw_spec(snapshot),
            failure_reason=_failure_payload(failure),
            run_id=run_id,
            last_run_id=run_id,
            status=QuarantineStatus.OPEN.value,
            outcome=QuarantineOutcome.PENDING.value,
        )
        db.add(record)
        db.flush()
        return record

    # 已存在：保留首次留档的 raw_snapshot（原输入永不丢失），刷新失败信息
    record.failure_reason = _failure_payload(failure)
    if run_id is not None:
        record.last_run_id = run_id
        if record.run_id is None:
            record.run_id = run_id
    if record.status == QuarantineStatus.RESOLVED.value:
        # 闭环后又被改坏：重新打开，历史更正记录保留
        record.status = QuarantineStatus.RE_ISOLATED.value
        record.outcome = QuarantineOutcome.PENDING.value
        record.advice_id = None
        record.resolved_at = None
    elif record.status == QuarantineStatus.CORRECTED.value:
        # 已更正但重算仍失败：回到可处理的重新隔离态
        record.status = QuarantineStatus.RE_ISOLATED.value
        record.outcome = QuarantineOutcome.STILL_INVALID.value
    db.flush()
    return record


def _failure_payload(failure: dict) -> dict:
    return {
        "error": failure.get("error") or "INVALID_SNAPSHOT",
        "message": failure.get("message") or "券条款配置非法，算法 A 无法还原",
        "warnings": list(failure.get("warnings") or []),
    }


def get_quarantine(db: Session, quarantine_id: int) -> SnapshotQuarantine:
    record = db.get(SnapshotQuarantine, quarantine_id)
    if record is None:
        raise not_found("quarantine", quarantine_id)
    return record


def correct_snapshot(
    db: Session,
    record: SnapshotQuarantine,
    payload: dict,
    *,
    operator_email: str,
) -> tuple[SnapshotQuarantine, Snapshot, dict]:
    """按正确券条款更正隔离快照并立即重算算法 A。

    - 只接受算法 A 能通过的更正；不通过则抛 VALIDATION，隔离单维持原状（仍可继续更正）。
    - 更正通过后更新快照行（原始输入已在 raw_snapshot 留档），隔离单转「已更正待重试」。
    - 返回 (隔离单, 快照, 算法 A 结果)，控制器据此提交审计与事务。
    """
    snapshot = db.get(Snapshot, record.snapshot_id)
    if snapshot is None:
        raise not_found("snapshot", record.snapshot_id)
    if record.status == QuarantineStatus.RESOLVED.value:
        raise AppError("CONFLICT", 409, {"reason": "该隔离单已闭环，无需再更正"})

    base = snapshot_raw_spec(snapshot)
    spec = {
        "page_price": payload.get("page_price", base["page_price"]),
        "full_reduction": payload.get("full_reduction", base["full_reduction"]),
        "shop_coupon": payload.get("shop_coupon", base["shop_coupon"]),
        "platform_coupon": payload.get("platform_coupon", base["platform_coupon"]),
        "member_discount": payload.get("member_discount", base["member_discount"]),
    }
    result = effective_price.compute_effective_price(spec)
    if not result.get("valid"):
        raise AppError(
            "VALIDATION",
            422,
            {
                "field": "coupon_spec",
                "reason": result.get("message") or "更正后的券条款仍无法通过算法 A",
                "warnings": result.get("warnings") or [],
            },
        )

    snapshot.page_price = spec["page_price"]
    snapshot.full_reduction = spec["full_reduction"]
    snapshot.shop_coupon = spec["shop_coupon"]
    snapshot.platform_coupon = spec["platform_coupon"]
    snapshot.member_discount = spec["member_discount"]
    snapshot.effective = result

    corrected = dict(spec)
    record.corrected_snapshot = corrected
    record.corrected_by = operator_email
    record.corrected_at = utcnow()
    record.status = QuarantineStatus.CORRECTED.value
    record.outcome = QuarantineOutcome.PENDING.value
    record.retry_count = (record.retry_count or 0) + 1
    record.resolved_at = None
    record.advice_id = None
    db.flush()
    return record, snapshot, result


def evaluate_during_run(
    db: Session,
    run: Run,
    window_start: date,
    window_end: date,
    sku_advice_ids: dict[int, int],
) -> dict:
    """测算运行中评估所有落入回放窗口的未闭环隔离单。

    前提：窗口内快照的算法 A 已在 pricing_service 重算，snapshot.effective 为最新结果。
    - 快照已合格：闭环；本次运行补出建议 -> advice_filled，否则 no_advice_needed。
    - 快照仍非法：保持/转为未闭环（open 或 re_isolated），outcome=still_invalid。
    幂等：重复运行对同一条隔离单只会更新到同一终态，建议关联不重复。
    """
    records = list(
        db.scalars(
            select(SnapshotQuarantine).where(
                SnapshotQuarantine.captured_on >= window_start,
                SnapshotQuarantine.captured_on <= window_end,
                SnapshotQuarantine.status.in_(OPEN_STATUSES),
            )
        ).all()
    )
    stats = {"resolved": 0, "re_isolated": 0, "advice_filled": 0, "no_advice_needed": 0}
    snapshot_ids = {record.snapshot_id for record in records}
    snapshots = {
        row.id: row
        for row in db.scalars(select(Snapshot).where(Snapshot.id.in_(snapshot_ids))).all()
    } if snapshot_ids else {}
    for record in records:
        record.last_run_id = run.id
        snapshot = snapshots.get(record.snapshot_id)
        valid = bool(snapshot and (snapshot.effective or {}).get("valid"))
        if not valid:
            # 更正后仍非法 vs 从未更正过：区分 re_isolated 与 open
            if record.status in (QuarantineStatus.CORRECTED.value, QuarantineStatus.RE_ISOLATED.value):
                record.status = QuarantineStatus.RE_ISOLATED.value
                stats["re_isolated"] += 1
            record.outcome = QuarantineOutcome.STILL_INVALID.value
            continue

        advice_id = sku_advice_ids.get(record.sku_id)
        record.advice_id = advice_id
        record.resolved_at = utcnow()
        record.status = QuarantineStatus.RESOLVED.value
        stats["resolved"] += 1
        if advice_id is not None:
            record.outcome = QuarantineOutcome.ADVICE_FILLED.value
            stats["advice_filled"] += 1
        else:
            record.outcome = QuarantineOutcome.NO_ADVICE_NEEDED.value
            stats["no_advice_needed"] += 1
    db.flush()
    return stats


def serialize(db: Session, record: SnapshotQuarantine, *, detail: bool = False) -> dict:
    sku = db.get(Sku, record.sku_id)
    competitor = db.get(Competitor, record.competitor_id) if record.competitor_id else None
    snapshot = db.get(Snapshot, record.snapshot_id)
    run = db.get(Run, record.last_run_id or record.run_id) if (record.last_run_id or record.run_id) else None
    data = {
        "id": record.id,
        "snapshot_id": record.snapshot_id,
        "sku_id": record.sku_id,
        "sku_code": sku.sku_code if sku else None,
        "sku_name": sku.name if sku else None,
        "category": sku.category if sku else None,
        "category_label": CATEGORY_LABELS.get(sku.category, sku.category) if sku else None,
        "competitor_id": record.competitor_id,
        "competitor_name": competitor.name if competitor else None,
        "competitor_code": competitor.code if competitor else None,
        "platform_label": PLATFORM_LABELS.get(competitor.platform, competitor.platform) if competitor else None,
        "captured_on": record.captured_on.isoformat() if record.captured_on else None,
        "status": record.status,
        "status_label": QUARANTINE_STATUS_LABELS.get(record.status, record.status),
        "outcome": record.outcome,
        "outcome_label": QUARANTINE_OUTCOME_LABELS.get(record.outcome, record.outcome),
        "failure_message": (record.failure_reason or {}).get("message"),
        "retry_count": record.retry_count or 0,
        "run_id": record.run_id,
        "last_run_id": record.last_run_id,
        "run_no": run.run_no if run else None,
        "advice_id": record.advice_id,
        "corrected_by": record.corrected_by,
        "corrected_at": iso_utc(record.corrected_at),
        "resolved_at": iso_utc(record.resolved_at),
        "created_at": iso_utc(record.created_at),
        "snapshot_valid": bool((snapshot.effective or {}).get("valid")) if snapshot else False,
    }
    if detail:
        data["raw_snapshot"] = record.raw_snapshot or {}
        data["corrected_snapshot"] = record.corrected_snapshot
        data["failure_reason"] = record.failure_reason or {}
        data["note"] = record.note
        if snapshot is not None:
            data["current_snapshot"] = snapshot_service.serialize(db, snapshot, detail=True)
    return data


def list_quarantines(
    db: Session,
    *,
    status: str | None = None,
    outcome: str | None = None,
    captured_on: date | None = None,
    sku_id: int | None = None,
    q: str | None = None,
    limit: int = 40,
    offset: int = 0,
) -> dict:
    stmt = select(SnapshotQuarantine).join(Sku, Sku.id == SnapshotQuarantine.sku_id)
    count_stmt = select(func.count()).select_from(SnapshotQuarantine).join(
        Sku, Sku.id == SnapshotQuarantine.sku_id
    )
    if status:
        stmt = stmt.where(SnapshotQuarantine.status == status)
        count_stmt = count_stmt.where(SnapshotQuarantine.status == status)
    if outcome:
        stmt = stmt.where(SnapshotQuarantine.outcome == outcome)
        count_stmt = count_stmt.where(SnapshotQuarantine.outcome == outcome)
    if captured_on:
        stmt = stmt.where(SnapshotQuarantine.captured_on == captured_on)
        count_stmt = count_stmt.where(SnapshotQuarantine.captured_on == captured_on)
    if sku_id:
        stmt = stmt.where(SnapshotQuarantine.sku_id == sku_id)
        count_stmt = count_stmt.where(SnapshotQuarantine.sku_id == sku_id)
    if q:
        like = f"%{q.strip()}%"
        condition = or_(Sku.name.ilike(like), Sku.sku_code.ilike(like))
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(SnapshotQuarantine.status.desc(), SnapshotQuarantine.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "items": [serialize(db, row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "stats": stats(db, captured_on),
    }


def stats(db: Session, captured_on: date | None = None) -> dict:
    """隔离台账统计：未闭环/待更正/已更正/已闭环/补建议计数。"""
    day_filter = SnapshotQuarantine.captured_on == captured_on if captured_on else True

    def _count(condition) -> int:
        return db.scalar(select(func.count()).select_from(SnapshotQuarantine).where(condition)) or 0

    open_count = _count(and_(day_filter, SnapshotQuarantine.status.in_(OPEN_STATUSES)))
    pending_correction = _count(and_(day_filter, SnapshotQuarantine.status == QuarantineStatus.OPEN.value))
    corrected_waiting = _count(
        and_(day_filter, SnapshotQuarantine.status == QuarantineStatus.CORRECTED.value)
    )
    resolved = _count(and_(day_filter, SnapshotQuarantine.status == QuarantineStatus.RESOLVED.value))
    advice_filled = _count(
        and_(day_filter, SnapshotQuarantine.outcome == QuarantineOutcome.ADVICE_FILLED.value)
    )
    re_isolated = _count(
        and_(day_filter, SnapshotQuarantine.status == QuarantineStatus.RE_ISOLATED.value)
    )
    total = db.scalar(select(func.count()).select_from(SnapshotQuarantine).where(day_filter)) or 0
    return {
        "total": total,
        "open_total": open_count,
        "pending_correction": pending_correction,
        "corrected_waiting_retry": corrected_waiting,
        "re_isolated": re_isolated,
        "resolved_total": resolved,
        "advice_filled": advice_filled,
        "captured_on": captured_on.isoformat() if captured_on else None,
    }
