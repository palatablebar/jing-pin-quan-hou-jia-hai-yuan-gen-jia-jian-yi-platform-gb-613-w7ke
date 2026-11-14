"""坏快照隔离控制器：台账列表/统计/详情/更正。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, quarantine_service


def list_quarantines(db: Session, **filters) -> dict:
    data = quarantine_service.list_quarantines(db, **filters)
    data["stats"] = quarantine_service.stats(db, filters.get("captured_on"))
    return data


def quarantine_stats(db: Session, captured_on=None) -> dict:
    return quarantine_service.stats(db, captured_on)


def get_quarantine(db: Session, quarantine_id: int) -> dict:
    record = quarantine_service.get_quarantine(db, quarantine_id)
    return quarantine_service.serialize(db, record, detail=True)


def correct_quarantine(db: Session, quarantine_id: int, payload: dict, principal: dict, request: Request) -> dict:
    record = quarantine_service.get_quarantine(db, quarantine_id)
    note = payload.pop("note", None)
    record, snapshot, result = quarantine_service.correct_snapshot(
        db, record, payload, operator_email=principal["email"]
    )
    if note:
        record.note = note
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="quarantine.correct",
        entity="quarantine",
        entity_id=record.id,
        detail={
            "snapshot_id": snapshot.id,
            "sku_id": record.sku_id,
            "captured_on": record.captured_on.isoformat() if record.captured_on else None,
            "unit_price_1": result.get("unit_price_1"),
            "retry_count": record.retry_count,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return quarantine_service.serialize(db, record, detail=True)
