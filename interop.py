"""JSON export/import compatible with the Android app's (Budget_APK Backup.kt)
export. Pure: no Flask, no file I/O.

APK file vs web data (all normalized on import):
  - ids are numbers                -> str()
  - subtarjeta "0000"              -> "*0000" ("principal" stays)
  - statement / debit upload ids are re-assigned by the phone on every
    import                         -> a statement matching a stored one by
    (card_key, fecha_corte), or an upload matching by (filename, fecha_desde,
    fecha_hasta), keeps the stored id (upload_id references follow)
  - statements lack saldo_anterior_*, validation, unparsed and uploads lack
    resumen_total, warnings        -> copied from that same stored match
  - `_local` (bill_payments, category_budgets_cents, sms_alerts) is stored
    as-is under `_local`; the web app reads/writes `bill_payments` (/bills)
    and `category_budgets_cents` (/category-budgets), same shapes.
Amounts may be JSON numbers or numeric strings; strings become floats
rounded to 2 dp. Top-level keys this app doesn't know are ignored.

Imported text reaches innerHTML/onclick/style sinks unescaped in places, so
ids, dates, `tipo` and category colors are held to strict patterns here.
"""
import copy
import math
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Replaced wholesale when present in the file, kept as-is when absent.
SECTIONS = ('accounts', 'credit_cards', 'installments', 'one_time', 'reserves', 'salary',
            'history', 'statements', 'debit_account', 'credit_cards_saved_at')
PHASE0_STATEMENT_FIELDS = ('saldo_anterior_usd', 'validation', 'unparsed')
PHASE0_UPLOAD_FIELDS = ('resumen_total', 'warnings')
MAX_AMOUNT = 1e12
MAX_DEPTH = 16  # real data nests 8 deep; deeper input would only exhaust recursion

# Static (translatable) messages; the offending JSON path travels separately.
MSG_ROOT = 'El archivo debe ser un objeto JSON.'
MSG_NO_SECTIONS = 'El archivo no contiene ninguna sección reconocida.'
MSG_DEPTH = 'El archivo tiene demasiados niveles de anidación.'
MSG_MISSING = 'Falta un campo requerido:'
MSG_AMOUNT = 'Importe inválido en'
MSG_LIST = 'Se esperaba una lista en'
MSG_OBJECT = 'Se esperaba un objeto en'
MSG_TEXT = 'Se esperaba un texto en'
MSG_INT = 'Se esperaba un número entero en'
MSG_CENTS = 'Meta en centavos inválida en'
CATEGORY_BUDGET_MAX_CENTS = 100_000_000  # $1,000,000; same bound as PUT /api/category-budgets
MSG_ID = 'Identificador inválido en'
MSG_DATE = 'Fecha inválida en'
MSG_COLOR = 'Color inválido en'
MSG_VALUE = 'Valor no permitido en'
MSG_DUP = 'Duplicado en'

_NUM_RE = re.compile(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\Z', re.ASCII)
_CENT = Decimal('0.01')
# parser.py stores '*' + 4 digits, or 'principal'; the phone strips the '*'.
_SUBCARD_DIGITS = re.compile(r'\d+\Z', re.ASCII)
_SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z', re.ASCII)
_ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z', re.ASCII)
_YEAR_MONTH = re.compile(r'\d{4}-(0[1-9]|1[0-2])\Z', re.ASCII)
_HEX_COLOR = re.compile(r'#[0-9a-fA-F]{6}\Z', re.ASCII)
_TIPO = re.compile(r'[a-z_]{1,32}\Z', re.ASCII)


class ImportError_(ValueError):
    def __init__(self, message, path=''):
        super().__init__(f'{message} {path}'.strip())
        self.message = message
        self.path = path


# ── Schema ────────────────────────────────────────────────────────────────
# Scalar kinds: M money, I int, S text, S! non-empty text, ID safe id
# (str|int -> str), D ISO date, D0 ISO date or '', TIPO, HEX color, O object,
# L list, SL list of text. A trailing '?' = may be absent or null.
# ('list', spec) / ('map', spec) = list / safe-id-keyed object of records;
# a plain dict = nested record. Fields not listed pass through unchecked.

_TX = {'fecha': 'D', 'descripcion': 'S', 'tipo': 'TIPO', 'ref': 'S?', 'subtarjeta': 'S?',
       'monto_usd': 'M'}
_STATEMENT = {
    'id': 'ID', 'fecha_corte': 'D', 'fecha_limite': 'D?', 'fecha_bonif': 'D?',
    'saldo_dolares': 'M?', 'pago_minimo_usd': 'M?',
    'pago_contado_usd': 'M?', 'saldo_anterior_usd': 'M?',
    'validation': 'O?', 'unparsed': 'SL?', 'raw_markdown': 'S?', 'source_type': 'S?',
    'transactions': ('list', _TX),
}
_DEBIT_TX = {'fecha': 'D', 'referencia': 'S', 'codigo': 'S', 'descripcion': 'S',
             'debito': 'M', 'credito': 'M', 'balance': 'M', 'upload_id': 'ID', 'orden': 'I'}
# The phone's placeholder upload (transactions without uploads) has '' dates.
_UPLOAD = {'id': 'ID', 'filename': 'S', 'fecha_snapshot': 'D0?', 'fecha_desde': 'D0',
           'fecha_hasta': 'D0', 'saldo_inicial': 'M', 'saldo_final': 'M',
           'saldo_disponible': 'M', 'tx_count': 'I', 'new_tx_count': 'I',
           'resumen_total': 'O?', 'warnings': 'L?', 'raw_csv': 'S?'}

_BUDGET = {
    'accounts': ('list', {'id': 'ID', 'disponible': 'M'}),
    'credit_cards': ('list', {'id': 'ID', 'dollars': 'M'}),
    'installments': ('list', {'id': 'ID', 'total': 'M', 'monthly': 'M',
                              'months_total': 'I', 'months_paid': 'I'}),
    'one_time': ('list', {'id': 'ID', 'amount': 'M'}),
    'reserves': ('list', {'id': 'ID', 'amount': 'M'}),
    'salary': {'gross': 'M', 'deductions': 'M', 'net': 'M'},
    'history': ('list', {'id': 'ID', 'date': 'D', 'total': 'M', 'delta': 'M?', 'pct': 'M?'}),
    'statements': ('map', {'card_name': 'S', 'card_key': 'ID?',
                           'statements': ('list', _STATEMENT)}),
    'debit_account': {'numero_cliente': 'S?', 'cuenta': 'S?', 'nombre': 'S?', 'moneda': 'S?',
                      '_next_seq': 'I?', 'transactions': ('list', _DEBIT_TX),
                      'uploads': ('list', _UPLOAD)},
    'credit_cards_saved_at': 'S?',
}
_CATEGORY = {'label': 'S', 'color': 'HEX?', 'keywords': 'SL'}
_SMS = {'body_hash': 'S!', 'monto_usd': 'M'}


def _money(v, path):
    """The value as stored: numbers unchanged, numeric strings -> 2-dp float."""
    if isinstance(v, bool):
        raise ImportError_(MSG_AMOUNT, path)
    if isinstance(v, str):
        s = v.strip()
        if not _NUM_RE.match(s):
            raise ImportError_(MSG_AMOUNT, path)
        try:
            v = float(Decimal(s).quantize(_CENT, rounding=ROUND_HALF_UP))
        except InvalidOperation:  # too many digits to quantize (e.g. '1e400')
            raise ImportError_(MSG_AMOUNT, path) from None
    elif not isinstance(v, (int, float)):
        raise ImportError_(MSG_AMOUNT, path)
    # abs() of an int is exact, so a 400-digit int can't overflow here.
    if (isinstance(v, float) and not math.isfinite(v)) or abs(v) > MAX_AMOUNT:
        raise ImportError_(MSG_AMOUNT, path)
    return v


def _scalar(v, kind, path):
    if kind == 'M':
        return _money(v, path)
    if kind == 'I':
        if isinstance(v, bool) or not isinstance(v, int):
            raise ImportError_(MSG_INT, path)
    elif kind in ('S', 'S!'):
        if not isinstance(v, str) or (kind == 'S!' and not v):
            raise ImportError_(MSG_TEXT, path)
    elif kind == 'ID':
        if isinstance(v, bool) or not isinstance(v, (str, int)) or not _SAFE_ID.match(str(v)):
            raise ImportError_(MSG_ID, path)
        return str(v)
    elif kind in ('D', 'D0'):
        if not isinstance(v, str) or not (_ISO_DATE.match(v) or (kind == 'D0' and v == '')):
            raise ImportError_(MSG_DATE, path)
    elif kind == 'TIPO':
        if not isinstance(v, str) or not _TIPO.match(v):
            raise ImportError_(MSG_VALUE, path)
    elif kind == 'HEX':
        if not isinstance(v, str) or not _HEX_COLOR.match(v):
            raise ImportError_(MSG_COLOR, path)
    elif kind == 'O':
        if not isinstance(v, dict):
            raise ImportError_(MSG_OBJECT, path)
    elif kind == 'L':
        if not isinstance(v, list):
            raise ImportError_(MSG_LIST, path)
    elif kind == 'SL':
        if not isinstance(v, list):
            raise ImportError_(MSG_LIST, path)
        for i, s in enumerate(v):
            if not isinstance(s, str):
                raise ImportError_(MSG_TEXT, f'{path}[{i}]')
    return v


def _check(v, spec, path):
    """Validate `v` against `spec`; return it normalized (records are
    normalized in place, so pass a copy)."""
    if isinstance(spec, str):
        return _scalar(v, spec, path)
    if isinstance(spec, tuple):
        how, item = spec
        if how == 'list':
            if not isinstance(v, list):
                raise ImportError_(MSG_LIST, path)
            for i, x in enumerate(v):
                v[i] = _check(x, item, f'{path}[{i}]')
        else:
            if not isinstance(v, dict):
                raise ImportError_(MSG_OBJECT, path)
            for k in v:
                if not _SAFE_ID.match(k):
                    raise ImportError_(MSG_ID, f'{path}.{k}')
                v[k] = _check(v[k], item, f'{path}.{k}')
        return v
    if not isinstance(v, dict):
        raise ImportError_(MSG_OBJECT, path)
    for key, kind in spec.items():
        p = f'{path}.{key}' if path else key
        optional = isinstance(kind, str) and kind.endswith('?')
        if key not in v or v[key] is None:
            if optional:
                continue
            raise ImportError_(MSG_MISSING, p)
        v[key] = _check(v[key], kind.rstrip('?') if optional else kind, p)
    return v


def _check_depth(obj):
    stack = [(obj, 1)]
    while stack:
        v, depth = stack.pop()
        if isinstance(v, (dict, list)):
            if depth > MAX_DEPTH:
                raise ImportError_(MSG_DEPTH)
            stack.extend((x, depth + 1) for x in (v.values() if isinstance(v, dict) else v))


def _check_unique(obj):
    """Ids the pages look records up by must be unique within the file."""
    def once(items, key, path):
        seen = set()
        for i, x in enumerate(items):
            if x[key] in seen:
                raise ImportError_(MSG_DUP, f'{path}[{i}].{key}')
            seen.add(x[key])

    for sec in ('accounts', 'credit_cards', 'installments', 'one_time', 'reserves', 'history'):
        if sec in obj:
            once(obj[sec], 'id', sec)
    if 'statements' in obj:
        ids = set()
        for ck, card in obj['statements'].items():
            once(card['statements'], 'fecha_corte', f'statements.{ck}.statements')
            for i, st in enumerate(card['statements']):
                if st['id'] in ids:  # /transactions/<id> is looked up across cards
                    raise ImportError_(MSG_DUP, f'statements.{ck}.statements[{i}].id')
                ids.add(st['id'])
    if 'debit_account' in obj:
        once(obj['debit_account']['uploads'], 'id', 'debit_account.uploads')


def _check_local(local):
    if not isinstance(local, dict):
        raise ImportError_(MSG_OBJECT, '_local')
    if 'bill_payments' in local:
        # The APK's BillPaymentEntity: category_key, "YYYY-MM" month, paid (defaults true).
        rows = _check(local['bill_payments'], ('list', {'category_key': 'ID', 'month': 'S'}),
                      '_local.bill_payments')
        for i, r in enumerate(rows):
            if not _YEAR_MONTH.match(r['month']):
                raise ImportError_(MSG_DATE, f'_local.bill_payments[{i}].month')
            if r.get('paid') is not None and not isinstance(r['paid'], bool):
                raise ImportError_(MSG_VALUE, f'_local.bill_payments[{i}].paid')
    if 'category_budgets_cents' in local:
        budgets = local['category_budgets_cents']
        if not isinstance(budgets, dict):
            raise ImportError_(MSG_OBJECT, '_local.category_budgets_cents')
        for k, cents in budgets.items():
            if k == '_uncat' or isinstance(cents, bool) or not isinstance(cents, int)                     or not 0 < cents <= CATEGORY_BUDGET_MAX_CENTS:
                raise ImportError_(MSG_CENTS, f'_local.category_budgets_cents.{k}')
    if 'sms_alerts' in local:
        _check(local['sms_alerts'], ('list', _SMS), '_local.sms_alerts')


def _normalized(obj):
    """Validated, normalized deep copy of an import file."""
    if not isinstance(obj, dict):
        raise ImportError_(MSG_ROOT)
    if not any(k in obj for k in SECTIONS + ('categories', '_local')):
        raise ImportError_(MSG_NO_SECTIONS)
    _check_depth(obj)
    obj = copy.deepcopy(obj)
    _check(obj, {k: v for k, v in _BUDGET.items() if k in obj}, '')
    _check_unique(obj)
    if 'categories' in obj:
        _check(obj['categories'], ('map', _CATEGORY), 'categories')
    if '_local' in obj:
        _check_local(obj['_local'])
    return obj


def validate_import(obj):
    """Raise ImportError_ (message + JSON path) on the first problem anywhere
    in the file; the whole file is checked before anything is written."""
    _normalized(obj)


def export_bundle(budget, categories):
    """Everything stored, plus `categories`. `_local` only when something is
    stored there: an empty `bill_payments` would wipe the phone's Pagos."""
    out = {k: v for k, v in budget.items() if k != '_local'}
    if budget.get('_local'):
        out['_local'] = budget['_local']
    out['categories'] = categories
    return out


def _counts(budget):
    stmts = [s for card in (budget.get('statements') or {}).values()
             for s in card.get('statements', [])]
    return {
        "statements": len(stmts),
        "statement_tx": sum(len(s.get('transactions', [])) for s in stmts),
        "debit_tx": len((budget.get('debit_account') or {}).get('transactions', [])),
        "history": len(budget.get('history') or []),
    }


def _merge_with_stored(records, stored, key, fields):
    """Give each record matching a stored one (by `key`) the stored id and
    any of `fields` it lacks; a non-matching record whose id is then taken
    gets a fresh one. Returns {file id: final id}."""
    matched, rest, taken = [], [], set()
    for r in records:
        m = stored.get(key(r))
        (matched if m else rest).append((r, m))
    remap = {}
    for r, m in matched:
        remap[r['id']] = m['id']
        r['id'] = m['id']
        taken.add(r['id'])
        for f in fields:
            if f not in r and f in m:
                r[f] = copy.deepcopy(m[f])
    for r, _ in rest:
        new_id = r['id']
        while new_id in taken:
            new_id = uuid.uuid4().hex[:8]
        remap[r['id']] = new_id
        r['id'] = new_id
        taken.add(new_id)
    return remap


def apply_import(obj, current_budget, current_categories):
    """(new_budget, new_categories, summary). Pure apart from fresh ids for
    colliding records; validates first. The caller sets `_version` (the
    file's is ignored)."""
    obj = _normalized(obj)
    new = copy.deepcopy(current_budget)
    replaced = [s for s in SECTIONS if s in obj]
    for s in replaced:
        new[s] = obj[s]

    if 'statements' in obj:
        stored = {(ck, s.get('fecha_corte')): s
                  for ck, card in (current_budget.get('statements') or {}).items()
                  for s in card.get('statements', [])}
        pairs = [(ck, st) for ck, card in obj['statements'].items() for st in card['statements']]
        for _, st in pairs:
            for t in st['transactions']:
                sub = t.get('subtarjeta')
                if sub and _SUBCARD_DIGITS.match(sub):
                    t['subtarjeta'] = '*' + sub
        card_of = {id(st): ck for ck, st in pairs}
        _merge_with_stored([st for _, st in pairs], stored,
                           lambda st: (card_of[id(st)], st['fecha_corte']),
                           PHASE0_STATEMENT_FIELDS)

    if 'debit_account' in obj:
        acct = obj['debit_account']
        acct['_next_seq'] = max([acct.get('_next_seq', 0)]
                                + [t['orden'] + 1 for t in acct['transactions']])
        stored = {(u.get('filename'), u.get('fecha_desde'), u.get('fecha_hasta')): u
                  for u in (current_budget.get('debit_account') or {}).get('uploads', [])}
        remap = _merge_with_stored(acct['uploads'], stored,
                                   lambda u: (u['filename'], u['fecha_desde'], u['fecha_hasta']),
                                   PHASE0_UPLOAD_FIELDS)
        for t in acct['transactions']:
            t['upload_id'] = remap.get(t['upload_id'], t['upload_id'])

    new_cats = copy.deepcopy(current_categories)
    file_cats = obj.get('categories', {})
    added = sum(1 for k in file_cats if k not in new_cats)
    new_cats.update(file_cats)

    summary = {"sections": replaced, **_counts(new),
               "categories_updated": len(file_cats) - added, "categories_added": added}

    if '_local' in obj:
        fl = obj['_local']
        local = new.get('_local') or {}
        if 'bill_payments' in fl:
            local['bill_payments'] = fl['bill_payments']
            summary['bill_payments'] = len(fl['bill_payments'])
        if fl.get('category_budgets_cents'):
            local['category_budgets_cents'] = fl['category_budgets_cents']
            summary['category_budgets'] = len(fl['category_budgets_cents'])
        if fl.get('sms_alerts'):
            known = {a.get('body_hash') for a in local.get('sms_alerts', [])}
            fresh = []
            for a in fl['sms_alerts']:
                if a['body_hash'] not in known:
                    known.add(a['body_hash'])
                    fresh.append(a)
            local['sms_alerts'] = local.get('sms_alerts', []) + fresh
            summary['sms_added'] = len(fresh)
        if local:
            new['_local'] = local
    return new, new_cats, summary
