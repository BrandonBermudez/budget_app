"""Debit/checking-account CSV statement parsing.

A completely different source document from parser.py's credit-card
PDF/markdown statements: a single-currency CSV export ("Transacciones del
mes") of one checking account's transaction ledger, with a running balance
column and the bank's own per-transaction-code debit/credit summary in a
footer section. That running balance and footer summary make this format
self-validating in a way the credit-card statements aren't — see
validate_debit_csv().
"""
import csv
import io
import math
from datetime import date

# The bank's own transaction-code vocabulary, as printed in its exports — used only
# for a human-readable label in the UI, never for parsing/validation logic.
CODE_LABELS = {
    'AR': 'Depósito ATM',
    'AT': 'Retiro ATM',
    'CM': 'Comisión',
    'DP': 'Depósito',
    'TF': 'Transferencia/Pago',
    'TS': 'Transacción',
    '59': 'Comisión',
    '3O': 'Intereses',
    '3Q': 'Retención de Impuesto',
}


def decode_bank_csv(raw_bytes: bytes) -> str:
    """The bank's export is Windows-1252 (Latin-1-superset) — 'Número' etc. UTF-8
    is tried first in case a future export changes encoding; cp1252 can
    decode any byte sequence, so it's the safe fallback, not the first try."""
    try:
        return raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        return raw_bytes.decode('cp1252')


class DebitParseError(ValueError):
    """A file that can't be trusted at all — nothing from it may be saved.
    `code` is a stable machine-readable id (ported from the APK's
    DebitParseOutcome.Error codes); str(e) is the Spanish message shown to
    the user (app.py's upload route surfaces str(e) as the 422 error)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _parse_date_dmy(s: str) -> str:
    """'03/01/2026' (DD/MM/YYYY) -> '2026-01-03'; '' if not a real date."""
    parts = s.strip().split('/')
    if len(parts) != 3:
        return ''
    try:
        return date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat()
    except ValueError:
        return ''


def _num(s: str, blank=None):
    """Amount cell -> float, or None if unreadable. `blank` is what an empty
    cell means (debito/credito legitimately leave one side empty; a balance
    or header saldo never may). An unreadable amount must never become 0.0
    — that would silently turn a garbled row into a fake $0 entry.

    float() also accepts "inf"/"nan"/overflow literals like "1e400"
    without raising — a non-finite value would later get merged into
    persisted transaction data and serialized as an invalid-JSON
    Infinity/NaN token (json.dump's default allow_nan=True), breaking the
    browser's strict-JSON res.json() on every future page load, and would
    silently defeat validate_debit_csv's balance-chain check (every
    comparison against NaN is False). So those are unreadable too."""
    s = (s or '').strip().replace(',', '')
    if not s:
        return blank
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _row_to_tx(cells):
    """One detail row -> transaction dict, or None when the row isn't a
    usable transaction (the caller decides whether that's worth a warning).

    descripcion is free text a person may have typed (transfer memos) —
    the bank's export doesn't quote fields, so a literal comma in there splits
    into extra cells and would silently shift every fixed-position field
    after it. debito/credito/balance are always the LAST three columns of a
    detail row regardless of how many commas landed in the description, so
    they're anchored from the end (never cells[4]/[5]/[6])."""
    if len(cells) < 6:
        return None
    fecha = _parse_date_dmy(cells[0])
    debito = _num(cells[-3], blank=0.0)
    credito = _num(cells[-2], blank=0.0)
    balance = _num(cells[-1])
    if not fecha or debito is None or credito is None or balance is None:
        return None
    return {
        "fecha":       fecha,
        "referencia":  cells[1],
        "codigo":      cells[2],
        "descripcion": ', '.join(cells[3:-3]),
        "debito":      debito,
        "credito":     credito,
        "balance":     balance,
    }


def _has_content(cells) -> bool:
    """4+ non-empty cells = a row that carries real data (APK rule), so
    failing to parse it is data loss, not noise like a blank/title line."""
    return sum(1 for c in cells if c) >= 4


def _unparsed_warning(code, line_idx, raw_row):
    return {"code": code, "line": line_idx + 1, "row": ','.join(raw_row)}


def parse_debit_csv(content: str) -> dict:
    """Parse one 'Transacciones del mes' CSV export. Despite the filename,
    a single export can span many months — callers should treat each upload as a date-ranged batch of
    transactions to merge into one continuous account ledger, not as a
    single calendar-month statement the way credit-card statements are.

    Raises DebitParseError when the file can't be trusted at all (no or
    short account header, unreadable saldo/fecha, no detail section, zero
    transactions). Otherwise nothing is dropped silently: any content row
    (see _has_content) that yields no transaction or footer entry goes into
    the returned 'warnings' list with its 1-based line number and raw text;
    validate_debit_csv() carries those into its result so the upload
    preview's acknowledge gate catches them.
    """
    reader = list(csv.reader(io.StringIO(content)))

    header = None
    detalle_start = None
    for i, row in enumerate(reader):
        cells = [c.strip() for c in row]
        if header is None and cells and cells[0].isdigit():
            header = cells
        if cells and cells[0].lower().startswith('fecha de transacci'):
            detalle_start = i + 1
            break

    if header is None:
        raise DebitParseError('header_not_found',
            "Formato de CSV no reconocido. Esta app es una plantilla que solo lee el CSV de un banco; para usar el tuyo hay que escribir un parser (ver README, «Adding your bank»).")
    if len(header) < 9:
        raise DebitParseError('header_too_short',
            "La fila de la cuenta está incompleta — el formato del archivo pudo haber cambiado.")
    saldo_inicial = _num(header[4])
    if saldo_inicial is None:
        raise DebitParseError('bad_saldo_inicial',
            f"No se pudo leer el Saldo Inicial ('{header[4]}') — sin él no se puede validar el saldo.")
    saldo_final = _num(header[5])
    if saldo_final is None:
        raise DebitParseError('bad_saldo_final',
            f"No se pudo leer el Saldo en Libros ('{header[5]}').")
    saldo_disponible = _num(header[7], blank=0.0)
    if saldo_disponible is None:
        raise DebitParseError('bad_saldo_disponible',
            f"No se pudo leer el Saldo Disponible ('{header[7]}').")
    fecha_snapshot = _parse_date_dmy(header[8])
    if not fecha_snapshot:
        raise DebitParseError('bad_fecha_snapshot',
            f"No se pudo leer la fecha del estado ('{header[8]}').")
    if detalle_start is None:
        raise DebitParseError('detail_header_not_found',
            "No se encontró la sección de detalle ('Fecha de Transacción') en el archivo.")

    warnings = []
    transactions = []
    resumen_start = None
    for i in range(detalle_start, len(reader)):
        cells = [c.strip() for c in reader[i]]
        if cells and cells[0].lower().startswith('resumen de estado'):
            resumen_start = i + 1
            break
        tx = _row_to_tx(cells)
        if tx is not None:
            transactions.append(tx)
        elif _has_content(cells):
            warnings.append(_unparsed_warning('detail_row_unparsed', i, reader[i]))

    if not transactions:
        raise DebitParseError('no_transactions',
            "No se encontraron transacciones — revisa que el archivo sea el CSV de 'Transacciones del mes'.")

    resumen_codigos = {}
    resumen_total   = None
    if resumen_start is not None:
        for i in range(resumen_start, len(reader)):
            cells = [c.strip() for c in reader[i]]
            if not cells or not cells[0]:
                continue
            if cells[0].lower().startswith('código transacci') or cells[0].lower().startswith('codigo transacci'):
                continue
            nums = [_num(c) for c in cells[1:5]] if len(cells) >= 5 else [None]
            if any(n is None for n in nums):
                if _has_content(cells):
                    warnings.append(_unparsed_warning('footer_row_unparsed', i, reader[i]))
                continue
            entry = {
                "debitos_count":  int(nums[0]),
                "debitos_monto":  nums[1],
                "creditos_count": int(nums[2]),
                "creditos_monto": nums[3],
            }
            if cells[0].lower() == 'total':
                resumen_total = entry
            else:
                resumen_codigos[cells[0]] = entry

    return {
        "numero_cliente":    header[0],
        "nombre":            header[1],
        "cuenta":            header[2],
        "moneda":            header[3],
        "saldo_inicial":     saldo_inicial,
        "saldo_final":       saldo_final,
        "saldo_disponible":  saldo_disponible,
        "fecha_snapshot":    fecha_snapshot,
        "transactions":      transactions,
        "resumen_codigos":   resumen_codigos,
        "resumen_total":     resumen_total,
        "warnings":          warnings,
    }


def validate_debit_csv(parsed: dict, tolerance: float = 0.01) -> dict:
    """Two independent checks, both against data the bank prints in the same
    file (no cross-document ambiguity like the credit-card validator has):

    1. Running-balance chain — replay saldo_inicial through every
       transaction in order and compare against each row's own printed
       balance. Pinpoints the exact first row where parsing diverged from
       the source document, rather than only an aggregate total.
    2. Footer reconciliation — the printed 'Resumen de Estado Bancario'
       per-code and grand-total debit/credit counts+amounts, compared
       against the same figures recomputed from the parsed transactions.

    Returns None if there's nothing to check against (no transactions and
    no footer).
    """
    txs = parsed.get('transactions') or []
    if not txs and not parsed.get('resumen_total'):
        return None

    # 1. Balance chain
    balance_breaks = []
    running = parsed.get('saldo_inicial', 0.0)
    for i, t in enumerate(txs):
        running = round(running - t['debito'] + t['credito'], 2)
        if abs(running - t['balance']) > tolerance:
            balance_breaks.append({
                "index": i, "fecha": t['fecha'], "referencia": t['referencia'],
                "descripcion": t['descripcion'],
                "computed_balance": running, "printed_balance": t['balance'],
            })
            running = t['balance']  # resync so one bad row doesn't cascade

    # 2. Footer reconciliation
    from collections import defaultdict
    agg = defaultdict(lambda: {"debitos_count": 0, "debitos_monto": 0.0,
                                "creditos_count": 0, "creditos_monto": 0.0})
    for t in txs:
        code = t['codigo']
        if t['debito']:
            agg[code]['debitos_count']  += 1
            agg[code]['debitos_monto']  = round(agg[code]['debitos_monto'] + t['debito'], 2)
        if t['credito']:
            agg[code]['creditos_count'] += 1
            agg[code]['creditos_monto'] = round(agg[code]['creditos_monto'] + t['credito'], 2)

    # A missing footer (resumen_codigos empty) isn't a reconciliation
    # failure — there's nothing printed to reconcile against — so this
    # check is skipped entirely rather than comparing against an implicit
    # "everything is zero" default, which would otherwise flag every single
    # code the file uses as mismatched.
    footer_mismatches = []
    printed_codigos = parsed.get('resumen_codigos') or {}
    if printed_codigos:
        all_codes = set(printed_codigos) | set(agg)
        for code in all_codes:
            printed  = printed_codigos.get(code, {"debitos_count": 0, "debitos_monto": 0.0,
                                                    "creditos_count": 0, "creditos_monto": 0.0})
            computed = agg.get(code, {"debitos_count": 0, "debitos_monto": 0.0,
                                       "creditos_count": 0, "creditos_monto": 0.0})
            if (printed['debitos_count'] != computed['debitos_count']
                    or abs(printed['debitos_monto'] - computed['debitos_monto']) > tolerance
                    or printed['creditos_count'] != computed['creditos_count']
                    or abs(printed['creditos_monto'] - computed['creditos_monto']) > tolerance):
                footer_mismatches.append({"codigo": code, "printed": printed, "computed": computed})

    printed_total = parsed.get('resumen_total')
    total_mismatch = None
    if printed_total:
        computed_total = {
            "debitos_count":  sum(v['debitos_count'] for v in agg.values()),
            "debitos_monto":  round(sum(v['debitos_monto'] for v in agg.values()), 2),
            "creditos_count": sum(v['creditos_count'] for v in agg.values()),
            "creditos_monto": round(sum(v['creditos_monto'] for v in agg.values()), 2),
        }
        if (printed_total['debitos_count'] != computed_total['debitos_count']
                or abs(printed_total['debitos_monto'] - computed_total['debitos_monto']) > tolerance
                or printed_total['creditos_count'] != computed_total['creditos_count']
                or abs(printed_total['creditos_monto'] - computed_total['creditos_monto']) > tolerance):
            total_mismatch = {"printed": printed_total, "computed": computed_total}

    # Rows the parser couldn't turn into a transaction/footer entry. Usually
    # also visible as a balance break, but not always (e.g. a garbled row
    # whose net is 0, or a lost footer row) — so they fail validation on
    # their own and reach the preview's acknowledge gate either way.
    warnings = parsed.get('warnings') or []

    return {
        "ok": not balance_breaks and not footer_mismatches and not total_mismatch and not warnings,
        "warnings":         warnings,
        "balance_breaks":   balance_breaks,
        "footer_mismatches": footer_mismatches,
        "total_mismatch":   total_mismatch,
    }
