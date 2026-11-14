"""测算主流程：抓价回放 → 算法 A（券后价还原）→ 算法 B（跟价建议）→ 建议入库。

纯服务端计算，同样的快照与规则两次运行结果一致；不访问外网。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.advice import Advice
from src.models.competitor import Competitor
from src.models.run import Run
from src.models.sku import Sku
from src.models.snapshot import Snapshot
from src.services import (
    effective_price,
    follow_advice,
    quarantine_service,
    rule_service,
    run_service,
)
from src.utils.timeutil import utcnow

CAPTURE_WINDOW_DAYS = 3


def _load_window(db: Session, captured_on: date) -> list[Snapshot]:
    start = captured_on - timedelta(days=CAPTURE_WINDOW_DAYS - 1)
    return list(
        db.scalars(
            select(Snapshot)
            .where(Snapshot.captured_on >= start, Snapshot.captured_on <= captured_on)
            .order_by(Snapshot.captured_on, Snapshot.id)
        ).all()
    )


def _upsert_advice(db: Session, run: Run, existing: dict[int, Advice], sku: Sku, snapshot: Snapshot, result: dict) -> Advice:
    advice = existing.get(sku.id)
    payload = {
        "algorithm": "B.follow_advice",
        "effective_algorithm": "A.effective_price",
        "current_price": round(sku.current_price, 2),
        "cost": round(sku.cost, 2),
        "floor_price": round(sku.floor_price, 2),
        "target_margin_pct": sku.target_margin_pct,
        "rule": result["strategy"],
        "cost_floor": result["cost_floor"],
        "market_cap": result["market_cap"],
        "clamped_by": result["clamped_by"],
        "breach": result["breach"],
        "reasons": result["reasons"],
        "p10": result["p10"],
        "p25": result["p25"],
        "p50": result["p50"],
        "mean": result["mean"],
        "samples": result["samples"],
        "snapshot": {
            "id": snapshot.id,
            "captured_on": snapshot.captured_on.isoformat(),
            "page_price": round(snapshot.page_price, 2),
            "full_reduction": snapshot.full_reduction or [],
            "shop_coupon": snapshot.shop_coupon,
            "platform_coupon": snapshot.platform_coupon,
            "member_discount": snapshot.member_discount,
            "unit_price_1": (snapshot.effective or {}).get("unit_price_1"),
            "unit_price_2": (snapshot.effective or {}).get("unit_price_2"),
            "unit_price_3": (snapshot.effective or {}).get("unit_price_3"),
            "best_units": (snapshot.effective or {}).get("best_units"),
            "note": (snapshot.effective or {}).get("note"),
        },
    }
    title = f"{sku.name}：竞品券后 ¥{result['p25']:.2f} → 建议 ¥{result['suggested_price']:.2f}"
    summary = result["reason"]
    if advice is None:
        advice = Advice(
            run_id=run.id,
            sku_id=sku.id,
            snapshot_id=snapshot.id,
            competitor_id=sku.competitor_id,
            rule_code=result["rule_code"],
            title=title[:200],
            summary=summary,
            action=result["action"],
            severity=result["risk"],
            score=abs(result["gap_pct"]),
            status="pending",
            current_price=sku.current_price,
            suggested_price=result["suggested_price"],
            effective_price=result["p25"],
            p10=result["p10"],
            p25=result["p25"],
            p50=result["p50"],
            margin_pct=result["margin_pct"],
            gap_pct=result["gap_pct"],
            payload=payload,
        )
        db.add(advice)
    else:
        # 重算刷新数值，但绝不覆盖审批状态与审批意见
        advice.snapshot_id = snapshot.id
        advice.competitor_id = sku.competitor_id
        advice.rule_code = result["rule_code"]
        advice.title = title[:200]
        advice.summary = summary
        advice.action = result["action"]
        advice.severity = result["risk"]
        advice.score = abs(result["gap_pct"])
        advice.current_price = sku.current_price
        advice.suggested_price = result["suggested_price"]
        advice.effective_price = result["p25"]
        advice.p10 = result["p10"]
        advice.p25 = result["p25"]
        advice.p50 = result["p50"]
        advice.margin_pct = result["margin_pct"]
        advice.gap_pct = result["gap_pct"]
        advice.payload = payload
    db.flush()
    return advice


def execute(db: Session, run: Run, captured_on: date, competitor_filter: int | None = None) -> dict:
    """执行一次测算，写入 run.metrics / run.log_text，返回 {metrics, log, status}。

    隔离与幂等口径：
    - 算法 A 失败的快照不丢弃：原样保留并落/刷新隔离单（quarantine_service），原输入不丢失。
    - 同一运行内已存在建议的 SKU 直接跳过，不重算、不重复；只为「缺建议」的 SKU 补一次。
    - 隔离快照更正合格后重跑，只补该货号缺失的建议，已出建议保持不动。
    """
    window = _load_window(db, captured_on)
    window_start = captured_on - timedelta(days=CAPTURE_WINDOW_DAYS - 1)
    skus = {row.id: row for row in db.scalars(select(Sku)).all()}
    competitors = {row.id: row for row in db.scalars(select(Competitor)).all()}
    rules = rule_service.effective_rules(db)

    prices_by_sku: dict[int, list[float]] = {}
    invalid = 0
    # 当日快照未通过算法 A 的货号：同货号当天锁死，不出建议，等更正后再补
    locked_sku_ids: set[int] = set()
    for snapshot in window:
        result = effective_price.compute_effective_price(
            {
                "page_price": snapshot.page_price,
                "full_reduction": snapshot.full_reduction or [],
                "shop_coupon": snapshot.shop_coupon,
                "platform_coupon": snapshot.platform_coupon,
                "member_discount": snapshot.member_discount,
            }
        )
        snapshot.effective = result
        if not result["valid"]:
            invalid += 1
            # 坏快照隔离：原输入留档、记录失败原因，同货号当天只维护一条未闭环单
            quarantine_service.quarantine_snapshot(db, snapshot, result, run_id=run.id)
            if snapshot.captured_on == captured_on:
                locked_sku_ids.add(snapshot.sku_id)
            continue
        prices_by_sku.setdefault(snapshot.sku_id, []).append(result["unit_price_1"])

    targets = [
        snapshot
        for snapshot in window
        if snapshot.captured_on == captured_on
        and skus.get(snapshot.sku_id) is not None
        and skus[snapshot.sku_id].enabled
        and (competitor_filter is None or snapshot.competitor_id == competitor_filter)
    ]

    existing = {
        row.sku_id: row
        for row in db.scalars(select(Advice).where(Advice.run_id == run.id)).all()
    }
    advice_count = len(existing)  # 已出建议不重算：基数为运行内既有建议数
    risk_bucket = {"high": 0, "medium": 0, "low": 0}
    gaps: list[float] = []
    discounts: list[float] = []
    blocked_sku_ids: set[int] = set()  # 缺建议的货号：被坏快照挡住，或算法 B 无法出数
    new_advice_sku_ids: set[int] = set()
    for snapshot in targets:
        sku = skus[snapshot.sku_id]
        competitor = competitors.get(snapshot.competitor_id)
        rule = rule_service.match_rule(rules, sku, competitor.code if competitor else None)
        if sku.id in locked_sku_ids:
            # 当日快照被隔离：同货号当天锁死，缺建议待更正重试后只补一次
            blocked_sku_ids.add(sku.id)
            continue
        prices = prices_by_sku.get(sku.id, [])
        if not prices:
            # 窗口内无任何有效价：这条货号缺建议，待更正后补
            blocked_sku_ids.add(sku.id)
            continue
        # 幂等：该运行已给此 SKU 出过建议就不再重算，建议不重复
        if sku.id in existing:
            continue
        try:
            result = follow_advice.build_advice(
                competitor_prices=prices,
                cost=sku.cost,
                floor_price=sku.floor_price,
                target_margin_pct=rule.target_margin_pct,
                current_price=sku.current_price,
                strategy=rule.strategy,
                max_drop_pct=rule.max_drop_pct,
            )
        except ValueError:
            # 算法 B 无法构建建议（如样本为空等）：同样算缺建议，留待后续补
            blocked_sku_ids.add(sku.id)
            continue
        result["rule_code"] = rule.code
        if (snapshot.effective or {}).get("discount_pct"):
            discounts.append(float(snapshot.effective["discount_pct"]))
        if not result["need_follow"]:
            # 规则判定无需跟价：属于「合格但无需建议」，不计入缺建议
            continue
        created_advice = _upsert_advice(db, run, existing, sku, snapshot, result)
        existing[sku.id] = created_advice  # 防止同运行内重复补单
        new_advice_sku_ids.add(sku.id)
        advice_count += 1
        risk_bucket[result["risk"]] = risk_bucket.get(result["risk"], 0) + 1
        gaps.append(result["gap_pct"])

    # 评估窗口内的未闭环隔离单：合格的闭环并关联本次补出的建议，仍坏的保持隔离
    db.flush()  # 确保本运行新增的隔离单对后续 SELECT 可见（session 配置了 autoflush=False）
    quarantine_stats = quarantine_service.evaluate_during_run(
        db,
        run,
        window_start,
        captured_on,
        sku_advice_ids={sku_id: existing[sku_id].id for sku_id in new_advice_sku_ids},
    )

    captured_at = utcnow()
    for competitor_id in {snapshot.competitor_id for snapshot in targets}:
        competitor = competitors.get(competitor_id)
        if competitor is not None:
            competitor.last_captured_at = captured_at

    target_sku_ids = {snapshot.sku_id for snapshot in targets}
    metrics = {
        "window_snapshots": len(window),
        "snapshots": len(targets),
        "skus": len(target_sku_ids),
        "advices": advice_count,
        "new_advices": len(new_advice_sku_ids),
        "skipped_existing_advices": advice_count - len(new_advice_sku_ids),
        "missing_advice_skus": len(blocked_sku_ids),
        "high": risk_bucket.get("high", 0),
        "medium": risk_bucket.get("medium", 0),
        "low": risk_bucket.get("low", 0),
        "avg_gap_pct": round(sum(gaps) / len(gaps), 2) if gaps else 0.0,
        "avg_discount_pct": round(sum(discounts) / len(discounts), 2) if discounts else 0.0,
        "invalid_snapshots": invalid,
        "quarantined_total": quarantine_service_open_count(db),
        "quarantined_open": quarantine_service_open_count(db, open_only=True),
        "quarantine_resolved": quarantine_stats["resolved"],
        "quarantine_re_isolated": quarantine_stats["re_isolated"],
        "quarantine_advice_filled": quarantine_stats["advice_filled"],
        "rules_applied": len({rule.code for rule in rules}),
        "window_days": CAPTURE_WINDOW_DAYS,
        "captured_on": captured_on.isoformat(),
    }
    log = (
        f"[{captured_on.isoformat()}] 回放窗口 {CAPTURE_WINDOW_DAYS} 天，快照 {len(window)} 条；"
        f"当日待测算 {len(targets)} 条 SKU；券后价还原失败 {invalid} 条，"
        f"隔离单闭环 {quarantine_stats['resolved']} 条（补建议 {quarantine_stats['advice_filled']} 条）、"
        f"仍隔离 {quarantine_stats['re_isolated']} 条；"
        f"本次新补建议 {len(new_advice_sku_ids)} 条、沿用既有建议 {advice_count - len(new_advice_sku_ids)} 条，"
        f"缺建议货号 {len(blocked_sku_ids)} 个；"
        f"风险分布（高 {risk_bucket.get('high', 0)} / 中 {risk_bucket.get('medium', 0)} / 低 {risk_bucket.get('low', 0)}）；"
        f"平均价差 {metrics['avg_gap_pct']}%，平均券后折扣 {metrics['avg_discount_pct']}%。"
    )
    status = "success" if metrics["snapshots"] > 0 else "partial"
    if invalid and invalid >= max(1, metrics["snapshots"] // 2):
        status = "partial"
    return {"metrics": metrics, "log": log, "status": status}


def quarantine_service_open_count(db: Session, *, open_only: bool = False) -> int:
    """运行指标用的隔离单计数（总数 / 未闭环数），避免在主流程里重复写查询。"""
    from src.models.quarantine import SnapshotQuarantine

    stmt = select(func.count()).select_from(SnapshotQuarantine)
    if open_only:
        stmt = stmt.where(SnapshotQuarantine.status.in_(quarantine_service.OPEN_STATUSES))
    return db.scalar(stmt) or 0


def compute_run(
    db: Session,
    *,
    captured_on: date,
    operator_email: str,
    trigger: str = "manual",
    seed_key: str | None = None,
    competitor_id: int | None = None,
) -> Run:
    db.flush()  # autoflush=False：保证同 session 内未提交的 Run 也能被自然键查到
    run = None
    if seed_key:
        run = db.scalar(select(Run).where(Run.seed_key == seed_key))

    # 幂等重试：复用同一 Run 重新执行测算。
    # execute 只补缺建议、不重算既有建议，因此 seed 重跑结果恒定；无 seed 的手动重跑会新建 Run。
    if run is None:
        run = Run(
            run_no=run_service.next_run_no(db),
            trigger=trigger,
            status="running",
            operator_email=operator_email,
            captured_on=captured_on,
            competitor_id=competitor_id,
            seed_key=seed_key,
        )
        db.add(run)
        db.flush()
    else:
        run.status = "running"
        run.finished_at = None
    try:
        result = execute(db, run, captured_on, competitor_id)
        run.metrics = result["metrics"]
        run.log_text = result["log"]
        run.status = result["status"]
        run.input_count = result["metrics"]["snapshots"]
        run.output_count = result["metrics"]["advices"]
    except Exception as exc:  # noqa: BLE001 - 失败留痕，不抛 500
        run.status = "failed"
        run.log_text = f"测算失败：{exc}"
        run.metrics = {"error": str(exc)[:300]}
    run.finished_at = utcnow()
    db.flush()
    return run
