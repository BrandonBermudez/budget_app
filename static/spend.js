/* ── Shared spend engine ──
   The pure data half of the Transacciones page's analytics (no DOM), shared
   so every page that shows spend produces the same numbers.

   The state below is script-global: a page may assign it directly (as
   transactions.html does after an upload) or load it all via loadSpend().

   Entry point for a new page:
     loadSpend({ statements, categories });   // /api/statements, /api/categories
     eachSpendTx((t, month, card, st) => ... categorize(t) ... txUsd(t) ...);
   Re-run identifySupersededCargos() whenever S is replaced or categories change. */
'use strict';

let S = {};         // statements data: { card_key: { card_name, statements: [...] } }
let CATEGORIES = {};        // { category_id: { label, color, keywords: [...] } }
let CATEGORY_LOOKUP = [];   // flat [keyword, category_id], sorted longest-keyword-first

function loadSpend({ statements, categories }) {
  setCategories(categories);
  S = statements;
  identifySupersededCargos();
}

/* ── Shared analytics helpers ──
   All spend figures are in USD;
   "spend" = cargo + cuota, matching the trend and category charts. */
function txUsd(t) { return t.monto_usd; }

// When a purchase is converted to a "Compras a Plazo" installment plan, the
// bank prints it TWICE: once as an ordinary cargo line (the full original price)
// in the same statement as the plan's first installment, and again as the
// monthly cuota lines going forward. The cargo line is informational, not a
// second charge: the cargo amount
// matches (monthly × total cuotas) within a couple cents of rounding drift,
// and always lands in the same corte as CUOTA:01/0N. Counting both would
// double every deferred purchase's cost across every total in the app.
// This identifies those cargo lines once per data load; isSpend() then
// excludes them everywhere spend is summed.
//
// A cuota's own description never names the merchant (the bank always prints
// it as "COMPRAS A PLAZO · CUOTA:0N/0M") — the matched origin cargo is the only
// place that information exists. So the same match is also used to look up
// the origin cargo's real category (Compras, Salud, whatever it is) and
// propagate it to every installment in that plan — every cuota row, not
// just the matched 01/0N one — so a deferred Amazon purchase keeps counting
// as "Compras" instead of collapsing into the flat "Cuotas a Plazo" bucket.
let SUPERSEDED_CARGOS = new WeakSet();
let PLAN_CATEGORY = new WeakMap();   // cuota tx -> category id inherited from its origin cargo

const UNRESOLVED_MATCH = '__unresolved__';   // sentinel: matched a cargo, but that cargo's own description hit no keyword

function identifySupersededCargos() {
  SUPERSEDED_CARGOS = new WeakSet();
  PLAN_CATEGORY = new WeakMap();

  // Pass 1: per statement, find each CUOTA:01/0N's matching origin cargo.
  // While matching, record which category each plan-signature resolved to —
  // pushing UNRESOLVED_MATCH (not skipping) when a match was found but its
  // cargo's description didn't hit any category keyword. Without that
  // sentinel, an unresolved match is indistinguishable from "no match at
  // all", and a *different*, unrelated plan that happens to share this
  // plan's signature (same card + total cuotas + rounded monthly amount)
  // could wrongly inherit whatever single category the *other* plan
  // resolved to — a wrong category, not just a missing one.
  const categoryBySig = {};  // sig.base -> [{ cents, cid }] (cid may be UNRESOLVED_MATCH)

  Object.entries(S).forEach(([cardKey, card]) => {
    card.statements.forEach(st => {
      const firstCuotas = st.transactions.filter(t => {
        if (t.tipo !== 'cuota') return false;
        const m = t.descripcion.match(/CUOTA:(\d+)\/(\d+)/);
        return m && parseInt(m[1], 10) === 1;
      });
      if (!firstCuotas.length) return;

      // Candidate cuotas (with their expected total + plan key) and cargos,
      // as pools to draw from — not claimed yet.
      const cuotaPool = firstCuotas.map(cuota => {
        const m = cuota.descripcion.match(/CUOTA:(\d+)\/(\d+)/);
        const total = parseInt(m[2], 10);
        return { cuota, expected: txUsd(cuota) * total, sig: planSignature(cardKey, st, cuota) };
      });
      const cargoPool = new Set(st.transactions.filter(t => t.tipo === 'cargo'));

      // Repeatedly extract the single globally-closest (cuota, cargo) pair
      // remaining, not "each cuota's independently-nearest cargo" — the
      // latter lets two plans that are equidistant from the same cargo both
      // claim it as their own "best" before either is actually assigned,
      // silently leaving one of two genuine duplicates unmatched (e.g. two
      // simultaneous $12.34×3 plans in one statement, both nearest to the
      // same $37.02 cargo).
      let remainingCuotas = cuotaPool.slice();
      while (remainingCuotas.length && cargoPool.size) {
        let bestCuotaIdx = -1, bestCargo = null, bestDiff = 0.10;
        remainingCuotas.forEach((cu, i) => {
          cargoPool.forEach(c => {
            const diff = Math.abs(txUsd(c) - cu.expected);
            if (diff < bestDiff) { bestDiff = diff; bestCuotaIdx = i; bestCargo = c; }
          });
        });
        if (bestCargo === null) break;  // nothing left within tolerance

        SUPERSEDED_CARGOS.add(bestCargo);
        cargoPool.delete(bestCargo);
        const cid = categorizeByKeyword(bestCargo.descripcion);
        const sig = remainingCuotas[bestCuotaIdx].sig;
        (categoryBySig[sig.base] ??= []).push({ cents: sig.cents, cid: cid || UNRESOLVED_MATCH });
        remainingCuotas.splice(bestCuotaIdx, 1);
      }
    });
  });

  // Pass 2: propagate to every installment (any CUOTA:0N/0M, not just 01) —
  // but only when its plan-signature resolved to exactly one category AND
  // nothing else sharing that signature was left unresolved. Two
  // coincidentally-same-size plans resolving to genuinely different
  // categories, or one resolved + one unresolved, are both treated as
  // ambiguous — skip propagation rather than guess, and let it fall back to
  // the flat "Cuotas a Plazo" bucket.
  Object.entries(S).forEach(([cardKey, card]) => {
    card.statements.forEach(st => {
      st.transactions.forEach(t => {
        if (t.tipo !== 'cuota') return;
        const sig = planSignature(cardKey, st, t);
        if (!sig) return;
        const known = categoryBySig[sig.base] || [];
        let hits = known.filter(e => e.cents === sig.cents);
        // The final cuota carries the bank's division remainder (≤ total−1 ¢).
        if (!hits.length && sig.n === sig.total) {
          hits = known.filter(e => Math.abs(e.cents - sig.cents) <= sig.total - 1);
        }
        const cids = new Set(hits.map(e => e.cid));
        if (cids.size === 1 && !cids.has(UNRESOLVED_MATCH)) {
          PLAN_CATEGORY.set(t, [...cids][0]);
        }
      });
    });
  });
}

// Installment-plan identity. The bank gives plans no stable id (each month's cuota
// line gets a fresh ref), so a plan is identified by card + term +
// description + sub-card + inferred first-cuota month (corte − (n−1)), with
// the monthly amount in exact cents kept separate so the final cuota can
// differ by its rounding remainder. Port of the APK's planCategoryKey /
// InstallmentLineage.
function installmentDescription(desc) {
  return (desc || '').toUpperCase()
    .replace(/[^A-Z0-9]*CUOTA\s*:?\s*\d+\s*\/\s*\d+\s*$/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function shiftMonth(month, delta) {
  const [y, m] = month.split('-').map(Number);
  const abs = y * 12 + (m - 1) + delta;
  return `${Math.floor(abs / 12)}-${String(abs % 12 + 1).padStart(2, '0')}`;
}

function planSignature(cardKey, st, t) {
  const m = t.descripcion.match(/CUOTA:(\d+)\/(\d+)/);
  const corte = (st.fecha_corte || '').slice(0, 7);
  if (!m || !corte) return null;
  const n = parseInt(m[1], 10), total = parseInt(m[2], 10);
  if (!(total > 0)) return null;
  return {
    n, total,
    cents: Math.round(txUsd(t) * 100),
    base: [cardKey, total, installmentDescription(t.descripcion),
           (t.subtarjeta || '').trim().toUpperCase(),
           shiftMonth(corte, -(Math.max(n, 1) - 1))].join('|'),
  };
}

// One entry per reconstructed plan (latest occurrence for progress).
// Parallel identical plans stay in separate lanes: an occurrence joins a lane
// only if that lane hasn't reached this cuota number yet, in an earlier corte.
function collectInstallmentPlans(monthsFilter, onlyCardKey) {
  const occurrences = [];
  Object.entries(S).forEach(([cardKey, card]) => {
    if (onlyCardKey && cardKey !== onlyCardKey) return;
    card.statements.forEach(st => {
      const month = (st.fecha_corte || '').slice(0, 7);
      if (!month || (monthsFilter && !monthsFilter.has(month))) return;
      st.transactions.forEach(t => {
        if (t.tipo !== 'cuota') return;
        const sig = planSignature(cardKey, st, t);
        if (sig) occurrences.push({ cardKey, cardName: card.card_name, month, t, sig });
      });
    });
  });
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  occurrences.sort((a, b) => cmp(a.month, b.month) || cmp(a.t.fecha, b.t.fecha) || cmp(a.t.ref, b.t.ref));

  const lanes = [];
  occurrences.forEach(({ cardKey, cardName, month, t, sig }) => {
    const { n, total, cents, base } = sig;
    const existing = n <= 1 ? null : lanes
      .filter(l => l.base === base && l.lastN < n && l.lastMonth < month &&
        (l.cents === cents || (n === total && Math.abs(l.cents - cents) <= total - 1)))
      .sort((a, b) => Math.abs(a.cents - cents) - Math.abs(b.cents - cents) || cmp(a.sourceRef, b.sourceRef))[0];
    const plan = {
      cardKey, cardName, fecha: t.fecha, month, n, total, descripcion: t.descripcion,
      monthlyUsd: (existing ? existing.cents : cents) / 100,
      cid: categorize(t) || 'installments',
    };
    if (!existing) {
      lanes.push({ base, cents, sourceRef: t.ref, lastN: n, lastMonth: month, plan });
    } else {
      existing.lastN = n;
      existing.lastMonth = month;
      if (t.fecha > existing.plan.fecha) existing.plan = plan;
      else if (month > existing.plan.month) existing.plan.month = month;
    }
  });
  return lanes.map(l => l.plan);
}

function isSpend(t) {
  if (t.tipo === 'cuota') return true;
  if (t.tipo === 'cargo') return !SUPERSEDED_CARGOS.has(t);
  // A purchase-linked refund/rebate/discount ("Reembolso Rebate Local")
  // reduces what was actually spent and nets against cargos here (the
  // bank prints its amount negative already). Two
  // kinds are excluded instead: a balance payment ("SU PAGO RECIBIDO
  // GRACIAS") isn't a purchase reversal, and "CR COMPRA PLZ ..." is the
  // bank's own reversal notation for a purchase converting to a Compras a Plazo
  // plan — the same conversion SUPERSEDED_CARGOS already excludes by
  // dropping the origin cargo, so counting this credit too would subtract
  // that purchase a second time.
  if (t.tipo === 'credito') return !/PAGO RECIBIDO|^CR COMPRA PLZ/i.test(t.descripcion);
  return false;
}

// `monthsFilter`, when given, is a Set of 'YYYY-MM' strings — statements
// outside it are skipped entirely. Optional so every existing all-time
// caller is unaffected.
function eachSpendTx(fn, monthsFilter) {
  Object.values(S).forEach(card => card.statements.forEach(st => {
    const month = (st.fecha_corte || '').slice(0, 7);
    if (!month) return;
    if (monthsFilter && !monthsFilter.has(month)) return;
    st.transactions.forEach(t => { if (isSpend(t)) fn(t, month, card, st); });
  }));
}

function allMonths() {
  const set = new Set();
  Object.values(S).forEach(card => card.statements.forEach(st => {
    const m = (st.fecha_corte || '').slice(0, 7);
    if (m) set.add(m);
  }));
  return [...set].sort();
}

// Normalize a raw statement description to a stable merchant name for grouping:
// drop the "*orderref" tail, trailing location/backslash junk, and long digit
// runs, then upper-case. "AMAZON.COM*GN9F" and "Amazon.com*ZI3F" → "AMAZON.COM".
function normalizeMerchant(desc) {
  const upper = (desc || '').toUpperCase();
  return upper
    .split('*')[0]
    .split('\\')[0]
    .replace(/\s{2,}.*$/, '')
    .replace(/\s+\d{4,}.*$/, '')
    .replace(/[.\s]+$/, '')
    .trim() || '(desconocido)';
}

/* ── Categorization ── */
function setCategories(data) {
  CATEGORIES = data;
  rebuildCategoryLookup();
}

function rebuildCategoryLookup() {
  // Longest-keyword-first so a more specific rule (e.g. "AMAZONPRIME") wins
  // over a shorter one that would otherwise also match ("AMAZON").
  CATEGORY_LOOKUP = Object.entries(CATEGORIES)
    .flatMap(([cid, c]) => c.keywords.map(kw => [kw, cid]))
    .sort((a, b) => b[0].length - a[0].length);
}

// The keyword-matching half of categorization — usable on any raw
// description, independent of transaction type. A cuota's own description
// is never a merchant name (the bank always prints it as "COMPRAS A PLAZO ·
// CUOTA:0N/0M"), so this is only meaningful for cargo descriptions — but
// it's also how a plan's *inherited* category is computed, from its
// matched origin cargo's description (see identifySupersededCargos).
function categorizeByKeyword(descripcion) {
  const upper = (descripcion || '').toUpperCase();
  const hit = CATEGORY_LOOKUP.find(([kw]) => upper.includes(kw));
  return hit ? hit[1] : null;
}

// Créditos are keyword-categorized too, so a refund/rebate nets against the
// category of the purchase it reverses (or lands in "refund" when it's a
// generic rebate like "Reembolso Rebate Local").
function categorize(t) {
  if (t.tipo === 'cuota') return PLAN_CATEGORY.get(t) || 'installments';
  if (t.tipo !== 'cargo' && t.tipo !== 'credito') return null;
  return categorizeByKeyword(t.descripcion);
}

// 'YYYY-MM' corte month -> category id ('_uncat' when none) -> USD. Same rows
// and categories as the Transacciones page, so refunds net against their
// category and land in 'refund' when generic.
function spendByMonthCategory(monthsFilter) {
  const out = {};
  eachSpendTx((t, month) => {
    const cid = categorize(t) || '_uncat';
    (out[month] ??= {})[cid] = (out[month][cid] || 0) + txUsd(t);
  }, monthsFilter);
  return out;
}

/* ── Merchant aggregation, active subscriptions ──
   Moved verbatim from transactions.html (P3 3.4 reuses them). */

// merchant -> { usd, n, nCargo, months:Set, cid, lastDate }. `cid` comes
// from the merchant's first cargo when it has one — a credit only sets it for
// a credit-only merchant — so a refund line can't relabel a merchant. `lastDate`
// is the latest tx date, falling back to the statement's fecha_corte.
// `include`, when given, keeps only the spend rows it returns true for.
function computeMerchants(monthsFilter, include) {
  const m = {};
  eachSpendTx((t, month, card, st) => {
    if (include && !include(t)) return;
    const key = normalizeMerchant(t.descripcion);
    const rec = (m[key] ??= { usd: 0, n: 0, nCargo: 0, months: new Set(), cid: categorize(t) || '_uncat', lastDate: '' });
    if (t.tipo === 'cargo' && !rec.nCargo) rec.cid = categorize(t) || '_uncat';
    if (t.tipo === 'cargo') rec.nCargo += 1;
    rec.usd += txUsd(t); rec.n += 1; rec.months.add(month);
    const date = t.fecha || st.fecha_corte;
    if (date > rec.lastDate) rec.lastDate = date;
  }, monthsFilter);
  return m;
}

// Active subscriptions (port of the APK's activeSubscriptions): merchants
// whose spend rows are categorized streaming/services, seen in ≥2 months of
// the selected period AND charged in its latest month — so a cancelled
// service drops off even over "Todo el período". Averages are per active month.
// `categoryIds` widens the category set (the projection page adds utilities
// and insurance); transactions.html uses the default.
const SUBSCRIPTION_CATEGORY_IDS = new Set(['streaming', 'services']);

function computeActiveSubscriptions(monthsFilter, categoryIds = SUBSCRIPTION_CATEGORY_IDS) {
  const selected = monthsFilter ? [...monthsFilter].sort() : allMonths();
  if (!selected.length) return [];
  const latest = selected[selected.length - 1];
  const merchants = computeMerchants(new Set(selected), t => categoryIds.has(categorize(t)));
  return Object.entries(merchants)
    .filter(([, r]) => r.months.size >= 2 && r.months.has(latest))
    .map(([name, r]) => ({ name, cid: r.cid, monthsCount: r.months.size, lastDate: r.lastDate,
                           avgUsd: r.usd / r.months.size }))
    .sort((a, b) => b.avgUsd - a.avgUsd);
}
