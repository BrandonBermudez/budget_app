/* Flujo de caja — pure monthly cash-flow analytics (no DOM, no fetch).
   Port of the APK's domain/CashFlowAnalytics. Needs spend.js (loaded via
   loadSpend) and debit.js in the same global scope.

   Per month: income = debit credito rows; outflow = debit debito rows +
   statement spend by corte month. Card-payment rows (matchCardPayment) are
   the same money as that card's statement spend, so they are dropped from
   both sides — by row identity, never by referencia — and only reported as
   an informational "excluded" total. */
'use strict';

// Statement spend per corte month, from the same engine as the
// Transacciones page (isSpend, superseded cargos, refunds, txUsd).
function cardSpendByMonth(monthsFilter) {
  const out = {};
  Object.entries(spendByMonthCategory(monthsFilter)).forEach(([month, byCat]) => {
    out[month] = Object.values(byCat).reduce((a, b) => a + b, 0);
  });
  return out;
}

// Union of debit-activity months and statement corte months, ascending.
function cashFlowMonths(debitTxs) {
  const set = new Set(allMonths());
  debitTxs.forEach(t => { if (t.fecha) set.add(t.fecha.slice(0, 7)); });
  return [...set].sort();
}

// A TF "PAGO xxxx-xx**-****-NNNN" row whose last-4 isn't in CARD_PAYMENT_MAP.
// matchCardPayment returns null for these, so they stay in debit outflow
// (their statement spend isn't loaded — excluding them would under-count);
// the page flags them instead. The APK excludes them as unmapped payments.
function isUnmappedCardPayment(t) {
  if (t.codigo !== 'TF') return false;
  const m = t.descripcion.match(PAGO_CARD_RE);
  return !!m && !CARD_PAYMENT_MAP[m[1]];
}

// Most recent month first. `monthsFilter`: Set of 'YYYY-MM' or null (all).
// Returns { rows, unmappedCount, unmappedTotal } — unmapped counted within
// the filtered months only.
function cashFlowByMonth(debitTxs, monthsFilter) {
  const inRange = m => !monthsFilter || monthsFilter.has(m);
  const payments = new Set(debitTxs.filter(t => matchCardPayment(t)));
  const acc = {};
  const get = m => (acc[m] ??= { income: 0, debitOutflow: 0, paymentsExcluded: 0, paymentsCount: 0 });
  let unmappedCount = 0, unmappedTotal = 0;

  debitTxs.forEach(t => {
    const m = (t.fecha || '').slice(0, 7);
    if (!m || !inRange(m)) return;
    const a = get(m);
    if (payments.has(t)) {
      a.paymentsExcluded += t.debito || 0;
      a.paymentsCount += 1;
      return;
    }
    if (isUnmappedCardPayment(t)) { unmappedCount += 1; unmappedTotal += t.debito || 0; }
    a.income += t.credito || 0;
    a.debitOutflow += t.debito || 0;
  });

  const card = cardSpendByMonth(monthsFilter);
  const rows = cashFlowMonths(debitTxs).filter(inRange).reverse().map(month => {
    const a = acc[month] || get(month);
    const cardSpend = card[month] || 0;
    const outflow = a.debitOutflow + cardSpend;
    const net = a.income - outflow;
    return {
      month,
      income: a.income,
      debitOutflow: a.debitOutflow,
      cardSpend,
      outflow,
      net,
      paymentsExcluded: a.paymentsExcluded,
      paymentsCount: a.paymentsCount,
      savingsPct: a.income > 0 ? net / a.income * 100 : null,
    };
  });
  return { rows, unmappedCount, unmappedTotal };
}
