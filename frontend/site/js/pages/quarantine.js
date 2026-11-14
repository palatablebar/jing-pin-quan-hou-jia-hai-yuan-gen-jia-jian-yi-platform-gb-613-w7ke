import { api } from '../api.js';
import { ENUMS, can, optionList, quarantineStatusTone, quarantineOutcomeTone } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, kv, table, toast } from '../ui.js';
import { couponText, money } from '../components/price.js';

const state = { status: '', outcome: '', q: '', capturedOn: '' };

function statStrip(stats) {
  const items = [
    { label: '隔离单总数', value: stats.total, hint: '全部坏快照留档' },
    { label: '待更正', value: stats.pending_correction, hint: '券条款写错，等待处理' },
    { label: '已更正待重试', value: stats.corrected_waiting_retry, hint: '重跑测算后自动闭环' },
    { label: '更正后仍隔离', value: stats.re_isolated, hint: '更正条款仍不合法' },
    { label: '已闭环', value: stats.resolved_total, hint: `其中补出建议 ${stats.advice_filled} 条` },
  ];
  return `<div class="stat-strip" data-testid="quarantine-stats">${items
    .map(
      (item) => `<div class="stat">
        <span class="k">${esc(item.label)}</span>
        <span class="v">${esc(item.value)}</span>
        <span class="hint">${esc(item.hint)}</span>
      </div>`,
    )
    .join('')}</div>`;
}

function specText(spec) {
  if (!spec || !Object.keys(spec).length) return '—';
  return esc(couponText(spec));
}

function diffBlock(raw, corrected) {
  const fields = [
    ['页面价', 'page_price', (v) => money(v)],
    ['满减', 'full_reduction', (v) => (v && v.length ? esc(v.map((t) => `满${t.threshold}减${t.minus}`).join('＋')) : '无')],
    ['店铺券', 'shop_coupon', (v) => (v ? esc(`满${v.threshold}减${v.minus}${v.stackable === false ? '（不可叠加）' : ''}`) : '无')],
    ['平台券', 'platform_coupon', (v) => (v ? esc(`每满${v.per}减${v.minus}`) : '无')],
    ['会员折扣', 'member_discount', (v) => (Number(v) < 1 ? `${Math.round(Number(v) * 100)}%` : '非会员价')],
  ];
  const rows = fields
    .map(([label, key, fmt]) => {
      const before = raw ? raw[key] : undefined;
      const after = corrected ? corrected[key] : undefined;
      const changed = JSON.stringify(before) !== JSON.stringify(after);
      return `<tr class="${changed ? 'best-row' : ''}">
        <td>${esc(label)}</td>
        <td>${before === undefined ? '—' : fmt(before)}</td>
        <td>${after === undefined ? '—' : fmt(after)}</td>
      </tr>`;
    })
    .join('');
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>字段</th><th>原始输入（留档不丢）</th><th>更正后条款</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function correctForm(item) {
  const raw = item.raw_snapshot || {};
  const current = item.corrected_snapshot || raw;
  return `<form id="correct-form" class="form-grid">
    <p class="muted tiny">按正确券条款更正，提交前后端会重算算法 A；不通过会返回 422 且隔离单保持可继续更正。留空字段沿用现值。</p>
    ${field({ label: '页面价', name: 'page_price', type: 'number', value: current.page_price ?? '', attrs: 'step="0.01" min="0.01"' })}
    ${field({ label: '会员折扣（0.5~1）', name: 'member_discount', type: 'number', value: current.member_discount ?? 1, attrs: 'step="0.01" min="0.5" max="1"' })}
    <div class="field"><label for="shop_coupon">店铺券（JSON，可清空）</label>
      <input name="shop_coupon" id="shop_coupon" value="${esc(JSON.stringify(current.shop_coupon ?? null))}" placeholder='{"threshold":200,"minus":20,"stackable":true}' /></div>
    <div class="field"><label for="platform_coupon">平台券（JSON，可清空）</label>
      <input name="platform_coupon" id="platform_coupon" value="${esc(JSON.stringify(current.platform_coupon ?? null))}" placeholder='{"per":300,"minus":50}' /></div>
    <div class="field"><label for="full_reduction">满减阶梯（JSON 数组）</label>
      <input name="full_reduction" id="full_reduction" value="${esc(JSON.stringify(current.full_reduction ?? []))}" placeholder='[{"threshold":300,"minus":40}]' /></div>
    ${field({ label: '更正备注', name: 'note', value: item.note || '', placeholder: '例如：店铺券门槛应为 200，原抓价串位' })}
    <div class="form-error" id="correct-error"></div>
    <button class="btn accent" type="submit">校验并更正</button>
  </form>`;
}

function parseJsonField(form, name) {
  const text = form[name].value.trim();
  if (!text || text === 'null') return null;
  return JSON.parse(text);
}

function openDetail(view, ctx, itemId) {
  api
    .get(`/api/quarantines/${itemId}`)
    .then((item) => {
      const canCorrect = can(ctx.user.role, 'run:create') && item.status !== 'resolved';
      const { root, close } = drawer({
        title: `${item.sku_name} · ${item.captured_on}`,
        subtitle: `${item.sku_code} · ${item.competitor_name || '—'} · 隔离单 #${item.id}`,
        body: `
          ${kv([
            ['隔离状态', `<span class="badge ${quarantineStatusTone(item.status)}">${esc(item.status_label)}</span>`],
            ['闭环结果', `<span class="badge ${quarantineOutcomeTone(item.outcome)}">${esc(item.outcome_label)}</span>`],
            ['失败原因', esc(item.failure_reason?.message || '—')],
            ['触发运行', item.run_no ? esc(item.run_no) : '补录阶段隔离'],
            ['重试次数', esc(item.retry_count)],
            ['更正人', esc(item.corrected_by || '—')],
            ['更正时间', esc(item.corrected_at || '—')],
            ['补出建议', item.advice_id ? `#${esc(item.advice_id)}` : '—'],
            ['闭环时间', esc(item.resolved_at || '—')],
          ])}
          <div class="section-label">原因 → 更正 → 结果 追溯</div>
          ${diffBlock(item.raw_snapshot, item.corrected_snapshot)}
          ${
            (item.failure_reason?.warnings || []).length
              ? `<ul class="reason-list">${item.failure_reason.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>`
              : ''
          }
          ${
            item.status !== 'resolved'
              ? `<div class="section-label">按正确条款更正</div>${canCorrect ? correctForm(item) : '<p class="muted">只有定价主管 / 价格运营可以更正隔离快照。</p>'}`
              : '<p class="muted">该隔离单已闭环：更正后的快照通过算法 A，缺失建议已在重试运行补齐。</p>'
          }`,
        footer: canCorrect
          ? '<span class="mono muted">更正后请在「抓价快照」页重跑当天测算，系统只补缺的建议，已出建议不重算。</span>'
          : '',
      });
      root.querySelector('#correct-form')?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.target;
        const errorNode = root.querySelector('#correct-error');
        let payload;
        try {
          payload = {
            page_price: Number(form.page_price.value),
            member_discount: Number(form.member_discount.value),
            shop_coupon: parseJsonField(form, 'shop_coupon'),
            platform_coupon: parseJsonField(form, 'platform_coupon'),
            full_reduction: JSON.parse(form.full_reduction.value || '[]'),
            note: form.note.value.trim() || undefined,
          };
        } catch (err) {
          errorNode.textContent = '券配置 JSON 格式不正确，请检查括号与引号';
          return;
        }
        try {
          const updated = await api.patch(`/api/quarantines/${item.id}/correct`, payload);
          toast(`已更正，算法 A 通过：1 件券后 ${money(updated.current_snapshot?.unit_price_1)}，请重跑当天测算`);
          close();
          quarantinePage.render(view, ctx);
        } catch (err) {
          errorNode.textContent = errorMessage(err);
        }
      });
    })
    .catch((err) => toast(errorMessage(err), 'err'));
}

export const quarantinePage = {
  async render(view, ctx) {
    let data;
    let days = [];
    try {
      const [list, snapshotData] = await Promise.all([
        api.get('/api/quarantines', {
          status: state.status,
          outcome: state.outcome,
          q: state.q,
          captured_on: state.capturedOn,
          limit: 50,
        }),
        api.get('/api/snapshots', { limit: 1 }).catch(() => ({ capture_days: [] })),
      ]);
      data = list;
      days = snapshotData.capture_days || [];
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => quarantinePage.render(view, ctx));
      return;
    }
    if (!state.capturedOn && days.length) state.capturedOn = days[0];
    const stats = data.stats || {};
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>坏快照隔离台</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 条隔离单 · 未闭环 ${stats.open_total ?? 0} 条</span>
            ${can(ctx.user.role, 'run:create') ? '<button class="btn sm accent" id="retry-run">重跑当天测算补建议</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${statStrip(stats)}
          ${filterBar('quarantine-filters', [
            field({ label: '快照日期', name: 'captured_on', value: state.capturedOn, options: days.map((day) => ({ value: day, label: day })) }),
            field({ label: '隔离状态', name: 'status', value: state.status, options: optionList(ENUMS.quarantineStatus) }),
            field({ label: '闭环结果', name: 'outcome', value: state.outcome, options: optionList(ENUMS.quarantineOutcome) }),
            field({ label: '搜索 SKU', name: 'q', value: state.q, placeholder: '名称 / 编码' }),
          ])}
        </div>
        ${table({
          rows: data.items,
          empty: { title: '没有隔离记录', hint: '当前筛选下所有快照都通过了算法 A。' },
          columns: [
            {
              key: 'sku_name',
              label: 'SKU / 竞品',
              render: (row) =>
                `<b>${esc(row.sku_name)}</b><div class="mono muted">${esc(row.sku_code)} · ${esc(row.competitor_name || '—')}</div>`,
            },
            { key: 'captured_on', label: '快照日期' },
            {
              key: 'failure_message',
              label: '隔离原因',
              render: (row) => `<span class="mono muted ellipsis">${esc(row.failure_message || '券条款非法')}</span>`,
            },
            {
              key: 'status',
              label: '隔离状态',
              render: (row) => `<span class="badge ${quarantineStatusTone(row.status)}">${esc(row.status_label)}</span>`,
            },
            {
              key: 'outcome',
              label: '更正结果',
              render: (row) => `<span class="badge ${quarantineOutcomeTone(row.outcome)}">${esc(row.outcome_label)}</span>`,
            },
            { key: 'retry_count', label: '重试', className: 'num' },
            {
              key: 'advice_id',
              label: '补出建议',
              render: (row) => (row.advice_id ? `#${esc(row.advice_id)}` : '<span class="muted">—</span>'),
            },
            { key: 'corrected_by', label: '更正人', render: (row) => esc(row.corrected_by || '—') },
          ],
        })}
      </section>`;

    const filters = view.querySelector('#quarantine-filters');
    filters?.addEventListener('change', () => {
      state.capturedOn = filters.querySelector('[name=captured_on]').value;
      state.status = filters.querySelector('[name=status]').value;
      state.outcome = filters.querySelector('[name=outcome]').value;
      state.q = filters.querySelector('[name=q]').value.trim();
      quarantinePage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        quarantinePage.render(view, ctx);
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => openDetail(view, ctx, row.dataset.id));
    });
    view.querySelector('#retry-run')?.addEventListener('click', async (event) => {
      const button = event.target;
      button.disabled = true;
      button.textContent = '测算中…';
      try {
        const run = await api.post('/api/runs', { captured_on: state.capturedOn || undefined, trigger: 'manual' });
        const m = run.metrics || {};
        toast(
          `重跑 ${run.run_no}：新补建议 ${m.new_advices ?? 0} 条、沿用既有 ${m.skipped_existing_advices ?? 0} 条、` +
            `隔离闭环 ${m.quarantine_resolved ?? 0} 条、仍缺建议 ${m.missing_advice_skus ?? 0} 个`,
        );
        await quarantinePage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
        button.textContent = '重跑当天测算补建议';
      }
    });
  },
};

export default quarantinePage;
