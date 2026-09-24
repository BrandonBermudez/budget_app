from flask import Flask, Response, render_template, request, jsonify, session
from flask_session import Session
from werkzeug.exceptions import HTTPException
from datetime import date, datetime
from dotenv import load_dotenv
import uuid
import os
import json
import re
import secrets
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from parser import parse_statement, convert_pdf_to_markdown, validate_statement, CARDS
from debit_parser import decode_bank_csv, parse_debit_csv, validate_debit_csv, DebitParseError
import backup
import interop

load_dotenv()

app = Flask(__name__)

# ── Secret key — override via SECRET_KEY env var before exposing beyond localhost
_DEV_SECRET_KEY = 'budget-dev-secret-2024'
app.secret_key = os.environ.get('SECRET_KEY') or _DEV_SECRET_KEY  # empty value in .env counts as unset
if app.secret_key == _DEV_SECRET_KEY:
    print('[WARN] Using default SECRET_KEY. Set the SECRET_KEY environment variable '
          'before running this app anywhere beyond your own localhost.')

# ── Server-side session (filesystem) — avoids 4KB cookie overflow ─────────
app.config['SESSION_TYPE']           = 'filesystem'
app.config['SESSION_FILE_DIR']       = os.path.join(os.path.dirname(__file__), '.flask_session')
app.config['SESSION_PERMANENT']      = False
app.config['SESSION_USE_SIGNER']     = True
app.config['SESSION_FILE_THRESHOLD'] = 500
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH']     = 5 * 1024 * 1024  # 5MB upload cap
IMPORT_MAX_BYTES = 25 * 1024 * 1024  # /api/import only: a full export is several MB
os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)
Session(app)


# ── Optional HTTP Basic Auth — only enforced if both env vars are set ─────
_AUTH_USER = os.environ.get('BUDGET_APP_USER')
_AUTH_PASS = os.environ.get('BUDGET_APP_PASS')


@app.before_request
def _require_auth():
    if not _AUTH_USER or not _AUTH_PASS:
        return  # auth not configured — local single-user use, unchanged behavior
    auth = request.authorization
    valid = (
        auth is not None
        and secrets.compare_digest(auth.username or '', _AUTH_USER)
        and secrets.compare_digest(auth.password or '', _AUTH_PASS)
    )
    if not valid:
        return (
            'Authentication required', 401,
            {'WWW-Authenticate': 'Basic realm="Presupuesto"'}
        )


# ── CSRF protection for state-mutating API calls ──────────────────────────

def _get_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)
        session.modified = True
    return session['csrf_token']


@app.before_request
def _csrf_protect():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.path.startswith('/api/') \
            and request.path != '/api/csrf-token':
        token        = session.get('csrf_token')
        header_token = request.headers.get('X-CSRFToken')
        if not token or not header_token or not secrets.compare_digest(token, header_token):
            return jsonify({"error": "Invalid or missing CSRF token"}), 403


@app.route('/api/ping', methods=['GET'])
def api_ping():
    # Lets a second launch detect this instance and reuse it (see __main__).
    return jsonify({"app": "presupuesto"})


@app.route('/api/csrf-token', methods=['GET'])
def api_csrf_token():
    return jsonify({"csrf_token": _get_csrf_token()})


@app.errorhandler(413)
def _file_too_large(e):
    if request.path == '/api/import':
        return jsonify({"error": "El archivo es demasiado grande (máx 25MB)"}), 413
    return jsonify({"error": "El archivo es demasiado grande (máx 5MB)"}), 413


@app.errorhandler(Exception)
def _handle_unexpected_error(e):
    # Keep expected HTTP errors (404, 405, the 413 above, etc.) as-is; only
    # genuinely unhandled exceptions (e.g. a failed disk write) get converted
    # to JSON here, so every /api/* response stays JSON-parseable on the
    # frontend instead of occasionally falling through to Flask's HTML page.
    if isinstance(e, HTTPException):
        # Only API routes should get a JSON body — let browser page routes
        # (/, /transactions, /transactions/<id>) fall through to Flask's normal
        # HTML error page instead of rendering raw JSON in the browser.
        if request.path.startswith('/api/'):
            return jsonify({"error": e.description}), e.code
        return e
    app.logger.exception('Unhandled exception')
    return jsonify({"error": "Error interno del servidor"}), 500


# ── Persistent store — a local JSON file, so data survives across browser
#    sessions and server restarts (no external database needed) ────────────
DATA_DIR        = os.path.join(os.path.dirname(__file__), 'data')
DATA_FILE       = os.path.join(DATA_DIR, 'budget_data.json')
DEFAULTS_FILE   = os.path.join(DATA_DIR, 'defaults.json')
CATEGORIES_FILE = os.path.join(DATA_DIR, 'categories.json')


def _load_persisted():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # Preserve the corrupt file instead of silently discarding it —
            # falling back to defaults would otherwise erase it on the next save.
            try:
                backup_path = DATA_FILE + '.corrupt-' + datetime.now().strftime('%Y%m%d%H%M%S%f')
                os.replace(DATA_FILE, backup_path)
                print(f'[WARN] Corrupt data file could not be read; backed up to {backup_path} '
                      'and falling back to defaults.')
            except OSError:
                pass
    return None


def _save_persisted(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    # A unique temp name (not a fixed DATA_FILE + '.tmp') so two overlapping
    # requests — the dev server is threaded — don't write to the same temp
    # file and clobber each other's partial JSON before the rename.
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DATA_FILE)  # atomic on both POSIX and Windows
    except BaseException:
        # Don't leave the temp file behind if the write/rename fails.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def get_data():
    """Always reads the on-disk file fresh — it is the single source of truth.
    (The Flask session is only used for `csrf_token` and `pending_upload`,
    both genuinely per-browser/ephemeral state, not the budget data itself.)"""
    return _load_persisted() or _default_data()


def set_data(data, current=None, bump_version=True):
    """Persist data and return its version.

    Pass `current` when the caller already has the freshly-loaded blob on hand
    (e.g. it just came from get_data()) to avoid a redundant disk read.

    `_version` is the optimistic-concurrency token the budget dashboard
    (index.html's `D`) tracks and echoes back on every save — it should only
    change when something the dashboard itself could have edited actually
    changed. Statement uploads/deletes touch an unrelated part of the data
    (`statements`) that the dashboard never sends or reads, so they pass
    bump_version=False to avoid invalidating the dashboard's cached version
    (which would otherwise make its very next unrelated save spuriously
    409 as a false "changed elsewhere" conflict).
    """
    # backup.LOCK: a save must not interleave with a restore's two-file write.
    with backup.LOCK:
        if current is None:
            current = _load_persisted()
        current_version = (current or {}).get('_version', 0)
        data['_version'] = current_version + 1 if bump_version else current_version
        _save_persisted(data)
        _auto_backup()
    return data['_version']


def _backup_dir():
    # Looked up at call time so a test harness that redirects DATA_DIR
    # redirects backups too.
    return os.path.join(DATA_DIR, 'backups')


def _auto_backup():
    """Rotating backup of both data files after a save. A backup failure must
    never affect the save itself."""
    try:
        backup.snapshot(DATA_FILE, CATEGORIES_FILE, _backup_dir())
    except Exception:
        app.logger.exception('Automatic backup failed')


def _prerestore_snapshot():
    """Forced, verified copy of the current state before a data-destroying
    operation (restore, reset). Raises on failure — the caller must abort."""
    return backup.snapshot(DATA_FILE, CATEGORIES_FILE, _backup_dir(),
                           kind='prerestore', force=True)


class _ReplaceFailed(Exception):
    """stage: 'snapshot' | 'write' (nothing changed) | 'rollback' (categories
    changed, budget not, rollback failed; `pre` names the copy) | 'partial'
    (no live file: budget written, categories not)."""
    def __init__(self, stage, pre=None):
        super().__init__(stage)
        self.stage = stage
        self.pre = pre


def _replace_all(budget, categories, kind):
    """Replace both data files (restore, import). Caller holds backup.LOCK.
    Returns the name of the forced, verified `kind` snapshot taken first, or
    None when there is no live data file to copy."""
    if not os.path.exists(DATA_FILE):
        # Nothing to copy first (e.g. the file was unreadable and moved aside
        # as .corrupt-*). Budget first, so a failure there changes nothing;
        # each write is atomic, so a categories failure leaves them as-is.
        try:
            _save_persisted(budget)
        except Exception:
            app.logger.exception('%s: writing budget data failed', kind)
            raise _ReplaceFailed('write')
        try:
            _write_categories(categories)
        except Exception:
            app.logger.exception('%s: wrote budget data but not categories', kind)
            raise _ReplaceFailed('partial')
        return None

    # The current state must be safely copied before anything changes.
    try:
        pre = backup.snapshot(DATA_FILE, CATEGORIES_FILE, _backup_dir(), kind=kind, force=True)
    except Exception:
        app.logger.exception('%s backup failed; aborted', kind)
        raise _ReplaceFailed('snapshot')
    _write_categories(categories)
    try:
        _save_persisted(budget)
    except Exception:
        app.logger.exception('%s: writing budget data failed; rolling back categories', kind)
        try:
            _write_categories(backup.load_verified(_backup_dir(), pre)[1])
        except Exception:
            app.logger.critical('ROLLBACK FAILED: categories.json was replaced but '
                                'budget_data.json was not; %s copy: %s', kind, pre, exc_info=True)
            raise _ReplaceFailed('rollback', pre)
        raise _ReplaceFailed('write', pre)
    return pre


def _default_data():
    """Load the seed/default budget data from data/defaults.json.
    Returns a fresh deep copy each time (json.loads on a cached string is a
    cheap, dependency-free way to deep-copy)."""
    with open(DEFAULTS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


# ── Transaction categorization rules — separate file from budget_data.json:
#    it's a set of user-curated keyword→category rules, not budget state, and
#    a budget "Reiniciar" shouldn't wipe out categorization work already done. ──

def _load_categories():
    with open(CATEGORIES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save_categories(categories):
    with backup.LOCK:
        _write_categories(categories)
        _auto_backup()


def _write_categories(categories):
    """Atomic write only — no backup hook (restore uses it directly)."""
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(categories, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CATEGORIES_FILE)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

# ── Page route ────────────────────────────────────────────────────────────

# Debit-side card-payment map for debit.js (last-4 → card), from card_map.json.
CARD_PAYMENT_MAP = {d: {'key': c['key'], 'label': c['name'], 'color': c['color']}
                    for c in CARDS for d in c.get('payment_last4', [])}


# Utility providers (gitignored; see data/utilities.example.json).
try:
    with open(os.path.join(DATA_DIR, 'utilities.json'), encoding='utf-8') as _f:
        UTILITY_PROVIDERS = json.load(_f)['providers']
except FileNotFoundError:
    UTILITY_PROVIDERS = []


@app.context_processor
def _inject_user_config():
    return {'card_payment_map': CARD_PAYMENT_MAP, 'utility_providers': UTILITY_PROVIDERS}

@app.route('/')
def index():
    return render_template('index.html')


# ── API: full data ────────────────────────────────────────────────────────

@app.route('/api/data', methods=['GET'])
def api_get():
    # `statements` (which carries every uploaded statement's full raw_markdown
    # text) is intentionally omitted here — the budget dashboard never reads
    # it, and re-transmitting it on every load/save would grow without bound
    # as statements accumulate. Use /api/statements for that data instead.
    data = dict(get_data())
    data.pop('statements', None)
    return jsonify(data)


@app.route('/api/data', methods=['POST'])
def api_post():
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Invalid JSON"}), 400

    # Reject a stale full-object overwrite — e.g. a browser tab left open
    # since before a Reset (or a save from another tab/session) — instead of
    # silently clobbering whatever is currently persisted.
    current         = _load_persisted()
    current_version = (current or {}).get('_version', 0)
    payload_version = payload.get('_version', 0)
    if current is not None and payload_version != current_version:
        return jsonify({
            "error": "conflict",
            "message": "Los datos cambiaron en otra sesión. Recarga la página antes de guardar."
        }), 409

    # The client (budget dashboard) never receives/carries `statements` — see
    # api_get() above — so preserve whatever is currently persisted for it
    # rather than letting its absence (or an explicit null/{}) wipe it out.
    if not payload.get('statements') and current and 'statements' in current:
        payload['statements'] = current['statements']
    # Same for `_local` (device-local state stored by the Android-app import).
    if '_local' not in payload and current and '_local' in current:
        payload['_local'] = current['_local']

    new_version = set_data(payload, current=current)
    return jsonify({"ok": True, "version": new_version})


# ── API: snapshot ─────────────────────────────────────────────────────────

@app.route('/api/snapshot', methods=['POST'])
def api_snapshot():
    data = get_data()
    body = request.get_json(silent=True) or {}

    # Prefer client-supplied total (avoids stale-session race on persist).
    # Fall back to server-side calculation if not provided.
    client_total = body.get('total')
    if client_total is not None:
        total = round(float(client_total), 2)
    else:
        total = round(sum(a['disponible'] for a in data.get('accounts', [])), 2)

    # Compute delta/pct against the last history entry
    history = data.get('history', [])
    prev_total = history[-1]['total'] if history else None
    if prev_total is not None and prev_total != 0:
        delta = round(total - prev_total, 2)
        pct   = round((delta / prev_total) * 100, 2)
    else:
        delta = None
        pct   = None

    entry = {
        "id":    str(uuid.uuid4())[:8],
        "date":  date.today().isoformat(),
        "total": total,
        "delta": delta,
        "pct":   pct,
        "note":  body.get("note", "").strip()
    }

    data.setdefault('history', []).append(entry)
    new_version = set_data(data, current=data)
    return jsonify({"ok": True, "entry": entry, "version": new_version})


# ── API: reset to defaults ────────────────────────────────────────────────

@app.route('/api/reset', methods=['POST'])
def api_reset():
    with backup.LOCK:
        # Reset destroys data: keep a verified copy first, or don't reset.
        # (No live file = nothing to lose.)
        if os.path.exists(DATA_FILE):
            try:
                _prerestore_snapshot()
            except Exception:
                app.logger.exception('Pre-reset backup failed; reset aborted')
                return jsonify({"error": "No se pudo respaldar los datos actuales; "
                                         "no se reinició nada."}), 500
        # The Android app's reset rule: SMS alerts and category budgets are
        # user-curated and survive; bill payment ticks are budget state.
        data = _default_data()
        local = {k: v for k, v in ((_load_persisted() or {}).get('_local') or {}).items()
                 if k != 'bill_payments'}
        if local:
            data['_local'] = local
        set_data(data)
    _drop_pending_uploads()
    return jsonify({"ok": True})


# ── API: backups (list + restore) ─────────────────────────────────────────

@app.route('/api/backups', methods=['GET'])
def api_backups():
    # Metadata only — never the backed-up data itself.
    return jsonify({"backups": backup.list_backups(_backup_dir()),
                    "version": (_load_persisted() or {}).get('_version', 0)})


@app.route('/api/backups/restore', methods=['POST'])
def api_backup_restore():
    payload = request.get_json(silent=True) or {}
    bdir = _backup_dir()
    with backup.LOCK:
        current = _load_persisted()
        if current is not None and payload.get('_version') != current.get('_version', 0):
            return jsonify({
                "error": "conflict",
                "message": "Los datos cambiaron en otra sesión. Recarga la página antes de guardar."
            }), 409

        name = payload.get('name')
        try:
            listed = os.listdir(bdir)
        except OSError:
            listed = []
        if not isinstance(name, str) or backup.parse_name(name) is None or name not in listed:
            return jsonify({"error": "Respaldo no encontrado"}), 404

        try:
            budget, cats = backup.load_verified(bdir, name)
        except (backup.BackupError, OSError):
            app.logger.exception('Backup %s failed verification', name)
            return jsonify({"error": "El respaldo está dañado o no se pudo leer; "
                                     "no se cambió nada."}), 400

        # Always a new version, so any open tab's next save gets a 409.
        # With no live file, restore is exactly the recovery path.
        budget['_version'] = (current or {}).get('_version', 0) + 1
        try:
            pre = _replace_all(budget, cats, 'prerestore')
        except _ReplaceFailed as e:
            if e.stage == 'partial':
                _drop_pending_uploads()
            body = {"error": _RESTORE_ERRORS[e.stage]}
            if e.stage == 'rollback':
                body['prerestore'] = e.pre
            return jsonify(body), 500
        _auto_backup()

    _drop_pending_uploads()
    return jsonify({"ok": True, "version": budget['_version'], "prerestore": pre})


_RESTORE_ERRORS = {
    'snapshot': "No se pudo respaldar los datos actuales; no se restauró nada.",
    'write':    "La restauración falló; no se cambió nada.",
    'rollback': "La restauración falló y no se pudo revertir.",
    'partial':  "Se restauraron los datos, pero no las categorías.",
}
_IMPORT_ERRORS = {
    'snapshot': "No se pudo respaldar los datos actuales; no se importó nada.",
    'write':    "La importación falló; no se cambió nada.",
    'rollback': "La importación falló y no se pudo revertir.",
    'partial':  "Se importaron los datos, pero no las categorías.",
}


def _drop_pending_uploads():
    for key in ('pending_upload', 'pending_debit_upload'):
        session.pop(key, None)


# ── API: JSON export / import (Android-app compatible, see interop.py) ────

@app.route('/api/export', methods=['GET'])
def api_export():
    with backup.LOCK:  # a consistent pair, never mid-restore/import
        bundle = interop.export_bundle(get_data(), _load_categories())
    return Response(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        mimetype='application/json',
        headers={
            'Content-Disposition':
                f'attachment; filename="presupuesto-export-{date.today():%Y%m%d}.json"',
            'Cache-Control': 'no-store',
        })


@app.route('/api/import', methods=['POST'])
def api_import():
    # Before anything touches request.form/files, which parse the body.
    request.max_content_length = IMPORT_MAX_BYTES
    f = request.files.get('file')
    sent_version = request.form.get('_version', '')
    with backup.LOCK:
        current = _load_persisted()
        if current is not None and sent_version != str(current.get('_version', 0)):
            return jsonify({
                "error": "conflict",
                "message": "Los datos cambiaron en otra sesión. Recarga la página antes de guardar."
            }), 409
        if f is None:
            return jsonify({"error": "No se envió ningún archivo."}), 400
        try:
            obj = json.loads(f.read().decode('utf-8-sig'))
        except (UnicodeDecodeError, ValueError, RecursionError):  # deep nesting -> RecursionError
            return jsonify({"error": "El archivo no es JSON válido."}), 400
        try:
            budget, cats, summary = interop.apply_import(
                obj, current or _default_data(), _load_categories())
        except interop.ImportError_ as e:
            return jsonify({"error": e.message, "path": e.path}), 400

        budget['_version'] = (current or {}).get('_version', 0) + 1
        try:
            pre = _replace_all(budget, cats, 'preimport')
        except _ReplaceFailed as e:
            if e.stage == 'partial':
                _drop_pending_uploads()
            body = {"error": _IMPORT_ERRORS[e.stage]}
            if e.stage == 'rollback':
                body['preimport'] = e.pre
            return jsonify(body), 500
        _auto_backup()

    _drop_pending_uploads()
    return jsonify({"ok": True, "version": budget['_version'], "preimport": pre,
                    "summary": summary})


# ── Transactions page routes ──────────────────────────────────────────────

@app.route('/transactions')
def transactions_page():
    return render_template('transactions.html')


@app.route('/transactions/<statement_id>')
def statement_detail_page(statement_id):
    return render_template('statement_detail.html', statement_id=statement_id)


# ── Debit account page route ───────────────────────────────────────────────

@app.route('/debit-account')
def debit_account_page():
    return render_template('debit_account.html')


@app.route('/cash-flow')
def cash_flow_page():
    return render_template('cash_flow.html')


@app.route('/bills')
def bills_page():
    return render_template('bills.html')


@app.route('/category-budgets')
def category_budgets_page():
    return render_template('category_budgets.html')


@app.route('/projection')
def projection_page():
    return render_template('projection.html')


# ── API: get all statements (list view — no raw_markdown) ─────────────────

@app.route('/api/statements', methods=['GET'])
def api_get_statements():
    data = get_data()
    statements = data.get('statements', {})
    # The card list/tab view never needs the full converted-document text —
    # only the single-statement endpoint below does. Strip it here so
    # browsing the statement list doesn't download every statement's full
    # raw_markdown blob just to show counts and totals.
    trimmed = {}
    for key, card in statements.items():
        trimmed[key] = {
            **card,
            "statements": [
                {k: v for k, v in s.items() if k != 'raw_markdown'}
                for s in card['statements']
            ],
        }
    return jsonify(trimmed)


# ── API: validate every stored statement against its own printed total ────
# See parser.validate_statement's docstring for exactly what this does and
# does not prove — it's a "worth a look" flag, not an error state.

@app.route('/api/validate-statements', methods=['GET'])
def api_validate_statements():
    data = get_data()
    flagged = []
    for card_key, card in data.get('statements', {}).items():
        for st in card['statements']:
            raw = st.get('raw_markdown')
            if not raw:
                continue
            reparsed = parse_statement(raw)
            v = validate_statement(reparsed)
            if v is None or v['ok']:
                continue
            flagged.append({
                "card_key": card_key, "card_name": card['card_name'],
                "statement_id": st['id'], "fecha_corte": st['fecha_corte'],
                **{k: v[k] for k in ('computed_usd', 'printed_usd', 'diff_usd')},
            })
    flagged.sort(key=lambda f: -abs(f['diff_usd']))
    return jsonify({"flagged": flagged})


# ── API: get a single statement (includes raw_markdown) ───────────────────

@app.route('/api/statements/<card_key>/<statement_id>', methods=['GET'])
def api_get_single_statement(card_key, statement_id):
    data = get_data()
    card = data.get('statements', {}).get(card_key)
    if not card:
        return jsonify({"error": "Unknown card_key"}), 404
    stmt = next((s for s in card['statements'] if s['id'] == statement_id), None)
    if not stmt:
        return jsonify({"error": "Statement not found"}), 404
    return jsonify({**stmt, "card_name": card['card_name'], "card_key": card_key})


# ── API: find a single statement by id alone (statement_detail.html only
#    knows the id from the URL, not which card it belongs to) ─────────────

@app.route('/api/statement/<statement_id>', methods=['GET'])
def api_get_statement_by_id(statement_id):
    data = get_data()
    for card_key, card in data.get('statements', {}).items():
        stmt = next((s for s in card['statements'] if s['id'] == statement_id), None)
        if stmt:
            return jsonify({**stmt, "card_name": card['card_name'], "card_key": card_key})
    return jsonify({"error": "Statement not found"}), 404


# ── API: upload & parse statement (preview only — not persisted yet) ──────

@app.route('/api/upload-statement', methods=['POST'])
def api_upload_statement():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files['file']
    is_pdf = (f.filename or '').lower().endswith('.pdf')

    if is_pdf:
        try:
            content = convert_pdf_to_markdown(f)
        except Exception as e:
            return jsonify({"error": f"Error al convertir PDF: {str(e)}"}), 422
    else:
        content = f.read().decode('utf-8', errors='replace')

    try:
        parsed = parse_statement(content)
    except Exception as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 422

    if not parsed['fecha_corte']:
        return jsonify({
            "error": "Formato de estado de cuenta no reconocido. Esta app es una plantilla que solo lee el formato de un banco; para usar el tuyo hay que escribir un parser (ver README, «Adding your bank»)."
        }), 422

    # Stash the parsed result in the session — nothing is written to the
    # persisted store until the user explicitly confirms via /api/confirm-statement.
    # A pending_id ties a specific upload to its confirm/cancel call, so a second
    # upload (e.g. from another browser tab sharing the same session cookie)
    # can't silently get confirmed/canceled by a stale first tab's buttons.
    pending_id = str(uuid.uuid4())
    session['pending_upload'] = {
        "pending_id":  pending_id,
        "parsed":      parsed,
        "raw_markdown": content,
        "source_type": 'pdf' if is_pdf else 'markdown',
    }
    session.modified = True

    data = get_data()
    return jsonify({
        "ok":           True,
        "pending_id":   pending_id,
        "card_key":     parsed['card_key'],
        "card_name":    parsed['card_name'],
        "fecha_corte":  parsed['fecha_corte'],
        "tx_count":     len(parsed['transactions']),
        "unparsed_count": len(parsed.get('unparsed', [])),
        "validation":   validate_statement(parsed),
        "replaces_existing": _replaces_existing(data, parsed),
    })


# ── Shared: insert/replace one parsed statement into `data['statements']` ──

def _replaces_existing(data, parsed):
    """True when this card already has a statement with the same fecha_corte
    (saving would replace it)."""
    card = data.get('statements', {}).get(parsed['card_key'])
    return bool(card) and any(s['fecha_corte'] == parsed['fecha_corte'] for s in card['statements'])


def _blanks_existing(data, parsed):
    """An empty parse that would replace a stored statement — saving it would
    delete a good statement's transactions, so it is refused (APK rule)."""
    return not parsed['transactions'] and _replaces_existing(data, parsed)


def _upsert_statement(data, parsed, raw_markdown, source_type):
    """Insert (or replace, by fecha_corte) one parsed statement. Returns
    (card_key, card_name, already_existed)."""
    already_existed = _replaces_existing(data, parsed)
    statements = data.setdefault('statements', {})
    card_key   = parsed['card_key']
    card_name  = parsed['card_name']

    if card_key not in statements:
        statements[card_key] = {
            "card_name": card_name,
            "card_key":  card_key,
            "statements": []
        }

    # Deduplicate: if a statement with the same fecha_corte already exists, replace it
    existing = [s for s in statements[card_key]['statements'] if s['fecha_corte'] != parsed['fecha_corte']]
    existing.append({
        "id":             str(uuid.uuid4())[:8],
        "fecha_corte":    parsed['fecha_corte'],
        "fecha_limite":   parsed['fecha_limite'],
        "fecha_bonif":    parsed['fecha_bonif'],
        "saldo_dolares":  parsed['saldo_dolares'],
        "pago_minimo_usd": parsed['pago_minimo_usd'],
        "pago_contado_usd": parsed['pago_contado_usd'],
        "saldo_anterior_usd": parsed.get('saldo_anterior_usd', 0.0),
        # Snapshot at import time (None = no printed Pago de Contado to check
        # against); /api/validate-statements still re-derives it live.
        "validation":     validate_statement(parsed),
        "unparsed":       parsed.get('unparsed', []),
        "transactions":   parsed['transactions'],
        "raw_markdown":   raw_markdown,
        "source_type":    source_type,
    })
    # Keep sorted by fecha_corte descending
    existing.sort(key=lambda s: s['fecha_corte'], reverse=True)
    statements[card_key]['statements'] = existing

    return card_key, card_name, already_existed


# ── API: confirm a pending upload — actually persists it ──────────────────

@app.route('/api/confirm-statement', methods=['POST'])
def api_confirm_statement():
    body       = request.get_json(silent=True) or {}
    pending_id = body.get('pending_id')
    pending    = session.get('pending_upload')

    if not pending:
        return jsonify({"error": "No hay ningún estado de cuenta pendiente de confirmar"}), 400
    # Require a matching pending_id unconditionally — an omitted pending_id
    # used to silently skip this check and confirm whatever happened to be
    # pending, rather than the specific upload the caller meant to confirm.
    if pending.get('pending_id') != pending_id:
        return jsonify({
            "error": "El estado de cuenta pendiente cambió — vuelve a subir el archivo."
        }), 409

    parsed  = pending['parsed']
    content = pending['raw_markdown']
    is_pdf  = pending['source_type'] == 'pdf'

    data = get_data()
    if _blanks_existing(data, parsed):
        return jsonify({
            "error": "No se encontró ninguna transacción; no se reemplazará el estado de cuenta existente con uno vacío."
        }), 409
    card_key, card_name, _ = _upsert_statement(
        data, parsed, content, 'pdf' if is_pdf else 'markdown'
    )

    # Statements are independent of the fields the budget dashboard tracks —
    # don't bump the shared _version, or its next unrelated save would
    # spuriously 409 as a false "changed elsewhere" conflict.
    set_data(data, current=data, bump_version=False)
    session.pop('pending_upload', None)
    return jsonify({
        "ok":           True,
        "card_key":     card_key,
        "card_name":    card_name,
        "fecha_corte":  parsed['fecha_corte'],
        "tx_count":     len(parsed['transactions']),
    })


# ── API: search email for bank statements and import all found ────────────

@app.route('/api/fetch-email-statements', methods=['POST'])
def api_fetch_email_statements():
    try:
        from email_fetcher import fetch_statements
    except Exception as e:
        return jsonify({"error": f"No se pudo cargar el módulo de correo: {str(e)}"}), 500

    try:
        email_results = fetch_statements()
    except RuntimeError as e:
        # RuntimeError is only raised locally (missing .env credentials) —
        # its message never comes from the IMAP server, safe to relay as-is.
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        # Other exceptions can carry raw IMAP server responses — log the detail
        # server-side and keep the client-facing message generic.
        app.logger.exception('Email fetch failed')
        return jsonify({"error": "Error al buscar en el correo. Revisa la conexión o inténtalo más tarde."}), 502

    data = get_data()
    imported = []
    failed   = []

    for item in email_results:
        try:
            parsed = parse_statement(item['markdown'])
        except Exception as e:
            failed.append({"filename": item['filename'], "reason": f"Error de parseo: {str(e)}"})
            continue

        if not parsed['fecha_corte']:
            failed.append({
                "filename": item['filename'],
                "reason": "No se pudo detectar la fecha de corte"
            })
            continue

        if _blanks_existing(data, parsed):
            failed.append({
                "filename": item['filename'],
                "reason": "Sin transacciones; no se reemplazó el estado de cuenta existente"
            })
            continue

        card_key, card_name, already_existed = _upsert_statement(
            data, parsed, item['markdown'], 'email'
        )
        imported.append({
            "card_name":   card_name,
            "card_key":    card_key,
            "fecha_corte": parsed['fecha_corte'],
            "tx_count":    len(parsed['transactions']),
            "replaced":    already_existed,
        })

    if imported:
        # Same reasoning as api_confirm_statement — don't invalidate the
        # budget dashboard's cached version for a change to `statements`.
        set_data(data, current=data, bump_version=False)

    return jsonify({
        "ok":       True,
        "found":    len(email_results),
        "imported": imported,
        "failed":   failed,
    })


# ── API: cancel a pending upload ───────────────────────────────────────────

@app.route('/api/cancel-upload', methods=['POST'])
def api_cancel_upload():
    body       = request.get_json(silent=True) or {}
    pending_id = body.get('pending_id')
    pending    = session.get('pending_upload')
    # Only clear it if it matches (or no id was given) — otherwise a stale
    # tab's cancel shouldn't discard a different, newer pending upload.
    if pending and (not pending_id or pending.get('pending_id') == pending_id):
        session.pop('pending_upload', None)
    return jsonify({"ok": True})


# ── API: delete a statement ───────────────────────────────────────────────

@app.route('/api/statements/<card_key>/<statement_id>', methods=['DELETE'])
def api_delete_statement(card_key, statement_id):
    data = get_data()
    statements = data.get('statements', {})
    if card_key not in statements:
        return jsonify({"error": "Unknown card_key"}), 404

    before = len(statements[card_key]['statements'])
    statements[card_key]['statements'] = [
        s for s in statements[card_key]['statements']
        if s['id'] != statement_id
    ]
    if len(statements[card_key]['statements']) == before:
        return jsonify({"error": "Statement not found"}), 404

    if not statements[card_key]['statements']:
        del statements[card_key]
    # Same reasoning as api_confirm_statement — don't invalidate the budget
    # dashboard's cached version for a change to an unrelated part of the data.
    set_data(data, current=data, bump_version=False)
    return jsonify({"ok": True})


# ── API: transaction categorization rules ─────────────────────────────────

@app.route('/api/categories', methods=['GET'])
def api_get_categories():
    return jsonify(_load_categories())


_CATEGORY_ID_RE    = re.compile(r'[a-z][a-z0-9_]{1,29}')
_CATEGORY_COLOR_RE = re.compile(r'#[0-9a-fA-F]{6}')
CATEGORY_LABEL_MAX = 40


@app.route('/api/categories', methods=['POST'])
def api_create_category():
    body  = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    cid   = body.get('id')
    label = body.get('label')
    color = body.get('color')
    if not isinstance(cid, str) or not _CATEGORY_ID_RE.fullmatch(cid) or cid in ('constructor', 'prototype'):
        return jsonify({"error": "El id debe tener 2–30 caracteres: minúsculas, números o _, empezando con una letra."}), 400
    label = label.strip() if isinstance(label, str) else ''
    if not label or len(label) > CATEGORY_LABEL_MAX:
        return jsonify({"error": f"El nombre debe tener entre 1 y {CATEGORY_LABEL_MAX} caracteres."}), 400
    if not isinstance(color, str) or not _CATEGORY_COLOR_RE.fullmatch(color):
        return jsonify({"error": "El color debe ser hexadecimal (#RRGGBB)."}), 400

    # One read-modify-write under the lock, so a concurrent assign/restore
    # can't interleave and lose either change.
    with backup.LOCK:
        categories = _load_categories()
        if cid in categories:
            return jsonify({"error": "Ya existe una categoría con ese id."}), 409
        if any(c.get('label', '').casefold() == label.casefold() for c in categories.values()):
            return jsonify({"error": "Ya existe una categoría con ese nombre."}), 409
        categories[cid] = {"label": label, "color": color.lower(), "keywords": []}
        _save_categories(categories)
    return jsonify({"ok": True, "id": cid, "category": categories[cid]})


@app.route('/api/categories/assign', methods=['POST'])
def api_assign_category():
    body     = request.get_json(silent=True) or {}
    keyword  = (body.get('keyword') or '').strip().upper()
    category = body.get('category')

    if not keyword:
        return jsonify({"error": "Falta el texto a asociar"}), 400

    categories = _load_categories()
    if category not in categories:
        return jsonify({"error": "Categoría desconocida"}), 400

    # A keyword can only belong to one category — remove it from any other
    # category's list first so re-assigning a merchant actually moves it,
    # rather than matching two categories (whichever sorts first wins ties).
    for c in categories.values():
        if keyword in c['keywords']:
            c['keywords'].remove(keyword)

    categories[category]['keywords'].append(keyword)
    _save_categories(categories)
    return jsonify({"ok": True, "category": category, "keyword": keyword})


# ── Debit account (checking-account CSV statements) ────────────────────────
# One continuous account ledger rather than per-cycle statements like the
# credit cards — each CSV upload is a date-ranged batch of transactions
# merged into a single deduplicated list (a real export can span many
# months despite the "del mes" filename — see debit_parser.py). Dedup key
# is the full row (fecha, referencia, codigo, debito, credito, balance):
# a bank reference number is reused across a transaction and its linked fee
# line (same TS/59 pair, same referencia), so referencia alone collides.

_DEBIT_DEDUP_FIELDS = ('fecha', 'referencia', 'codigo', 'debito', 'credito', 'balance')


def _debit_dedup_key(t):
    return tuple(t[f] for f in _DEBIT_DEDUP_FIELDS)


def _dedupe_new_debit_txs(parsed_txs, existing_keys):
    """New transactions from one parsed upload, excluding both (a) rows
    already persisted and (b) a row repeated more than once *within this
    same file* (rare — would need a same-day, same-reference, same-code,
    zero-net repeat to collide — but upload-preview and confirm must agree
    on the count either way, so both call this one shared function rather
    than duplicating the loop)."""
    seen = set(existing_keys)
    new = []
    for t in parsed_txs:
        key = _debit_dedup_key(t)
        if key in seen:
            continue
        seen.add(key)
        new.append(t)
    return new


@app.route('/api/upload-debit-statement', methods=['POST'])
def api_upload_debit_statement():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files['file']
    try:
        content = decode_bank_csv(f.read())
    except UnicodeDecodeError:
        return jsonify({"error": "No se pudo leer el archivo — verifica que sea el CSV exportado por el banco."}), 422

    try:
        parsed = parse_debit_csv(content)
    except DebitParseError as e:
        return jsonify({"error": str(e), "code": e.code}), 422
    except Exception as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 422

    validation = validate_debit_csv(parsed)

    data = get_data()
    existing_keys = {
        _debit_dedup_key(t) for t in data.get('debit_account', {}).get('transactions', [])
    }
    new_txs = _dedupe_new_debit_txs(parsed['transactions'], existing_keys)

    pending_id = str(uuid.uuid4())
    session['pending_debit_upload'] = {
        "pending_id":   pending_id,
        "parsed":       parsed,
        "raw_csv":      content,
        "filename":     f.filename or 'estado.csv',
        "validation_ok": validation['ok'],
    }
    session.modified = True

    fechas = [t['fecha'] for t in parsed['transactions']]
    return jsonify({
        "ok":              True,
        "pending_id":      pending_id,
        "cuenta":          parsed['cuenta'],
        "nombre":          parsed['nombre'],
        "moneda":          parsed['moneda'],
        "fecha_desde":     min(fechas),
        "fecha_hasta":     max(fechas),
        "tx_count":        len(parsed['transactions']),
        "new_tx_count":    len(new_txs),
        "duplicate_count": len(parsed['transactions']) - len(new_txs),
        "validation":      validation,
    })


@app.route('/api/confirm-debit-statement', methods=['POST'])
def api_confirm_debit_statement():
    body       = request.get_json(silent=True) or {}
    pending_id = body.get('pending_id')
    pending    = session.get('pending_debit_upload')

    if not pending:
        return jsonify({"error": "No hay ningún archivo pendiente de confirmar"}), 400
    # Require a matching pending_id unconditionally — see the same comment
    # in api_confirm_statement above.
    if pending.get('pending_id') != pending_id:
        return jsonify({"error": "El archivo pendiente cambió — vuelve a subirlo."}), 409

    # A file that failed validation (balance chain, footer, unreadable rows)
    # needs the preview's explicit acknowledgement — enforced here too, not
    # only by the disabled button.
    if not pending.get('validation_ok', True) and body.get('acknowledged') is not True:
        return jsonify({"error": "El archivo no pasó la validación — confirma que deseas guardarlo."}), 409

    parsed = pending['parsed']

    data    = get_data()
    account = data.setdefault('debit_account', {
        "numero_cliente": '', "cuenta": '', "nombre": '', "moneda": '',
        "transactions": [], "uploads": [],
    })
    account['numero_cliente'] = parsed['numero_cliente'] or account['numero_cliente']
    account['cuenta']         = parsed['cuenta'] or account['cuenta']
    account['nombre']         = parsed['nombre'] or account['nombre']
    account['moneda']         = parsed['moneda'] or account['moneda']

    upload_id     = str(uuid.uuid4())[:8]
    existing_keys = {_debit_dedup_key(t) for t in account['transactions']}
    new_txs_raw   = _dedupe_new_debit_txs(parsed['transactions'], existing_keys)
    # `seq` is a persistent, monotonically increasing counter across every
    # transaction ever confirmed into this account (not reset per upload)
    # — same-day rows have no time-of-day field, so merging two uploads
    # needs *some* stable tiebreaker, and a per-upload-relative index would
    # collide arbitrarily between transactions from different files (upload
    # B's row 5 isn't meaningfully "before" upload A's row 100 just because
    # 5 < 100 — they're different files' local indices, not one timeline).
    # A global counter guarantees transactions confirmed earlier always
    # sort before ones confirmed later when dates tie, which is at least a
    # consistent, well-defined order — not a perfect reconstruction of true
    # bank posting order across separate exports, but stable and correct
    # within any single upload (verified against the balance chain).
    next_seq = account.get('_next_seq', 0)
    new_txs = []
    for t in new_txs_raw:
        new_txs.append({**t, "upload_id": upload_id, "orden": next_seq})
        next_seq += 1
    account['_next_seq'] = next_seq

    account['transactions'].extend(new_txs)
    account['transactions'].sort(key=lambda t: (t['fecha'], t.get('orden', 0)))

    fechas = [t['fecha'] for t in parsed['transactions']]
    account.setdefault('uploads', []).append({
        "id":              upload_id,
        "filename":        pending['filename'],
        "fecha_snapshot":  parsed['fecha_snapshot'],
        "fecha_desde":     min(fechas),
        "fecha_hasta":     max(fechas),
        "saldo_inicial":   parsed['saldo_inicial'],
        "saldo_final":     parsed['saldo_final'],
        "saldo_disponible": parsed['saldo_disponible'],
        "tx_count":        len(parsed['transactions']),
        "new_tx_count":    len(new_txs),
        "resumen_total":   parsed['resumen_total'],
        "warnings":        parsed.get('warnings', []),
        "raw_csv":         pending['raw_csv'],
    })

    # Independent of the fields the budget dashboard tracks — don't bump the
    # shared _version (same reasoning as api_confirm_statement above).
    set_data(data, current=data, bump_version=False)
    session.pop('pending_debit_upload', None)
    return jsonify({"ok": True, "new_tx_count": len(new_txs), "tx_count": len(account['transactions'])})


@app.route('/api/cancel-debit-upload', methods=['POST'])
def api_cancel_debit_upload():
    body       = request.get_json(silent=True) or {}
    pending_id = body.get('pending_id')
    pending    = session.get('pending_debit_upload')
    if pending and (not pending_id or pending.get('pending_id') == pending_id):
        session.pop('pending_debit_upload', None)
    return jsonify({"ok": True})


@app.route('/api/debit-account', methods=['GET'])
def api_get_debit_account():
    data    = get_data()
    account = data.get('debit_account')
    if not account:
        return jsonify({"numero_cliente": '', "cuenta": '', "nombre": '', "moneda": '',
                         "transactions": [], "uploads": []})
    # raw_csv is only needed for a future re-parse migration (same role as
    # raw_markdown for credit-card statements) — never sent to the browser.
    trimmed_uploads = [{k: v for k, v in u.items() if k != 'raw_csv'} for u in account.get('uploads', [])]
    return jsonify({**account, "uploads": trimmed_uploads})


@app.route('/api/debit-account/uploads/<upload_id>', methods=['DELETE'])
def api_delete_debit_upload(upload_id):
    data    = get_data()
    account = data.get('debit_account')
    if not account:
        return jsonify({"error": "No hay cuenta de débito cargada"}), 404

    before = len(account.get('uploads', []))
    account['uploads'] = [u for u in account.get('uploads', []) if u['id'] != upload_id]
    if len(account['uploads']) == before:
        return jsonify({"error": "Upload not found"}), 404

    # Overlapping rows are stored once, owned by the upload that introduced
    # them first — so rows a remaining upload's date range also covers are
    # handed to it (oldest covering upload wins) rather than deleted, or the
    # ledger would lose rows that upload's file contains and the balance
    # chain would break. Only rows no remaining upload covers are dropped.
    # Dates are ISO 'YYYY-MM-DD', so string comparison is chronological.
    reassigned = deleted = 0
    kept = []
    for t in account.get('transactions', []):
        if t.get('upload_id') == upload_id:
            owner = min((u for u in account['uploads']
                         if u.get('fecha_desde') and u['fecha_desde'] <= t['fecha'] <= u['fecha_hasta']),
                        key=lambda u: u['fecha_desde'], default=None)
            if owner is None:
                deleted += 1
                continue
            t['upload_id'] = owner['id']
            reassigned += 1
        kept.append(t)
    account['transactions'] = kept
    set_data(data, current=data, bump_version=False)
    return jsonify({"ok": True, "reassigned_count": reassigned, "deleted_count": deleted})


# ── Pagos: paid ticks, `_local.bill_payments` (shared with the Android app) ──
# The APK's bill category_key strings (BillsAnalytics.CATEGORIES); the same
# keys as BILL_CATEGORIES in static/bills.js.
BILL_KEYS = frozenset(p['key'] for p in UTILITY_PROVIDERS if p.get('bill_category'))
_BILL_MONTH_RE = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])')


@app.route('/api/bill-payments', methods=['PUT'])
def api_put_bill_payment():
    """Tick (paid=true: upsert one row) or untick (paid=false: delete the row,
    as the APK's BillsDao.clearPaid does) one (category_key, month). Bumps
    `_version` and requires the caller's, because the dashboard POSTs its
    whole cached blob, `_local` included: a stale tab must 409, not restore
    old ticks."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    key, month, paid, version = (body.get(k) for k in ('category_key', 'month', 'paid', '_version'))
    if not isinstance(key, str) or key not in BILL_KEYS:
        return jsonify({"error": "Pago desconocido"}), 400
    if not isinstance(month, str) or not _BILL_MONTH_RE.fullmatch(month):
        return jsonify({"error": "Mes inválido (AAAA-MM)"}), 400
    if not isinstance(paid, bool):
        return jsonify({"error": "paid debe ser true o false"}), 400
    if isinstance(version, bool) or not isinstance(version, int):
        return jsonify({"error": "Falta _version"}), 400

    with backup.LOCK:
        data = get_data()
        if version != data.get('_version', 0):
            return jsonify({
                "error": "conflict",
                "message": "Los datos cambiaron en otra sesión. Recarga la página antes de guardar."
            }), 409
        local = data.get('_local') or {}
        existing = local.get('bill_payments', [])
        if not isinstance(existing, list):
            # Never rebuild over a corrupted section: iterating a string would
            # persist its characters as rows, and [] would drop the ticks.
            return jsonify({"error": "_local.bill_payments está dañado; restaura un respaldo"}), 500
        rows = [r for r in existing
                if not (isinstance(r, dict) and r.get('category_key') == key and r.get('month') == month)]
        if paid:
            rows.append({"category_key": key, "month": month, "paid": True})
        local['bill_payments'] = rows
        data['_local'] = local
        new_version = set_data(data, current=data)
    return jsonify({"ok": True, "version": new_version, "bill_payments": rows})


# ── Category budgets (monthly USD target per category, integer cents) ──────
# Stored in `_local.category_budgets_cents`, the APK's export shape. Targets of
# deleted categories are kept (never pruned); the page hides them. Writes bump
# `_version`: the budget dashboard POSTs its whole cached blob, `_local`
# included, so it must 409 and reload rather than restore stale targets.

CATEGORY_BUDGET_MAX_CENTS = interop.CATEGORY_BUDGET_MAX_CENTS


def _write_category_budget(category_id, cents):
    """cents=None removes the target. Returns (response, status)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    version = body.get('_version')
    if isinstance(version, bool) or not isinstance(version, int):
        return jsonify({"error": "Falta _version"}), 400
    with backup.LOCK:
        data = get_data()
        if version != data.get('_version', 0):
            return jsonify({
                "error": "conflict",
                "message": "Los datos cambiaron en otra sesión. Recarga la página antes de guardar."
            }), 409
        local = data.get('_local')
        if not isinstance(local, dict):
            local = data['_local'] = {}
        targets = local.get('category_budgets_cents')
        if not isinstance(targets, dict):
            targets = local['category_budgets_cents'] = {}
        if cents is None:
            if category_id not in targets:
                return jsonify({"error": "Esa categoría no tiene meta"}), 404
            del targets[category_id]
        else:
            targets[category_id] = cents
        new_version = set_data(data, current=data)
    return jsonify({"ok": True, "version": new_version, "category_budgets_cents": targets}), 200


@app.route('/api/category-budgets/<category_id>', methods=['PUT'])
def api_put_category_budget(category_id):
    if category_id == '_uncat' or category_id not in _load_categories():
        return jsonify({"error": "Categoría desconocida"}), 400
    body = request.get_json(silent=True)
    cents = body.get('cents') if isinstance(body, dict) else None
    if isinstance(cents, bool) or not isinstance(cents, int) \
            or not 0 < cents <= CATEGORY_BUDGET_MAX_CENTS:
        return jsonify({"error": "La meta debe ser un entero de centavos entre 1 y "
                                 f"{CATEGORY_BUDGET_MAX_CENTS}."}), 400
    return _write_category_budget(category_id, cents)


@app.route('/api/category-budgets/<category_id>', methods=['DELETE'])
def api_delete_category_budget(category_id):
    return _write_category_budget(category_id, None)


# ── Run ───────────────────────────────────────────────────────────────────

# 5050 preferred; falls forward if another program holds it. Stays below the
# 5090-5099 range the test-app skill uses for isolated test instances.
PORT_RANGE = range(5050, 5070)


def _is_this_app(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/ping', timeout=1) as r:
            return json.loads(r.read(256)).get('app') == 'presupuesto'
    except urllib.error.HTTPError as e:
        # Basic Auth enabled: 401 with our realm is still this app
        return e.code == 401 and 'Presupuesto' in e.headers.get('WWW-Authenticate', '')
    except Exception:
        return False


def _acquire_instance_lock():
    """Exclusive OS lock on the data dir, held for the process lifetime and
    released by the OS on exit. None if another instance holds it — so two
    servers never write the same data files, even if launched simultaneously
    (a port check alone can't guarantee that: werkzeug binds with
    SO_REUSEADDR, which on Windows lets two servers share a port)."""
    f = open(os.path.join(DATA_DIR, '.instance.lock'), 'a')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _find_running_port(wait=15):
    """Port of the instance holding the lock; polls since it may still be starting."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        for port in PORT_RANGE:
            if _is_this_app(port):
                return port
        time.sleep(0.5)
    return None


def _find_free_port():
    for port in PORT_RANGE:
        with socket.socket() as s:
            # Connect check too: on Windows, binding 127.0.0.1 can succeed
            # even while another program listens on 0.0.0.0 at that port.
            s.settimeout(0.3)
            if s.connect_ex(('127.0.0.1', port)) == 0:
                continue
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', port))
            except OSError:
                continue
        return port
    raise SystemExit(f'[ERROR] No free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}.')


if __name__ == '__main__':
    _debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    if os.environ.get('WERKZEUG_RUN_MAIN'):
        # Debug-reloader child: the parent holds the lock, picked the port and opened the browser.
        _port = int(os.environ['BUDGET_APP_PORT'])
    else:
        _lock = _acquire_instance_lock()
        if _lock is None:
            _port = _find_running_port()
            if _port is None:
                raise SystemExit('[ERROR] Presupuesto is already running but not reachable '
                                 f'on ports {PORT_RANGE.start}-{PORT_RANGE.stop - 1}.')
            print(f'Presupuesto is already running at http://127.0.0.1:{_port}/ - opening it.')
            webbrowser.open(f'http://127.0.0.1:{_port}/')
            raise SystemExit(0)
        _port = _find_free_port()
        _url = f'http://127.0.0.1:{_port}/'
        os.environ['BUDGET_APP_PORT'] = str(_port)
        print(f'Presupuesto: {_url}')
        threading.Timer(1.0, webbrowser.open, (_url,)).start()
    app.run(debug=_debug, host='127.0.0.1', port=_port)
