"""坏快照隔离 → 更正 → 重试同一测算的端到端链路（内存 SQLite，不依赖外部服务）。

覆盖需求口径：
- 券条款写错的快照仍入库（原输入不丢），同时生成可处理的隔离信息；
- 同货号当天只有一条未闭环隔离单，测算继续发布其余建议；
- 按正确条款更正后重跑同一测算：合格建议不重算、不重复，缺的只补一次；
- 隔离单能从失败原因追溯到更正结果（advice_filled / no_advice_needed / still_invalid）。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.models import Base, Advice, Competitor, Rule, Run, Sku, Snapshot, SnapshotQuarantine
from src.services import effective_price, pricing_service, quarantine_service

TODAY = date(2026, 11, 14)
VALID_SPEC = {
    "page_price": 299.0,
    "full_reduction": [{"threshold": 300.0, "minus": 40.0}],
    "shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True},
    "platform_coupon": {"per": 300.0, "minus": 50.0},
    "member_discount": 1.0,
}
# 券面 > 门槛：算法 A 必判非法
BAD_SPEC = dict(VALID_SPEC, shop_coupon={"threshold": 50.0, "minus": 80.0, "stackable": True})


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    _seed_catalog(session)
    yield session
    session.close()
    engine.dispose()


def _seed_catalog(db) -> None:
    competitor = Competitor(
        code="dy-muyan", name="木言家居旗舰店", platform="douyin",
        shop_url="https://example.test/shop", capture_config={}, enabled=True,
    )
    db.add(competitor)
    db.flush()
    # 两个 SKU：A 会被坏快照挡住，B 全程正常（用来验证其余建议照常发布）
    for code, name in (("PB-sofa-001", "云漫布艺沙发"), ("PB-sofa-002", "青屿单人沙发")):
        db.add(
            Sku(
                sku_code=code, name=name, category="sofa", competitor_id=competitor.id,
                unit="件", cost=191.4, floor_price=239.25, target_margin_pct=12,
                current_price=319.0, stock=10, enabled=True,
            )
        )
    db.add(
        Rule(
            code="rule-global", name="全局有底线跟价", scope_kind="global",
            strategy="guarded", target_margin_pct=12, max_drop_pct=15, priority=100, enabled=True,
        )
    )
    db.flush()


def _add_snapshot(db, sku: Sku, competitor: Competitor, day: date, spec: dict) -> Snapshot:
    snapshot = Snapshot(
        sku_id=sku.id, competitor_id=competitor.id, captured_on=day,
        page_price=spec["page_price"], full_reduction=spec.get("full_reduction") or [],
        shop_coupon=spec.get("shop_coupon"), platform_coupon=spec.get("platform_coupon"),
        member_discount=spec.get("member_discount", 1.0), stock=5, campaign="早班抓价",
        effective=effective_price.compute_effective_price(spec),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _skus(db) -> tuple[Sku, Sku]:
    rows = {row.sku_code: row for row in db.scalars(select(Sku)).all()}
    return rows["PB-sofa-001"], rows["PB-sofa-002"]


def _run(db, captured_on: date = TODAY, seed_key: str | None = None) -> Run:
    return pricing_service.compute_run(
        db, captured_on=captured_on, operator_email="analyst@pricewatch.io",
        trigger="manual", seed_key=seed_key,
    )


def test_bad_snapshot_is_stored_and_quarantined_while_other_advice_publishes(db):
    sku_a, sku_b = _skus(db)
    competitor = db.scalar(select(Competitor))
    # 近三天：A 前两天正常、今天券条款写错；B 三天都正常
    for offset in (2, 1):
        _add_snapshot(db, sku_a, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    bad = _add_snapshot(db, sku_a, competitor, TODAY, BAD_SPEC)
    for offset in (2, 1, 0):
        _add_snapshot(db, sku_b, competitor, TODAY - timedelta(days=offset), VALID_SPEC)

    # 坏快照没有被丢弃：行还在，effective 标记非法
    assert db.get(Snapshot, bad.id) is not None
    assert bad.effective["valid"] is False

    run = _run(db)
    metrics = run.metrics
    quarantines = db.scalars(select(SnapshotQuarantine)).all()

    # 生成一条隔离单，原输入完整留档，失败原因可处理
    assert len(quarantines) == 1
    record = quarantines[0]
    assert record.snapshot_id == bad.id
    assert record.status == "open"
    assert record.raw_snapshot["shop_coupon"]["minus"] == 80.0
    assert "非法" in record.failure_reason["message"]
    # 测算继续：B 的建议照常发布，A 因隔离缺建议
    assert metrics["invalid_snapshots"] >= 1
    assert metrics["missing_advice_skus"] == 1
    assert metrics["advices"] == 1
    advice_skus = {row.sku_id for row in db.scalars(select(Advice).where(Advice.run_id == run.id)).all()}
    assert advice_skus == {sku_b.id}
    assert run.status == "partial"


def test_correction_requires_valid_coupon_terms(db):
    sku_a, _ = _skus(db)
    competitor = db.scalar(select(Competitor))
    for offset in (2, 1):
        _add_snapshot(db, sku_a, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    bad = _add_snapshot(db, sku_a, competitor, TODAY, BAD_SPEC)
    _run(db)
    record = db.scalar(select(SnapshotQuarantine).where(SnapshotQuarantine.snapshot_id == bad.id))

    # 提交一份仍然非法的更正：422，隔离单保持 open，快照原输入不变
    from src.utils.errors import AppError

    record_id = record.id
    snapshot_id = bad.id
    with pytest.raises(AppError) as exc:
        quarantine_service.correct_snapshot(
            db, record,
            {"shop_coupon": {"threshold": 10.0, "minus": 20.0, "stackable": True}},
            operator_email="analyst@pricewatch.io",
        )
    assert exc.value.status_code == 422
    # 更正被拒：不提交、不改动，隔离单仍可继续更正（这里显式 expire 后重新读取持久态）
    db.expire_all()
    record = db.get(SnapshotQuarantine, record_id)
    assert record.status == "open"
    assert record.retry_count == 0
    assert db.get(Snapshot, snapshot_id).shop_coupon["minus"] == 80.0


def test_correct_then_retry_fills_missing_advice_once_without_recomputing_others(db):
    sku_a, sku_b = _skus(db)
    competitor = db.scalar(select(Competitor))
    for offset in (2, 1):
        _add_snapshot(db, sku_a, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    bad = _add_snapshot(db, sku_a, competitor, TODAY, BAD_SPEC)
    for offset in (2, 1, 0):
        _add_snapshot(db, sku_b, competitor, TODAY - timedelta(days=offset), VALID_SPEC)

    first_run = _run(db, seed_key="run-morning")
    b_advice_before = db.scalar(select(Advice).where(Advice.run_id == first_run.id, Advice.sku_id == sku_b.id))
    assert b_advice_before is not None

    # 更正：把店铺券改成合法条款，算法 A 立即通过，隔离单转「已更正待重试」
    record = db.scalar(select(SnapshotQuarantine).where(SnapshotQuarantine.snapshot_id == bad.id))
    record, snapshot, result = quarantine_service.correct_snapshot(
        db, record,
        {"shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True}},
        operator_email="analyst@pricewatch.io",
    )
    db.commit()
    assert result["valid"] is True
    assert record.status == "corrected"
    assert record.retry_count == 1
    # 原始错误输入仍留档，快照行已更新为正确条款
    assert record.raw_snapshot["shop_coupon"]["minus"] == 80.0
    assert db.get(Snapshot, bad.id).shop_coupon["minus"] == 20.0

    # 重试同一测算（同一天、同一运行自然键）：只补缺的，B 的建议不重算、不重复
    second_run = _run(db, seed_key="run-morning")
    assert second_run.id == first_run.id  # 同一运行，不新开 run
    advices = db.scalars(select(Advice).where(Advice.run_id == first_run.id)).all()
    assert {row.sku_id for row in advices} == {sku_a.id, sku_b.id}
    assert len(advices) == 2  # 没有重复建议
    b_advice_after = db.scalar(select(Advice).where(Advice.run_id == first_run.id, Advice.sku_id == sku_b.id))
    assert b_advice_after.id == b_advice_before.id  # 既有建议原封不动

    metrics = second_run.metrics
    assert metrics["new_advices"] == 1
    assert metrics["skipped_existing_advices"] == 1
    assert metrics["missing_advice_skus"] == 0

    # 隔离单闭环并关联补出的建议，原因 → 更正 → 结果链路完整
    closed = db.get(SnapshotQuarantine, record.id)
    assert closed.status == "resolved"
    assert closed.outcome == "advice_filled"
    assert closed.advice_id is not None
    assert closed.resolved_at is not None
    filled = db.get(Advice, closed.advice_id)
    assert filled.sku_id == sku_a.id


def test_repeated_retry_is_idempotent(db):
    sku_a, sku_b = _skus(db)
    competitor = db.scalar(select(Competitor))
    for offset in (2, 1):
        _add_snapshot(db, sku_a, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    bad = _add_snapshot(db, sku_a, competitor, TODAY, BAD_SPEC)
    for offset in (2, 1, 0):
        _add_snapshot(db, sku_b, competitor, TODAY - timedelta(days=offset), VALID_SPEC)

    run = _run(db, seed_key="run-keep")
    record = db.scalar(select(SnapshotQuarantine).where(SnapshotQuarantine.snapshot_id == bad.id))
    quarantine_service.correct_snapshot(
        db, record,
        {"shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True}},
        operator_email="analyst@pricewatch.io",
    )
    db.commit()

    _run(db, seed_key="run-keep")
    third = _run(db, seed_key="run-keep")
    # 再跑两次：建议数恒定、隔离单仍是同一条且已闭环
    assert db.scalar(select(func.count()).select_from(Advice).where(Advice.run_id == run.id)) == 2
    assert db.scalar(select(func.count()).select_from(SnapshotQuarantine)) == 1
    closed = db.get(SnapshotQuarantine, record.id)
    assert closed.status == "resolved"
    assert third.metrics["new_advices"] == 0


def test_still_invalid_correction_re_isolates_and_preserves_trace(db):
    sku_a, _ = _skus(db)
    competitor = db.scalar(select(Competitor))
    for offset in (2, 1):
        _add_snapshot(db, sku_a, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    bad = _add_snapshot(db, sku_a, competitor, TODAY, BAD_SPEC)
    _run(db)
    record = db.scalar(select(SnapshotQuarantine).where(SnapshotQuarantine.snapshot_id == bad.id))

    # 先更正成合法（状态变 corrected），再人为把快照改回非法条款，重跑应重新隔离
    quarantine_service.correct_snapshot(
        db, record,
        {"shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True}},
        operator_email="analyst@pricewatch.io",
    )
    db.commit()
    snapshot = db.get(Snapshot, bad.id)
    snapshot.shop_coupon = {"threshold": 50.0, "minus": 80.0, "stackable": True}
    db.commit()

    run = _run(db)
    reopened = db.get(SnapshotQuarantine, record.id)
    assert reopened.status == "re_isolated"
    assert reopened.outcome == "still_invalid"
    assert reopened.advice_id is None
    assert run.metrics["missing_advice_skus"] == 1
    # 历史更正记录仍保留，可追溯
    assert reopened.corrected_snapshot is not None
    assert reopened.retry_count == 1


def test_resolved_no_advice_needed_when_rule_says_hold(db):
    sku_a, sku_b = _skus(db)
    competitor = db.scalar(select(Competitor))
    # 给 A 一条 hold 规则：即使快照合格也不产出建议
    db.add(
        Rule(
            code="rule-hold-a", name="A 只守不降", scope_kind="sku", scope_value="PB-sofa-001",
            sku_id=sku_a.id, strategy="hold", target_margin_pct=12, max_drop_pct=15,
            priority=10, enabled=True,
        )
    )
    db.flush()
    for offset in (2, 1):
        _add_snapshot(db, sku_a, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    bad = _add_snapshot(db, sku_a, competitor, TODAY, BAD_SPEC)
    for offset in (2, 1, 0):
        _add_snapshot(db, sku_b, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    _run(db)
    record = db.scalar(select(SnapshotQuarantine).where(SnapshotQuarantine.snapshot_id == bad.id))
    quarantine_service.correct_snapshot(
        db, record,
        {"shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True}},
        operator_email="analyst@pricewatch.io",
    )
    db.commit()
    _run(db)

    closed = db.get(SnapshotQuarantine, record.id)
    assert closed.status == "resolved"
    assert closed.outcome == "no_advice_needed"
    assert closed.advice_id is None


def test_stats_and_serialization_roundtrip(db):
    sku_a, sku_b = _skus(db)
    competitor = db.scalar(select(Competitor))
    for offset in (2, 1):
        _add_snapshot(db, sku_a, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    bad = _add_snapshot(db, sku_a, competitor, TODAY, BAD_SPEC)
    for offset in (2, 1, 0):
        _add_snapshot(db, sku_b, competitor, TODAY - timedelta(days=offset), VALID_SPEC)
    _run(db)

    stats = quarantine_service.stats(db)
    assert stats["total"] == 1
    assert stats["open_total"] == 1
    assert stats["pending_correction"] == 1
    assert stats["resolved_total"] == 0

    record = db.scalar(select(SnapshotQuarantine).where(SnapshotQuarantine.snapshot_id == bad.id))
    payload = quarantine_service.serialize(db, record, detail=True)
    assert payload["sku_code"] == "PB-sofa-001"
    assert payload["raw_snapshot"]["page_price"] == 299.0
    assert "message" in payload["failure_reason"]

    listing = quarantine_service.list_quarantines(db, status="open", limit=10, offset=0)
    assert listing["total"] == 1 and len(listing["items"]) == 1
    assert listing["stats"]["open_total"] == 1
