"""隔离台 HTTP 接口集成测试：鉴权、RBAC、422 校验与端到端闭环（TestClient + 内存 SQLite）。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.database import get_db
from src.config.app import create_app
from src.models import Base, Competitor, Rule, Sku, Snapshot, SnapshotQuarantine
from src.services import effective_price
from src.utils.security import create_access_token

TODAY = date(2026, 11, 14)
VALID_SPEC = {
    "page_price": 299.0,
    "full_reduction": [{"threshold": 300.0, "minus": 40.0}],
    "shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True},
    "platform_coupon": {"per": 300.0, "minus": 50.0},
    "member_discount": 1.0,
}
BAD_SPEC = dict(VALID_SPEC, shop_coupon={"threshold": 50.0, "minus": 80.0, "stackable": True})


@pytest.fixture()
def client_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    db = Session()
    competitor = Competitor(
        code="dy-muyan", name="木言家居旗舰店", platform="douyin",
        shop_url="https://example.test/shop", capture_config={}, enabled=True,
    )
    db.add(competitor)
    db.flush()
    sku = Sku(
        sku_code="PB-sofa-001", name="云漫布艺沙发", category="sofa", competitor_id=competitor.id,
        cost=191.4, floor_price=239.25, target_margin_pct=12, current_price=319.0,
        stock=10, enabled=True,
    )
    db.add(sku)
    db.add(
        Rule(code="rule-global", name="全局", scope_kind="global", strategy="guarded",
             target_margin_pct=12, max_drop_pct=15, priority=100, enabled=True)
    )
    db.flush()
    for offset in (2, 1):
        db.add(_snapshot(sku, competitor, TODAY - timedelta(days=offset), VALID_SPEC))
    db.add(_snapshot(sku, competitor, TODAY, BAD_SPEC))
    db.commit()

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    yield client, db
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def _snapshot(sku, competitor, day, spec):
    return Snapshot(
        sku_id=sku.id, competitor_id=competitor.id, captured_on=day,
        page_price=spec["page_price"], full_reduction=spec.get("full_reduction") or [],
        shop_coupon=spec.get("shop_coupon"), platform_coupon=spec.get("platform_coupon"),
        member_discount=spec.get("member_discount", 1.0), stock=5, campaign="早班",
        effective=effective_price.compute_effective_price(spec),
    )


def _auth(role: str) -> dict:
    token = create_access_token(f"{role}@pricewatch.io", role)[0]
    return {"Authorization": f"Bearer {token}"}


def test_quarantine_requires_authentication(client_db):
    client, _ = client_db
    assert client.get("/api/quarantines").status_code == 401
    assert client.get("/api/quarantines/stats").status_code == 401


def test_viewer_can_read_but_cannot_correct(client_db):
    client, db = client_db
    # 先跑一次测算生成隔离单
    run_resp = client.post("/api/runs", json={"captured_on": TODAY.isoformat(), "trigger": "manual"}, headers=_auth("analyst"))
    assert run_resp.status_code == 201
    record = db.scalar(select(SnapshotQuarantine))
    assert record is not None

    listing = client.get("/api/quarantines", headers=_auth("viewer"))
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["stats"]["open_total"] == 1
    assert body["items"][0]["failure_message"]

    detail = client.get(f"/api/quarantines/{record.id}", headers=_auth("viewer"))
    assert detail.status_code == 200
    assert detail.json()["raw_snapshot"]["shop_coupon"]["minus"] == 80.0

    # viewer 无权更正：真实 403
    forbidden = client.patch(
        f"/api/quarantines/{record.id}/correct",
        json={"shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True}},
        headers=_auth("viewer"),
    )
    assert forbidden.status_code == 403


def test_invalid_correction_returns_422_and_keeps_quarantine_open(client_db):
    client, db = client_db
    client.post("/api/runs", json={"captured_on": TODAY.isoformat(), "trigger": "manual"}, headers=_auth("admin"))
    record = db.scalar(select(SnapshotQuarantine))

    resp = client.patch(
        f"/api/quarantines/{record.id}/correct",
        json={"shop_coupon": {"threshold": 10.0, "minus": 20.0, "stackable": True}},
        headers=_auth("analyst"),
    )
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["code"] == "VALIDATION"
    db.expire_all()
    assert db.get(SnapshotQuarantine, record.id).status == "open"


def test_full_correct_and_retry_loop_over_http(client_db):
    client, db = client_db
    first = client.post("/api/runs", json={"captured_on": TODAY.isoformat(), "trigger": "manual"}, headers=_auth("analyst"))
    first_metrics = first.json()["metrics"]
    assert first_metrics["missing_advice_skus"] == 1
    assert first_metrics["quarantined_open"] == 1

    record = db.scalar(select(SnapshotQuarantine))
    corrected = client.patch(
        f"/api/quarantines/{record.id}/correct",
        json={"shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True}},
        headers=_auth("analyst"),
    )
    assert corrected.status_code == 200
    assert corrected.json()["status"] == "corrected"

    # 重跑同一测算：缺的建议只补一次
    second = client.post("/api/runs", json={"captured_on": TODAY.isoformat(), "trigger": "manual"}, headers=_auth("analyst"))
    metrics = second.json()["metrics"]
    assert metrics["new_advices"] == 1
    assert metrics["missing_advice_skus"] == 0
    assert metrics["quarantine_resolved"] == 1
    assert metrics["quarantine_advice_filled"] == 1

    detail = client.get(f"/api/quarantines/{record.id}", headers=_auth("admin")).json()
    assert detail["status"] == "resolved"
    assert detail["outcome"] == "advice_filled"
    assert detail["advice_id"] is not None

    # 统计与导出
    stats = client.get("/api/quarantines/stats", headers=_auth("admin")).json()
    assert stats["resolved_total"] == 1 and stats["advice_filled"] == 1
    export = client.get("/api/exports/quarantines.csv", headers=_auth("admin"))
    assert export.status_code == 200
    assert "隔离单号".encode() in export.content
    # viewer 导出被拒
    assert client.get("/api/exports/quarantines.csv", headers=_auth("viewer")).status_code == 403


def test_overview_exposes_pipeline_counts(client_db):
    client, _ = client_db
    client.post("/api/runs", json={"captured_on": TODAY.isoformat(), "trigger": "manual"}, headers=_auth("admin"))
    overview = client.get("/api/overview", headers=_auth("viewer")).json()
    assert overview["quarantine"]["open_total"] == 1
    assert overview["pipeline_counts"]["quarantined_open"] == 1
    assert overview["pipeline_counts"]["missing_advice_skus"] == 1


def test_manual_capture_of_bad_snapshot_is_stored_and_quarantined(client_db):
    """手工补录一条券条款写错的快照：HTTP 201 入库不丢，同时自动落隔离单。"""
    client, db = client_db
    sku = db.scalar(select(Sku))
    resp = client.post(
        "/api/snapshots",
        json={
            "sku_id": sku.id,
            "captured_on": (TODAY - timedelta(days=4)).isoformat(),
            "page_price": 199.0,
            "shop_coupon": {"threshold": 30.0, "minus": 99.0, "stackable": True},
            "member_discount": 1.0,
        },
        headers=_auth("admin"),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["valid"] is False
    assert body["quarantined"] is True
    assert body["quarantine_status"] == "open"

    records = db.scalars(select(SnapshotQuarantine)).all()
    assert len(records) == 1
    assert records[0].raw_snapshot["page_price"] == 199.0
    # 快照行确实保留
    assert db.get(Snapshot, body["id"]) is not None

    # analyst 可以补录（SKU_WRITE 实际仅 admin；analyst 补录应 403，隔离台账只读不受影响）
    forbidden = client.post(
        "/api/snapshots",
        json={
            "sku_id": sku.id,
            "captured_on": (TODAY - timedelta(days=5)).isoformat(),
            "page_price": 199.0,
        },
        headers=_auth("analyst"),
    )
    assert forbidden.status_code == 403
