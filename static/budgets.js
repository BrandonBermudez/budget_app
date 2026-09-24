/* Presupuesto por categoría — pure budget-vs-actual logic (no DOM, no fetch).
   Port of the APK's CategoryBudgetAnalytics.audited(). Actuals come from
   spend.js spendByMonthCategory(), so corte-month bucketing, superseded
   cargos and refund netting are inherited, never re-derived here.
   Requires spend.js loaded (and loadSpend() called) first. */
'use strict';

const BUDGET_UNCAT = '_uncat';
const BUDGET_MAX_CENTS = 100000000;   // $1,000,000 — same cap as the server

// Corte months with statements, newest first.
function budgetMonths() {
  return allMonths().reverse();
}

// Targets whose category still exists. Deleted categories' targets stay
// stored (an import can bring the category back); they are only hidden.
function visibleTargets(targetsCents, categories) {
  const out = {};
  Object.entries(targetsCents || {}).forEach(([cid, cents]) => {
    if (cid !== BUDGET_UNCAT && Object.prototype.hasOwnProperty.call(categories, cid)
        && Number.isInteger(cents) && cents > 0) out[cid] = cents;
  });
  return out;
}

/* One month's budget. Rows = every category with a visible target OR a
   nonzero actual that month (the APK's choice: untargeted spend is listed
   under its own section, so the totals reconcile with total spend).
   Actual keeps its sign — a net-refund month can be negative.
   Row: { categoryId, targetCents|null, actualUsd, varianceUsd|null, ratio|null }
   varianceUsd = target − actual (negative = over). */
function monthBudget(targetsCents, categories, month) {
  const targets = visibleTargets(targetsCents, categories);
  const actual = spendByMonthCategory(new Set([month]))[month] || {};
  const ids = new Set(Object.keys(targets));
  Object.entries(actual).forEach(([cid, usd]) => {
    if (Math.round(usd * 100) !== 0) ids.add(cid);   // float noise from a fully-netted refund is zero
  });
  const rows = [...ids].map(cid => {
    const targetCents = targets[cid] ?? null;
    const actualUsd = actual[cid] || 0;
    return {
      categoryId: cid,
      targetCents,
      actualUsd,
      varianceUsd: targetCents === null ? null : targetCents / 100 - actualUsd,
      ratio: targetCents === null ? null : actualUsd / (targetCents / 100),
    };
  });
  rows.sort((a, b) =>
    ((b.targetCents !== null) - (a.targetCents !== null)) ||
    ((b.ratio ?? -Infinity) - (a.ratio ?? -Infinity)) ||
    (b.actualUsd - a.actualUsd));
  const withTarget = rows.filter(r => r.targetCents !== null);
  return {
    month,
    rows,
    totalTargetCents: withTarget.reduce((s, r) => s + r.targetCents, 0),
    totalActualWithTargetUsd: withTarget.reduce((s, r) => s + r.actualUsd, 0),
    totalActualUsd: rows.reduce((s, r) => s + r.actualUsd, 0),
  };
}

/* User-typed dollars → integer cents, parsed as a string (no float math, so
   "0.29" is exactly 29). Accepts "250", "250.5", "$1,250.00", ".29".
   A lone comma is rejected, not guessed: "12,50" could be either locale.
   Returns { cents } or { error: 'empty' | 'invalid' | 'zero' | 'too_large' }. */
function parseDollarsToCents(input) {
  let s = String(input ?? '').trim().replace(/^\$\s*/, '');
  if (!s) return { error: 'empty' };
  if (s.includes(',')) {
    if (!/^\d{1,3}(,\d{3})+(\.\d*)?$/.test(s)) return { error: 'invalid' };
    s = s.replace(/,/g, '');
  }
  const m = s.match(/^(\d*)(?:\.(\d{0,2}))?$/);
  if (!m || (m[1] === '' && !m[2])) return { error: 'invalid' };
  const whole = m[1].replace(/^0+(?=\d)/, '');
  if (whole.length > 7) return { error: 'too_large' };
  const cents = Number(whole || '0') * 100 + Number((m[2] || '').padEnd(2, '0'));
  if (cents === 0) return { error: 'zero' };
  if (cents > BUDGET_MAX_CENTS) return { error: 'too_large' };
  return { cents };
}

// Integer cents → "1250.00" for an edit field.
function centsToEditString(cents) {
  return `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`;
}
