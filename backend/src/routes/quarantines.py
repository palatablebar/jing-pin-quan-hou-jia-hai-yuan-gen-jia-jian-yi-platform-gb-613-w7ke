from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import RUN_CREATE_ROLES
from src.controllers import quarantine_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.snapshot import QuarantineCorrectIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/quarantines", tags=["quarantines"])


@router.get("/stats")
def quarantine_stats(
    captured_on: date | None = None,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return quarantine_controller.quarantine_stats(db, captured_on)


@router.get("")
def list_quarantines(
    status: str | None = None,
    outcome: str | None = None,
    captured_on: date | None = None,
    sku_id: int | None = None,
    q: str | None = None,
    limit: int = Query(40),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return quarantine_controller.list_quarantines(
        db,
        status=status,
        outcome=outcome,
        captured_on=captured_on,
        sku_id=sku_id,
        q=q,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{quarantine_id}")
def get_quarantine(
    quarantine_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return quarantine_controller.get_quarantine(db, quarantine_id)


@router.patch("/{quarantine_id}/correct")
def correct_quarantine(
    quarantine_id: int,
    payload: QuarantineCorrectIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*RUN_CREATE_ROLES)),
) -> dict:
    return quarantine_controller.correct_quarantine(
        db, quarantine_id, payload.model_dump(exclude_unset=True), principal, request
    )
