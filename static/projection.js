/* Próximos 3 meses — pure 3-month obligations projection (no DOM, no fetch).
   Port of the APK's domain/ObligationsAnalytics (projectSeparated,
   projectionMetadata, reconcileInstallments, recurringProjectionComponents).
   Needs spend.js (loaded via loadSpend) in the same global scope. All
   amounts are USD.

   Months: like the APK, the projection covers the 3 calendar months after
   today's. Card cuotas are placed by statement CORTE month (a plan last seen
   at n/total in corte M bills cuota n+d in corte M+d); the recurring lines
   are flat monthly estimates.

   Deviations from the APK (the web's own rules win where both exist):
   - Committed subscriptions/utilities use the web's active-subscription rule
     (computeActiveSubscriptions: ≥2 months of the window and charged in its
     latest month, average per active month), over the APK's committed
     category set. The APK uses latest-month presence + median of the last
     3 charges.
   - Variable merchants use the web's merchant grouping (computeMerchants:
     category of the first cargo, refunds netted). The APK takes the latest
     charge's category and drops credits.
   - A manual installment with the same term and amount (±1¢) as a statement
     plan but different progress is AMBIGUOUS (still counted, flagged). The
     APK calls it manual-only/confirmed and only warns when the names agree,
     which never happens with the bank's generic "COMPRAS A PLAZO" descriptions. */
'use strict';

const PROJECTION_MONTH_COUNT = 3;
const PROJECTION_WINDOW_MONTHS = 6;
// The APK's committed category set; ids this app doesn't have are inert.
const PROJECTION_COMMITTED_CATEGORY_IDS = new Set([
  'services', 'streaming', 'utilities', 'insurance', 'fees', 'subscriptions',
  'agua', 'electricidad', 'internet_telefono', 'seguros',
]);

const toCents = usd => Math.round(usd * 100);
const sumUsd = items => items.reduce((s, c) => s + c.usd, 0);

function isIsoDate(s) {
  return /^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])/.test(s || '');
}

function monthDiff(from, to) {
  const [ay, am] = from.split('-').map(Number), [by, bm] = to.split('-').map(Number);
  return (by - ay) * 12 + (bm - am);
}

// `today`: 'YYYY-MM-DD' or a Date (local calendar), default now.
function projectionMonthList(today = new Date(), count = PROJECTION_MONTH_COUNT) {
  const current = typeof today === 'string'
    ? today.slice(0, 7)
    : `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}`;
  return Array.from({ length: count }, (_, k) => shiftMonth(current, k + 1));
}

function medianUsd(values) {
  if (!values.length) return 0;
  const s = values.slice().sort((a, b) => a - b), mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function normalizedName(v) {
  return (v || '').normalize('NFD').replace(/\p{M}+/gu, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ').trim();
}

function namesAgree(manual, statement) {
  const a = normalizedName(manual), b = normalizedName(statement);
  if (!a || !b) return false;
  if (a === b || b.includes(a) || a.includes(b)) return true;
  const bTokens = new Set(b.split(' ').filter(x => x.length >= 3));
  return a.split(' ').some(x => x.length >= 3 && bTokens.has(x));
}

/* Manual (dashboard) installments ↔ statement plans. A candidate agrees on
   term, progress and amount within 1¢; a unique name match may break a tie.
   Outcomes: matched / manual_only / ambiguous / statement_only. Nothing
   ambiguous is merged — the caller still counts it, flagged. */
function reconcileInstallments(manualRows, plans) {
  const sameTermAmount = (m, p) => m.months_total === p.total &&
    Math.abs(toCents(m.monthly) - toCents(p.monthlyUsd)) <= 1;
  const raw = manualRows.map(m => {
    let idx = plans.map((_, i) => i).filter(i => sameTermAmount(m, plans[i]) && m.months_paid === plans[i].n);
    if (idx.length > 1) {
      const byName = idx.filter(i => namesAgree(m.name, plans[i].descripcion));
      if (byName.length === 1) idx = byName;
    }
    return idx;
  });
  const claims = new Map();
  raw.flat().forEach(i => claims.set(i, (claims.get(i) || 0) + 1));
  const matched = new Set();
  const results = manualRows.map((manual, mi) => {
    const idx = raw[mi];
    if (!idx.length) {
      const conflicts = plans.filter(p => sameTermAmount(manual, p) && p.n !== manual.months_paid);
      return conflicts.length
        ? { outcome: 'ambiguous', reason: 'progress', manual, statement: null, candidates: conflicts }
        : { outcome: 'manual_only', reason: null, manual, statement: null, candidates: [] };
    }
    if (idx.length === 1 && claims.get(idx[0]) === 1) {
      matched.add(idx[0]);
      return { outcome: 'matched', reason: null, manual, statement: plans[idx[0]], candidates: [plans[idx[0]]] };
    }
    return { outcome: 'ambiguous', reason: idx.length > 1 ? 'several_plans' : 'shared_plan',
             manual, statement: null, candidates: idx.map(i => plans[i]) };
  });
  plans.forEach((p, i) => {
    if (!matched.has(i)) results.push({ outcome: 'statement_only', reason: null, manual: null, statement: p, candidates: [] });
  });
  return results;
}

/* Recurring card spend over the last `PROJECTION_WINDOW_MONTHS` corte months
   of the data. Committed = active subscriptions/utilities/insurance (web
   rule). Variable = other merchants active in ≥3 window months, including
   the latest or previous one, at the median of their monthly totals.
   Cuota rows are excluded (plans are projected with decay), and so are
   refund and installment merchants. A committed-category merchant that
   misses the subscription rule (e.g. a utility bill absent from the latest
   corte) falls through to variable instead of vanishing — the APK drops it. */
function recurringProjection(windowMonths) {
  if (!windowMonths.length) return { committed: [], variable: [] };
  const windowSet = new Set(windowMonths);
  const committed = computeActiveSubscriptions(windowSet, PROJECTION_COMMITTED_CATEGORY_IDS)
    .filter(s => toCents(s.avgUsd) > 0)
    .map(s => ({ id: `subscription:${s.name}`, label: s.name, source: 'subscription', cid: s.cid,
                 usd: s.avgUsd, confidence: 'confirmed',
                 detail: { monthsCount: s.monthsCount, windowMonths: windowMonths.length, lastDate: s.lastDate } }));
  const committedNames = new Set(committed.map(c => c.label));
  const latest = windowMonths[windowMonths.length - 1], previous = windowMonths[windowMonths.length - 2];
  const notCuota = t => t.tipo !== 'cuota';
  const merchants = computeMerchants(windowSet, notCuota);
  const perMonth = Object.fromEntries(windowMonths.map(m => [m, computeMerchants(new Set([m]), notCuota)]));
  const variable = [];
  Object.entries(merchants).forEach(([name, r]) => {
    if (committedNames.has(name) || r.cid === 'refund' || r.cid === 'installments') return;
    if (r.months.size < 3 || !(r.months.has(latest) || r.months.has(previous))) return;
    const totals = windowMonths.filter(m => r.months.has(m)).map(m => perMonth[m][name].usd);
    const usd = medianUsd(totals);
    if (toCents(usd) <= 0) return;
    variable.push({ id: `recurring:${name}`, label: name, source: 'recurring', cid: r.cid, usd,
                    confidence: 'inferred',
                    detail: { monthsCount: r.months.size, windowMonths: windowMonths.length, lastDate: r.lastDate } });
  });
  const byUsd = (a, b) => b.usd - a.usd || (a.label < b.label ? -1 : 1);
  return { committed: committed.sort(byUsd), variable: variable.sort(byUsd) };
}

/* Source freshness. A card is stale when its latest corte month is behind
   the newest corte month of any card (APK rule: compared by month, since
   cards close on different days). A card is unvalidated when its latest
   statement's stored `validation` is missing, null (no printed total to
   check) or not ok. */
function projectionMetadata({ debitTxs, months }) {
  const cards = Object.entries(S).map(([cardKey, card]) => {
    const sts = (card.statements || []).filter(st => isIsoDate(st.fecha_corte));
    if (!sts.length) return null;
    const latest = sts.reduce((a, b) => (b.fecha_corte > a.fecha_corte ? b : a));
    const v = latest.validation;
    return { cardKey, cardName: card.card_name || cardKey, latestCutoff: latest.fecha_corte.slice(0, 10),
             validation: v === undefined ? 'missing' : v === null ? 'unchecked' : v.ok === true ? 'ok' : 'mismatch' };
  }).filter(Boolean).sort((a, b) => (a.cardName < b.cardName ? -1 : a.cardName > b.cardName ? 1 : 0));
  const newest = cards.reduce((m, c) => (c.latestCutoff.slice(0, 7) > m ? c.latestCutoff.slice(0, 7) : m), '');
  cards.forEach(c => {
    c.stale = c.latestCutoff.slice(0, 7) < newest;
    c.validated = c.validation === 'ok';
  });

  const start = months[0];
  const latestOf = dates => dates.filter(isIsoDate).sort().pop() || null;
  const latestDebitDate = latestOf(debitTxs.map(t => t.fecha));
  return {
    cards,
    latestDebitDate,
    // Flag the debit ledger once it is 2+ calendar months behind today's.
    debitStale: !!latestDebitDate && monthDiff(latestDebitDate.slice(0, 7), shiftMonth(start, -1)) >= 2,
  };
}

/* Entry point. `budget` = /api/data, `debitTxs` = /api/debit-account
   transactions; statements/categories come from loadSpend(). Returns plain
   data: { months: [{ month, committed, variable, committedTotal,
   variableTotal, total, ambiguousTotal }], metadata, warnings,
   reconciliation, windowMonths }. */
function buildProjection({ budget, debitTxs = [], today } = {}) {
  const months = projectionMonthList(today);
  const plans = collectInstallmentPlans();
  const manual = Array.isArray(budget.installments) ? budget.installments : [];

  const reconciliation = reconcileInstallments(manual, plans);
  const outcomeOf = new Map(reconciliation.filter(r => r.manual).map(r => [r.manual, r]));

  const windowMonths = allMonths().slice(-PROJECTION_WINDOW_MONTHS);
  const recurring = recurringProjection(windowMonths);
  const metadata = projectionMetadata({ debitTxs, months });

  const warnings = [];
  const stale = metadata.cards.filter(c => c.stale);
  if (stale.length) warnings.push({ code: 'stale_card', cards: stale });
  const unvalidated = metadata.cards.filter(c => !c.validated);
  if (unvalidated.length) warnings.push({ code: 'unvalidated_statement', cards: unvalidated });
  const progress = reconciliation.filter(r => r.reason === 'progress');
  if (progress.length) warnings.push({ code: 'installment_progress_conflict', items: progress });
  const ambiguous = reconciliation.filter(r => r.outcome === 'ambiguous' && r.reason !== 'progress');
  if (ambiguous.length) warnings.push({ code: 'ambiguous_installment', items: ambiguous });
  // An unfinished plan missing from its card's later statements (paid off early,
  // cancelled): still projected, per "never drop silently", but as ambiguous.
  const latestCorte = new Map(metadata.cards.map(c => [c.cardKey, c.latestCutoff.slice(0, 7)]));
  const unseen = new Set(plans.filter(p => p.n < p.total && (latestCorte.get(p.cardKey) || '') > p.month));
  if (unseen.size) warnings.push({ code: 'plan_not_seen', plans: [...unseen] });

  const out = months.map((month, k) => {
    const committed = [];

    plans.forEach(p => {
      const remaining = p.total - p.n, d = monthDiff(p.month, month);
      if (remaining > 0 && d >= 1 && d <= remaining) {
        committed.push({ id: `card:${p.cardKey}:${p.fecha}:${p.n}/${p.total}:${toCents(p.monthlyUsd)}`,
                         label: p.cardName, source: 'card_installment', cid: p.cid, usd: p.monthlyUsd,
                         confidence: unseen.has(p) ? 'ambiguous' : 'confirmed',
                         detail: { cardName: p.cardName, cuota: p.n + d, total: p.total, lastCorte: p.month, ending: d === remaining } });
      }
    });

    manual.forEach(m => {
      const rec = outcomeOf.get(m);
      const outcome = rec ? rec.outcome : 'manual_only';
      const remaining = Math.max(0, m.months_total - m.months_paid) || 0;  // NaN (malformed row) -> 0
      if (outcome === 'matched' || !(m.monthly > 0) || k >= remaining) return;
      committed.push({ id: `manual:${m.id}`, label: m.name, source: 'manual_installment', cid: 'installments',
                       usd: m.monthly, confidence: outcome === 'ambiguous' ? 'ambiguous' : 'confirmed',
                       detail: { outcome, reason: rec ? rec.reason : null, cuota: m.months_paid + k + 1, total: m.months_total,
                                 candidates: rec ? rec.candidates : [] } });
    });

    committed.push(...recurring.committed);
    const variable = recurring.variable.slice();
    const committedTotal = sumUsd(committed), variableTotal = sumUsd(variable);
    return {
      month, committed, variable, committedTotal, variableTotal,
      total: committedTotal + variableTotal,
      ambiguousTotal: sumUsd(committed.filter(c => c.confidence === 'ambiguous')),
    };
  });

  return { months: out, metadata, warnings, reconciliation, windowMonths };
}
