/* Debit-account pure logic (no DOM) — shared by debit_account.html and the
   P3 pages (cash flow, bills, projection). Rendering stays in the templates. */
'use strict';

/* ── Transaction categorization — TF description patterns, not
   user-editable like credit-card merchant categories (these are fixed
   bank-format patterns, not free-text merchant names). ── */

// CARD_PAYMENT_MAP ("PAGO XXXX-XX**-****-XXXX" last-4 → {key, label, color})
// is defined by the page from data/card_map.json `payment_last4` — card
// digits are config, so they're served by the backend rather than hardcoded
// here.
const PAGO_CARD_RE = /^PAGO\s+\d{4}-\d{2}\*\*-\*\*\*\*-(\d{4})/;

function matchCardPayment(t) {
  if (t.codigo !== 'TF') return null;
  const m = t.descripcion.match(PAGO_CARD_RE);
  if (!m) return null;
  const card = CARD_PAYMENT_MAP[m[1]];
  if (!card) console.warn('Card payment with unmapped last-4 digits, add it to payment_last4 in data/card_map.json:', t.descripcion);
  return card || null;
}
