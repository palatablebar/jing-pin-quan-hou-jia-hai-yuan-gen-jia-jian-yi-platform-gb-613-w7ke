import { api } from '../api.js';
import { ENUMS, can, optionList, quarantineStatusTone } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, kv, table, toast } from '../ui.js';
import { couponText, money, priceCompare, signedPct } from '../components/price.js';

const state = { capturedOn: '', competitorId: '', category: '', q: '' };

function unitTable(effective) {
  const units = effective.units || [];
  if (!units.length) return '<p class="muted">这条快照的券配置非法，算法 A 无法还原。</p>';
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>件数</th><th>小计</th><th>满减</th><th>店铺券</th><th>平台券</th><th>会员</th><th>应付</th><th>单价</th></tr></thead>
    <tbody>${units
      .map(
        (unit) => `<tr class="${unit.units === effective.best_units ? 'best-row' : ''}">
          <td class="num">${unit.units}</td>
          <td class="num money">${money(unit.subtotal)}</td>
          <td class="num">${unit.full_reduction.applied ? `−${money(unit.full_reduction.minus)}` : '未触发'}</td>
          <td class="num">${unit.shop_coupon.applied ? `−${money(unit.shop_coupon.minus)}` : '未触发'}</td>
          <td class="num">${unit.platform_coupon.applied ? `−${money(unit.platform_coupon.minus)} ×${unit.platform_coupon.times}` : '未触发'}</td>
          <td class="num">${unit.member_discount < 1 ? `${Math.round(unit.member_discount * 100)}%` : '—'}</td>
          <td class="num money">${money(unit.payable)}</td>
          <td class="num money">${money(unit.unit_price)}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>
    <p class="muted tiny">命中组合按「满减单档 → 店铺券 → 平台跨店券（每满可重复）→ 会员折扣」顺序结算；最优件数 = 单件到手价最低。</p>`;
}

function openSnapshot(view, ctx, item) {
  const { root } = drawer({
    title: `${item.sku_name} · ${item.captured_on}`,
    subtitle: `${item.competitor_name} · 快照 #${item.id}`,
    body: `${priceCompare(item)}
      ${kv([
        ['页面价', `<span class="money">${money(item.page_price)}</span>`],
        ['券配置', esc(couponText(item))],
        ['会员折扣', item.member_discount < 1 ? `${Math.round(item.member_discount * 100)}%` : '非会员价'],
        ['活动', esc(item.campaign || '—')],
        ['库存', esc(item.stock)],
        [
          '算法 A 结论',
          item.quarantined
            ? `<span class="badge danger">${esc(item.quarantine_status_label || '隔离中')}</span> <a class="link" href="#/quarantine">去隔离台更正</a>`
            : esc(item.note || '—'),
        ],
        ['置信度', `${item.valid ? '0.99' : '0.00'}${(item.warnings || []).length ? ` · ${esc(item.warnings.join('；'))}` : ''}`],
      ])}
      <div class="section-label">按件数拆解（算法 A 明细）</div>
      ${unitTable(item.effective || {})}`,
  });
  root.querySelector('[data-close]')?.addEventListener('click', () => {});
}

export const snapshotsPage = {
  async render(view, ctx) {
    let data;
    let qStats = { open_total: 0, pending_correction: 0, corrected_waiting_retry: 0 };
    try {
      data = await api.get('/api/snapshots', {
        captured_on: state.capturedOn,
        competitor_id: state.competitorId,
        category: state.category,
        q: state.q,
        limit: 50,
      });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => snapshotsPage.render(view, ctx));
      return;
    }
    const days = data.capture_days || [];
    if (!state.capturedOn && days.length) state.capturedOn = days[0];
    try {
      qStats = await api.get('/api/quarantines/stats', { captured_on: state.capturedOn || undefined });
    } catch {
      qStats = { open_total: 0 };
    }
    const competitors = await api.get('/api/competitors', { limit: 20 }).catch(() => ({ items: [] }));
    const quarantinedRows = data.items.filter((row) => row.quarantined).length;
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>抓价快照 · 页面价 vs 券后价</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 条快照 · 快照日 ${days.length} 天 · 未闭环隔离 ${qStats.open_total ?? 0} 条</span>
            ${can(ctx.user.role, 'run:create') ? '<button class="btn sm accent" id="run-calc">重跑今天测算</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          <div class="stat-strip" data-testid="snapshot-pipeline">
            <div class="stat"><span class="k">本页输入快照</span><span class="v">${data.total}</span><span class="hint">含坏快照，原输入不丢</span></div>
            <div class="stat"><span class="k">隔离中</span><span class="v">${qStats.open_total ?? 0}</span><span class="hint">待更正 ${qStats.pending_correction ?? 0} · 待重试 ${qStats.corrected_waiting_retry ?? 0}</span></div>
            <div class="stat"><span class="k">列表内隔离行</span><span class="v">${quarantinedRows}</span><span class="hint">带红色隔离标记</span></div>
            <div class="stat"><span class="k">合格快照</span><span class="v">${data.total - quarantinedRows}</span><span class="hint">算法 A 已还原券后价</span></div>
          </div>
          ${filterBar('snap-filters', [
            field({ label: '快照日期', name: 'captured_on', value: state.capturedOn, options: days.map((day) => ({ value: day, label: day })) }),
            field({ label: '类目', name: 'category', value: state.category, options: optionList(ENUMS.category) }),
            field({
              label: '竞品店铺',
              name: 'competitor_id',
              value: state.competitorId,
              options: [{ value: '', label: '全部' }, ...(competitors.items || []).map((item) => ({ value: item.id, label: item.name }))],
            }),
            field({ label: '搜索 SKU', name: 'q', value: state.q, placeholder: '名称 / 编码' }),
          ])}
        </div>
        ${table({
          rows: data.items,
          empty: { title: '这个筛选下没有快照', hint: '换一天，或先跑一次抓价回放。' },
          columns: [
            {
              key: 'sku_name',
              label: 'SKU / 竞品',
              render: (row) =>
                `<b>${esc(row.sku_name)}</b><div class="mono muted">${esc(row.sku_code)} · ${esc(row.competitor_name || '—')}</div>`,
            },
            {
              key: 'status',
              label: '状态',
              render: (row) =>
                row.quarantined
                  ? `<span class="badge ${quarantineStatusTone(row.quarantine_status)}" data-testid="quarantine-badge">${esc(row.quarantine_status_label || '隔离中')}</span>`
                  : '<span class="badge ok">合格</span>',
            },
            { key: 'page_price', label: '页面价', className: 'num money', render: (row) => money(row.page_price) },
            { key: 'unit_price_1', label: '券后 1 件', className: 'num money', render: (row) => money(row.unit_price_1) },
            { key: 'unit_price_2', label: '券后 2 件', className: 'num money', render: (row) => money(row.unit_price_2) },
            { key: 'unit_price_3', label: '券后 3 件', className: 'num money', render: (row) => money(row.unit_price_3) },
            {
              key: 'discount_pct',
              label: '券后折扣',
              className: 'num',
              render: (row) => `<span class="badge info">${signedPct(-Math.abs(Number(row.discount_pct)))}</span>`,
            },
            {
              key: 'note',
              label: '命中组合 / 隔离原因',
              render: (row) =>
                row.quarantined
                  ? `<a class="link" href="#/quarantine" data-quarantine-id="${esc(row.quarantine_id)}">查看隔离单 #${esc(row.quarantine_id)}</a>`
                  : `<span class="mono muted ellipsis">${esc(row.note || '—')}</span>`,
            },
          ],
        })}
      </section>`;

    const filters = view.querySelector('#snap-filters');
    filters?.addEventListener('change', () => {
      state.capturedOn = filters.querySelector('[name=captured_on]').value;
      state.category = filters.querySelector('[name=category]').value;
      state.competitorId = filters.querySelector('[name=competitor_id]').value;
      state.q = filters.querySelector('[name=q]').value.trim();
      snapshotsPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        snapshotsPage.render(view, ctx);
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', async () => {
        try {
          const detail = await api.get(`/api/snapshots/${row.dataset.id}`);
          openSnapshot(view, ctx, detail);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
    view.querySelector('#run-calc')?.addEventListener('click', async () => {
      const button = view.querySelector('#run-calc');
      button.disabled = true;
      button.textContent = '测算中…';
      try {
        const run = await api.post('/api/runs', { captured_on: state.capturedOn || undefined, trigger: 'manual' });
        const m = run.metrics || {};
        toast(
          `${run.run_no}：输入 ${m.snapshots ?? 0} 条 → 建议 ${m.advices ?? 0} 条（新补 ${m.new_advices ?? 0}、沿用 ${m.skipped_existing_advices ?? 0}），隔离未闭环 ${m.quarantined_open ?? 0}、缺建议 ${m.missing_advice_skus ?? 0}`,
        );
        await snapshotsPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
        button.textContent = '重跑今天测算';
      }
    });
  },
};

export default snapshotsPage;
