"""Credit-card statement parsing — PDF/markdown → structured transactions.

Only dollar amounts are imported: a row whose amount is printed only in the
bank's local-currency column/marker is skipped.

Extracted from app.py so the parsing logic (which has no dependency on
Flask/session/request) can be read and reasoned about on its own.
"""
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import date

from markitdown import MarkItDown

_md_converter = MarkItDown()


def convert_pdf_to_markdown(file_storage) -> str:
    """Save an uploaded PDF to a temp file, convert it to markdown, then clean up."""
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        file_storage.save(tmp_path)
        result = _md_converter.convert(tmp_path)
        return result.text_content
    finally:
        os.remove(tmp_path)


# Month map for Spanish abbreviated months
_MONTH_MAP = {
    'ENE': 1, 'FEB': 2, 'MAR': 3, 'ABR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AGO': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DIC': 12
}

# Per-user card identities, kept out of the code because they hold real card
# digits: data/card_map.json (gitignored; see data/card_map.example.json).
# Missing file → no known cards; identity falls back to the header text below.
_CARD_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'card_map.json')
try:
    with open(_CARD_MAP_FILE, encoding='utf-8') as _f:
        CARDS = json.load(_f)['cards']
except FileNotFoundError:
    CARDS = []

# Account-number → canonical (display name, key). Checked BEFORE the header
# text below, because the account number is the card's stable identity while
# the bank's header *name* text varies between statements for the same card.
# Keying off the number keeps every statement for one physical card under a
# single, name-based key. Onboard a card by adding its account-number suffix
# to `account_suffixes` in card_map.json — the source of truth for identity.
_ACCOUNT_MAP = {s.upper(): (c['name'], c['key'])
                for c in CARDS for s in c.get('account_suffixes', [])}

# Card name normalisation — maps fragments found in headers to display names.
# Fallback for cards whose account number isn't in _ACCOUNT_MAP yet.
# Regexes from card_map.json `header_patterns`, tried in file order.
_CARD_NAME_MAP = [(p, c['name']) for c in CARDS for p in c.get('header_patterns', [])]

# Transaction-line regexes — compiled once at module load, not per-call.

# Raw text transaction lines: ref  [code]  MON/DD  DESCRIPTION  [LOCATION]  [currency]AMOUNT[-]
#
# Ref is 4+ digits and the code is optional: "SU PAGO RECIBIDO" lines carry no
# code and bonificación lines carry a 4-digit ref ("0000 JUL/01 BONIFICACION
# ..."), and both used to be silently dropped. The code group is
# non-capturing so the group indexes below don't move. The amount needs no
# leading digit ("$.41-") but must end in exactly two decimals.
RAW_TX = re.compile(
    r'^(\d{4,})\s+(?:\w+\s+)?'       # ref + optional code (usually digits, but a
                                      # few are alphanumeric, e.g. "N001")
    r'([A-Z]{3}/\d{2})\s+'           # MON/DD
    r'(.+?)\s{2,}'                   # description (2+ spaces before amount)
    r'(?:\S+\s+)?'                   # optional location token
    r'(C\$\s*|NI\s*|\$)?'           # optional currency prefix (local or dollar)
    r'([\d,]*\.\d{2}-?)$'            # amount
)

# Fallback for lines RAW_TX misses — the PDF-to-markdown conversion doesn't
# reliably leave 2+ spaces between description and amount; long descriptions
# often collapse table-column padding to a single space, or even zero (e.g.
# "...EXAMPLE CITY USA<marker> $1.23", where the location and the currency
# marker are glued together with no space at all). RAW_TX's hard `\s{2,}`
# requirement silently drops those lines entirely instead of just parsing
# them with an uglier description, so
# this is tried only when RAW_TX fails — every line RAW_TX already handles
# keeps its exact current parse (money/date/type untouched), and this only
# recovers otherwise-missing transactions.
#
# Description is lazy and the tail (optional local-currency marker, optional
# currency prefix, amount) is anchored to the true end of line — backtracking
# finds the correct split because the amount is always preceded by a
# non-digit character (a currency marker, a "$", or plain description text),
# so the first (shortest, leftmost) position where the tail matches is
# always the real one, never a false split into the amount's own digits.
RAW_TX_FALLBACK = re.compile(
    r'^(\d{4,})\s+(?:\w+\s+)?'       # ref + optional code (see RAW_TX)
    r'([A-Z]{3}/\d{2})\s+'           # MON/DD
    r'(.+?)\s*'                      # description (lazy)
    r'(?:NI\s*)?'                    # optional local-currency marker — informational only, not captured
    r'(C\$\s*|\$\s*)?'               # optional currency prefix directly before the amount
    r'([\d,]*\.\d{2}-?)$'            # amount, anchored to end of line
)

# Table row transactions: | ref | MON/DD | description | | amount | usd_amount |
# Some layouts put the code in the ref cell ("| 000000000000  000 | AGO/01 |")
# or an empty cell between ref and date ("| 0000 |     | AGO/01 |"). Only
# ref/date/description are read from the match; amounts come from
# _table_cell_amounts.
TABLE_TX = re.compile(
    r'^\|\s*(\d{4,})(?:\s+\w+)?\s*\|(?:\s*\|)*\s*([A-Z]{3}/\d{2})\s*\|\s*(.+?)\s*\|.*?'
    r'\|\s*([\d,\.]*-?)\s*\|\s*\$?([\d,\.]*-?)\s*\|'
)

# A single table cell holding an amount: "123.45-", "$67.89-", "$.08-".
TABLE_AMOUNT_CELL = re.compile(r'^\$?\s*[\d,]*\.\d{2}-?$')


def _table_cell_amounts(line: str) -> tuple[str, str]:
    """(local-currency, dollar) amount strings from a table row's cells after
    the description, '' when absent. A cell with "$" is dollars, else local
    currency.

    Replaces TABLE_TX's positional amount groups, which grab the first two
    cells after the description: in wider layouts those are empty padding
    cells (payment/bonificación rows were dropped) or the local-currency
    column lands in the dollar group (local-currency "SU PAGO RECIBIDO" and
    bonificación rows were stored as dollars).
    """
    local = usd = ''
    for cell in (c.strip() for c in line.strip().strip('|').split('|')[3:]):
        if not TABLE_AMOUNT_CELL.match(cell):
            continue
        if '$' in cell:
            usd = usd or cell
        else:
            local = local or cell
    return local, usd

_BONIF_MARKERS = ('BONIFICACION', 'INTERESES CORRIENTES')

# Currency marker directly before a raw line's trailing amount. A bare "$" is
# dollars, even after the local-currency marker; the other markers matched by
# _LOCAL_MARKER are local currency.
_USD_MARKER = re.compile(r'(?<!C)\$\s*[\d,]*\.\d{2}-?$')
_LOCAL_MARKER = re.compile(r'(?:C\$|NI(?:C)?)\s*[\d,]*\.\d{2}-?$')


def _is_usd_line(line: str, desc: str) -> bool:
    if _USD_MARKER.search(line):
        return True
    if _LOCAL_MARKER.search(line):
        return False
    # No marker at all: Amazon charges are billed in dollars. Kept only as a
    # fallback — an explicit marker always wins.
    return 'AMZN' in desc or 'AMAZON' in desc

# Unparsed-line heuristic (port of the APK's LOOKS_LIKE_TX): a 4+ digit run
# (ref) plus an amount. Such a line that no tx pattern parsed is reported in
# parsed['unparsed'] instead of being dropped silently. Unlike the APK, a
# dash-joined run doesn't count: the bank's phone number ("0000-0000") heads
# the SALDO AL CORTE value row, which would otherwise be flagged on every statement.
LOOKS_LIKE_REF = re.compile(r'(?<![\d-])\d{4,}(?![\d-])')
LOOKS_LIKE_AMOUNT = re.compile(r'[\d,]*\.\d{2}')


def _looks_like_tx(line: str) -> bool:
    return bool(LOOKS_LIKE_REF.search(line) and LOOKS_LIKE_AMOUNT.search(line))


def _matches_tx_pattern(line: str) -> bool:
    return bool(RAW_TX.match(line) or RAW_TX_FALLBACK.match(line)
                or (TABLE_TX.match(line) and any(_table_cell_amounts(line))))

SUBCARD_RE = re.compile(r'^\*{4}-\*{6}-\*(\d{4})')

# Cuotas a plazo raw lines (different format: no 12-digit ref). The
# local-currency marker is not always printed ("... CUOTA:03/03 $12.34").
CUOTA_RE = re.compile(
    r'^(\w{10,})\s+([A-Z]{3}/\d{2})\s+(COMPRAS A PLAZO)\s+(CUOTA:\d+/\d+)\s+(?:NI\s+)?\$([\d,\.]+)'
)


def _normalise_card_name(header_line: str, account_no: str = '') -> tuple[str, str]:
    """Return (card_name, card_key) from statement header line.

    Resolution order: (1) the account number against `_ACCOUNT_MAP` — the
    stable identity, robust to the bank's varying header name text; (2) the header
    text against `_CARD_NAME_MAP`; (3) the raw account number; (4) a hash.

    `account_no` (e.g. 'XXXXX0000'), when the caller has it, is passed in
    separately because the caller's regex captures the name text *after* the
    account number, so it's no longer present in `header_line` to re-find.
    """
    # 1. Account number is the source of truth for a known card — a physical
    #    card keeps one key even when its header name text changes month to month.
    if account_no and account_no.upper() in _ACCOUNT_MAP:
        return _ACCOUNT_MAP[account_no.upper()]

    upper = header_line.upper()
    for pattern, name in _CARD_NAME_MAP:
        if re.search(pattern, upper):
            key = name.lower().replace(' ', '_')
            return name, key
    # Fallback: use the account number if the caller has one — the strongest
    # available discriminator between two different unrecognized cards.
    if account_no:
        return account_no, account_no.lower().replace(' ', '_')
    # No account number either — fall back to a deterministic hash of the
    # header text so two different unrecognized cards don't collide under
    # the same key. (hashlib, not hash(), since Python's built-in hash() is
    # randomized per-process and wouldn't be stable across server restarts.)
    digest = hashlib.sha256(header_line.strip().encode('utf-8')).hexdigest()[:8]
    key = 'desconocida_' + digest
    return 'Tarjeta Desconocida', key


def _parse_date_slash(s: str) -> str:
    """Parse d/mm/yy or d/mm/yyyy → ISO date. e.g. '3/07/26' → '2026-07-03'"""
    parts = s.strip().split('/')
    if len(parts) == 3:
        d, m, y = parts
        y = int(y)
        if y < 100:
            y += 2000
        return f"{y:04d}-{int(m):02d}-{int(d):02d}"
    return ''


def _clean_amount(s: str) -> float:
    """'1,234.56' or '$12.34' or '123.45-' → float (negative if ends with -)"""
    s = s.strip().replace(',', '').replace('$', '').replace('C', '')
    negative = s.endswith('-')
    s = s.rstrip('-').strip()
    try:
        val = float(s)
    except ValueError:
        return 0.0
    # See debit_parser._num's comment — float() accepts "inf"/"nan"/overflow
    # literals without raising, and a non-finite value here would later be
    # persisted as an invalid-JSON Infinity/NaN token, breaking every
    # future page load.
    if not math.isfinite(val):
        return 0.0
    return -val if negative else val


def parse_statement(content: str) -> dict:
    lines = content.splitlines()

    # ── Header fields ──
    card_name  = 'Tarjeta Desconocida'
    card_key   = 'desconocida'
    fecha_corte = ''
    fecha_limite = ''
    fecha_bonif  = ''
    saldo_usd  = None
    pago_min_usd = 0.0
    pago_cont_usd = 0.0

    # Parse header table rows
    for line in lines[:30]:
        # Card name line: | Número de cuenta | XXXXX0000 EXAMPLE CARD | ...
        m = re.search(r'N[uú]mero de cuenta.*?(X+\d+)\s+(.+?)(?:\s*\||\s*$)', line, re.IGNORECASE)
        if m:
            card_name, card_key = _normalise_card_name(m.group(2).strip(), account_no=m.group(1))

        m = re.search(r'Fecha de corte\s*\|\s*([\d/]+)', line, re.IGNORECASE)
        if m:
            fecha_corte = _parse_date_slash(m.group(1))

        m = re.search(r'Fecha l[ií]mite para pagar\s*\|\s*(\d+/\w+/?\d*)', line, re.IGNORECASE)
        if m:
            fecha_limite = _parse_date_slash(m.group(1).replace('AGO','08').replace('JUL','07')
                .replace('JUN','06').replace('MAY','05').replace('ABR','04').replace('MAR','03')
                .replace('FEB','02').replace('ENE','01').replace('SEP','09').replace('OCT','10')
                .replace('NOV','11').replace('DIC','12'))

        m = re.search(r'Fecha l[ií]mite para\s*\|\s*(\d+/\w+/?\d*)', line, re.IGNORECASE)
        if m and not fecha_bonif:
            raw = m.group(1)
            for es, num in [('ENE','01'),('FEB','02'),('MAR','03'),('ABR','04'),('MAY','05'),
                            ('JUN','06'),('JUL','07'),('AGO','08'),('SEP','09'),('OCT','10'),
                            ('NOV','11'),('DIC','12')]:
                raw = raw.replace(es, num)
            fecha_bonif = _parse_date_slash(raw) if '/' in raw else ''

        # The local-currency figure precedes the dollar one; only the dollar
        # figure is kept.
        m = re.search(r'Pago m[ií]nimo\s*\|\s*C\$\s*[\d,\.]+\s*\|\s*\|\s*\$([\d,\.]+)', line, re.IGNORECASE)
        if m:
            pago_min_usd = _clean_amount(m.group(1))

        m = re.search(r'Pago de contado\s*\|\s*C\$\s*[\d,\.]+\s*\|\s*\|\s*\$([\d,\.]+)', line, re.IGNORECASE)
        if m:
            pago_cont_usd = _clean_amount(m.group(1))

    # "SALDO AL CORTE" and "Saldo Anterior" aren't reliably in the first 30
    # lines — some statement layouts print the transaction detail (and thus
    # push these lines) well past that window — so these are searched across
    # the whole document instead. Both patterns are specific enough (a fixed
    # label immediately followed by a local-currency and a $ figure) that a
    # false match elsewhere in the document — e.g. inside the boilerplate
    # legal text — is not a real risk. First match wins.
    saldo_anterior_usd = None
    for line in lines:
        if saldo_usd is None:
            m = re.search(r'SALDO AL CORTE.*?C\$\s*[\d,\.]+.*?\$([\d,\.]+)', line, re.IGNORECASE)
            if m:
                saldo_usd = _clean_amount(m.group(1))
        if saldo_anterior_usd is None:
            m = re.search(r'Saldo Anterior.*?C\$\s*[\d,\.]+.*?\$([\d,\.]+)', line, re.IGNORECASE)
            if m:
                saldo_anterior_usd = _clean_amount(m.group(1))
    saldo_usd = saldo_usd or 0.0
    saldo_anterior_usd = saldo_anterior_usd or 0.0

    # Derive year from fecha_corte for transaction date parsing
    corte_year = int(fecha_corte[:4]) if fecha_corte else date.today().year
    corte_month = int(fecha_corte[5:7]) if fecha_corte else date.today().month

    # ── Transaction parsing ──
    transactions = []
    unparsed = []
    current_subcards = 'principal'

    for line in lines:
        # Track sub-card context
        sm = SUBCARD_RE.match(line.strip())
        if sm:
            current_subcards = f'*{sm.group(1)}'
            continue

        # Skip noise lines. NOTE: 'seguro' is deliberately not in this list —
        # the bank prints real monthly insurance-premium charges ("SEGURO
        # ...") as ordinary transaction lines, and filtering the word would
        # silently drop them (the boilerplate paragraphs about insurance
        # don't start with a ref number, so they never match the tx regexes
        # anyway).
        low = line.lower()
        if any(x in low for x in [
            'saldo anterior', 'serie a', 'millas', 'disponible', 'retiro',
            'intereses corrientes', 'servicio al cliente', 'informacion de su',
            'resumen de compras', 'sorteo', 'acciones ganadas',
            '-----', 'número referencia', 'saldo al corte'
        ]):
            # These are substring matches, so a real transaction whose
            # description happens to contain one would vanish — surface it.
            if _matches_tx_pattern(line.strip()):
                unparsed.append(line.strip())
            continue

        # Try raw text format first, falling back to the looser single-space
        # variant only when the strict one misses — see RAW_TX_FALLBACK comment.
        m = RAW_TX.match(line.strip()) or RAW_TX_FALLBACK.match(line.strip())
        if m:
            ref, date_str, desc, _, amt_str = m.groups()
            month_abbr = date_str[:3]
            day        = int(date_str[4:])
            mo         = _MONTH_MAP.get(month_abbr, 0)
            # If transaction month is ahead of corte month by more than 2, it's previous year
            yr = corte_year if mo <= corte_month + 1 else corte_year - 1
            tx_date = f"{yr:04d}-{mo:02d}-{day:02d}" if mo else ''

            # Local-currency rows are not imported.
            if not _is_usd_line(line.strip(), desc):
                continue
            amt    = _clean_amount(amt_str)
            is_cr  = amt < 0
            # Same classification as the table path below — a raw-text
            # "BONIFICACION PAGO SALDO" line is a bonificación, not a credit
            # (a credit would net against spend in validate_statement).
            if any(x in desc.upper() for x in _BONIF_MARKERS):
                tipo = "bonificacion"
            else:
                tipo = "credito" if is_cr else "cargo"

            transactions.append({
                "ref":        ref,
                "fecha":      tx_date,
                "descripcion": desc.strip(),
                "monto_usd":  amt,
                "tipo":       tipo,
                "subtarjeta": current_subcards,
            })
            continue

        # Try table row format
        m = TABLE_TX.match(line.strip())
        if m:
            ref, date_str, desc = m.groups()[:3]
            month_abbr = date_str[:3]
            day        = int(date_str[4:])
            mo         = _MONTH_MAP.get(month_abbr, 0)
            yr = corte_year if mo <= corte_month + 1 else corte_year - 1
            tx_date = f"{yr:04d}-{mo:02d}-{day:02d}" if mo else ''

            amt_local, amt_usd = _table_cell_amounts(line)
            # Skip rows with no amount at all — these are table fragments
            # (headers, separators, wrapped text), not transactions.
            if not amt_local.strip() and not amt_usd.strip():
                if _looks_like_tx(line):
                    unparsed.append(line.strip())
                continue
            # Local-currency-only rows are not imported.
            if not amt_usd.strip():
                continue

            usd = _clean_amount(amt_usd)
            is_cr = usd < 0

            # Skip pure bonification/interest lines
            desc_clean = desc.strip()
            if any(x in desc_clean.upper() for x in _BONIF_MARKERS):
                transactions.append({
                    "ref":         ref,
                    "fecha":       tx_date,
                    "descripcion": desc_clean,
                    "monto_usd":   usd,
                    "tipo":        "bonificacion",
                    "subtarjeta":  current_subcards,
                })
                continue

            transactions.append({
                "ref":         ref,
                "fecha":       tx_date,
                "descripcion": desc_clean,
                "monto_usd":   usd,
                "tipo":        "credito" if is_cr else "cargo",
                "subtarjeta":  current_subcards,
            })
            continue

        # Cuota lines are parsed by the second pass below.
        if _looks_like_tx(line) and not CUOTA_RE.match(line.strip()):
            unparsed.append(line.strip())

    for line in lines:
        m = CUOTA_RE.match(line.strip())
        if m:
            ref, date_str, desc, cuota_info, amt_str = m.groups()
            month_abbr = date_str[:3]
            day        = int(date_str[4:])
            mo         = _MONTH_MAP.get(month_abbr, 0)
            yr = corte_year if mo <= corte_month + 1 else corte_year - 1
            tx_date = f"{yr:04d}-{mo:02d}-{day:02d}" if mo else ''
            # Avoid duplicates
            if not any(t['ref'] == ref for t in transactions):
                transactions.append({
                    "ref":         ref,
                    "fecha":       tx_date,
                    "descripcion": f"{desc} · {cuota_info}",
                    "monto_usd":   _clean_amount(amt_str),
                    "tipo":        "cuota",
                    "subtarjeta":  current_subcards,
                })

    return {
        "card_name":       card_name,
        "card_key":        card_key,
        "fecha_corte":     fecha_corte,
        "fecha_limite":    fecha_limite,
        "fecha_bonif":     fecha_bonif,
        "saldo_dolares":   saldo_usd,
        "saldo_anterior_usd": saldo_anterior_usd,
        "pago_minimo_usd": pago_min_usd,
        "pago_contado_usd": pago_cont_usd,
        "transactions":    transactions,
        "unparsed":        unparsed,
    }


CUOTA_NUM_RE = re.compile(r'CUOTA:(\d+)/(\d+)')


def _tx_usd(t):
    return t.get('monto_usd') or 0.0


def _find_superseded_cargo_refs(transactions):
    """Single-statement port of identifySupersededCargos() (see
    templates/transactions.html) — when a purchase converts to a "Compras a
    Plazo" installment plan, the bank prints it twice: once as an ordinary cargo
    (the full price) and again as the plan's first cuota. Counting both
    inflates any per-statement total that sums cargo+cuota — including this
    validator — by the full converted purchase price. Returns the set of
    id() for cargo dicts that are the origin of a plan and should be
    excluded from such a sum.
    """
    superseded = set()
    first_cuotas = []
    for t in transactions:
        if t['tipo'] != 'cuota':
            continue
        m = CUOTA_NUM_RE.search(t['descripcion'])
        if m and int(m.group(1)) == 1:
            first_cuotas.append(t)
    if not first_cuotas:
        return superseded

    cuota_pool = [{'expected': _tx_usd(c) * int(CUOTA_NUM_RE.search(c['descripcion']).group(2))}
                  for c in first_cuotas]
    cargo_pool = [t for t in transactions if t['tipo'] == 'cargo']

    remaining = cuota_pool[:]
    while remaining and cargo_pool:
        best_i, best_cargo, best_diff = -1, None, 0.10
        for i, cu in enumerate(remaining):
            for c in cargo_pool:
                diff = abs(_tx_usd(c) - cu['expected'])
                if diff < best_diff:
                    best_diff, best_i, best_cargo = diff, i, c
        if best_cargo is None:
            break
        superseded.add(id(best_cargo))
        cargo_pool.remove(best_cargo)
        remaining.pop(best_i)
    return superseded


def validate_statement(parsed, tolerance=1.00):
    """Cross-check parsed['transactions'] against the statement's own printed
    "Pago de Contado" total — sum of this cycle's charges (cargo + cuota,
    with installment-plan origin cargos excluded — see
    _find_superseded_cargo_refs, same dedup as everywhere else in the app)
    compared to the printed pago_contado, in dollars.

    Note this is only a good proxy when the card had no prior balance
    carried in and no mid-cycle payment — "Pago de Contado" is actually a
    running balance (prior balance + new charges − payments). Folding
    "Saldo Anterior" (see parse_statement) into this formula fixes the
    handful of statements that carry a balance but makes the large majority
    that pass today fail, because a statement's payments this cycle
    usually pay off more than just that one prior-balance figure in a way
    this simple model can't separate. So this check deliberately stays
    balance-agnostic — a real, non-trivial gap here reliably means either a
    genuine parsing gap (investigate) or a statement that carried a balance
    or received a mid-cycle payment (expected, not a bug — surfaced for
    review either way, not proof of one).
    Returns None if pago_contado wasn't found at all (nothing to check against).
    """
    if not parsed.get('pago_contado_usd'):
        return None

    superseded = _find_superseded_cargo_refs(parsed['transactions'])

    def is_counted(t):
        if t['tipo'] == 'cuota':
            return True
        if t['tipo'] == 'cargo':
            return id(t) not in superseded
        if t['tipo'] == 'credito':
            # A purchase-linked refund/rebate/discount (e.g. "REEMBOLSO
            # EXAMPLE", "5% DESCUENTO EXAMPLE") nets against this cycle's
            # charges and belongs in the sum — the bank prints its amount
            # already negative. Two kinds are excluded instead:
            # - A balance payment ("SU PAGO RECIBIDO GRACIAS") is not a
            #   charge reversal, it's money applied against the prior
            #   balance this formula doesn't model (see the docstring above).
            # - "CR COMPRA PLZ ..." is the bank's own reversal notation for the
            #   exact moment a purchase converts to a Compras a Plazo
            #   installment plan — the same conversion _find_superseded_cargo_refs
            #   already excludes by dropping the origin cargo. Counting this
            #   credit too would subtract that purchase a second time.
            desc = t['descripcion'].upper()
            return 'PAGO RECIBIDO' not in desc and not desc.startswith('CR COMPRA PLZ')
        return False

    computed_usd = round(sum(t['monto_usd'] for t in parsed['transactions'] if is_counted(t)), 2)
    diff_usd = round(computed_usd - parsed['pago_contado_usd'], 2)

    return {
        'computed_usd': computed_usd, 'printed_usd': parsed['pago_contado_usd'], 'diff_usd': diff_usd,
        'ok': abs(diff_usd) <= tolerance,
    }
