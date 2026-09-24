/* Pagos: fixed monthly bills. Pure data (no DOM, no fetch), a port of the
   APK's domain/BillsAnalytics.kt. Needs spend.js (loadSpend already run)
   loaded first.

   Source: utility providers with a bill_category (data/utilities.json):
   card-statement spend rows (isSpend via eachSpendTx) in that category.
   Cuota rows are skipped, as in the APK (it matches the merchant name in
   the description, which a "COMPRAS A PLAZO" cuota never carries).
   Providers without one aren't bills, so they have no entry and their
   keywords are excluded by NON_BILL_UTILITY_KEYS.

   Month = the calendar month of the row's own `fecha` (not the corte month),
   as in the APK. A statement row with a blank fecha falls back to its corte
   month's first day. */
'use strict';

// `key` is the APK's category_key, stored in _local.bill_payments; the same
// set is BILL_KEYS in app.py. UTILITY_PROVIDERS is injected by the page.
const BILL_CATEGORIES = [
  ...UTILITY_PROVIDERS.filter(p => p.bill_category)
    .map(p => ({ key: p.key, label: p.label, color: p.color, categoryId: p.bill_category })),
];

// APK NON_BILL_UTILITY_KEYS (BillsAnalytics.kt:53): excluded by description
// even when categorize() says utility, so non-bill providers' charges never
// blend into a bill line if their keywords move into that category.
const NON_BILL_UTILITY_KEYS = UTILITY_PROVIDERS.filter(p => !p.bill_category).map(p => p.keyword);

// Every charge (positive or credit) of one bill, over the full history.
function _billCharges(cat) {
  const out = [];
  eachSpendTx((t, month) => {
    if (t.tipo === 'cuota' || categorize(t) !== cat.categoryId) return;
    const desc = (t.descripcion || '').toUpperCase();
    if (NON_BILL_UTILITY_KEYS.some(k => desc.includes(k))) return;
    out.push({ fecha: t.fecha || `${month}-01`, usd: txUsd(t) });
  });
  return out;
}

/* monthsFilter: optional Set of 'YYYY-MM'; scopes byMonth / averages only.
   lastCharge* and lastDate always come from the full history.
   Returns { months, lines, totalAvgUsd }; lines in BILL_CATEGORIES order,
   only bills with activity in the window. */
function computeBills({ monthsFilter } = {}) {
  const lines = [];
  BILL_CATEGORIES.forEach(cat => {
    const byMonth = {};
    let lastDate = '', last = null;
    _billCharges(cat).forEach(c => {
      if (c.fecha > lastDate) lastDate = c.fecha;
      // The single most recent positive charge (date tie: the larger one), never
      // a month sum: two bills can post in one calendar month.
      if (c.usd > 0 && (!last || c.fecha > last.fecha || (c.fecha === last.fecha && c.usd > last.usd))) last = c;
      const m = c.fecha.slice(0, 7);
      if (monthsFilter && !monthsFilter.has(m)) return;
      byMonth[m] = (byMonth[m] || 0) + c.usd;
    });
    const months = Object.keys(byMonth).sort();
    if (!months.length) return;
    const lastMonth = months[months.length - 1];
    const sum = o => Object.values(o).reduce((s, v) => s + v, 0);
    lines.push({
      key: cat.key, label: cat.label, color: cat.color,
      months, byMonth,
      avgUsd: sum(byMonth) / months.length,
      // No positive charge at all (a credit-only line): the latest month's net.
      lastChargeUsd: last ? last.usd : byMonth[lastMonth],
      lastDate: last ? last.fecha : lastDate,
    });
  });
  const months = [...new Set(lines.flatMap(l => l.months))].sort();
  return { months, lines, totalAvgUsd: lines.reduce((s, l) => s + l.avgUsd, 0) };
}

// category_keys ticked as paid for `month`. A stored row means paid (untick
// deletes it, as in the APK); paid:false rows from an import are ignored.
function paidKeysFor(billPayments, month) {
  return new Set((billPayments || []).filter(b => b && b.month === month && b.paid !== false).map(b => b.category_key));
}
