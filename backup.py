"""Automatic rotating backups of budget_data.json + categories.json.

One backup file holds both data files plus hashes, and is verified (re-read,
re-hashed) before it gets its final name, so a listed backup is always
restorable. No Flask imports — every function takes its paths as arguments.
"""
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
from datetime import datetime

log = logging.getLogger(__name__)

FORMAT = 'presupuesto-backup'
FORMAT_VERSION = 1
KINDS = ('auto', 'prerestore', 'preimport')
NAME_RE = re.compile(r'^(\d{8}-\d{6}-\d{6})_(auto|prerestore|preimport)\.json\Z', re.ASCII)
STALE_TMP_SECONDS = 3600

KEEP_AUTO = 10
KEEP_DAYS = 30
KEEP_MONTHS = 12
KEEP_PRE = 5

# Held by snapshot+prune; 1.2 reuses it for restore. Reentrant so a restore
# holding it can still take its own pre-restore snapshot.
LOCK = threading.RLock()

# path -> (size, mtime_ns, ok). Backups are immutable, so a verify result
# stays valid while size+mtime are unchanged; saves prune without re-reading
# every ~2 MB file.
_verified_cache = {}


class BackupError(Exception):
    pass


def parse_name(name):
    """(datetime, kind) for a well-formed backup name, else None."""
    m = NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), '%Y%m%d-%H%M%S-%f'), m.group(2)
    except ValueError:
        return None


def _payload_sha256(budget_data, categories):
    canon = json.dumps({"budget_data": budget_data, "categories": categories},
                       sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()


def _check_sources(budget_data, categories):
    if not isinstance(budget_data, dict) or '_version' not in budget_data:
        raise BackupError('budget_data is not a dict with _version')
    if not isinstance(categories, dict):
        raise BackupError('categories is not a dict')


def _counts(budget_data, categories):
    cards = (budget_data.get('statements') or {}).values()
    stmts = [s for card in cards for s in card.get('statements', [])]
    return {
        "statements":   len(stmts),
        "statement_tx": sum(len(s.get('transactions', [])) for s in stmts),
        "debit_tx":     len((budget_data.get('debit_account') or {}).get('transactions', [])),
        "history":      len(budget_data.get('history') or []),
        "categories":   len(categories),
    }


def verify(raw):
    """Parse and fully validate a backup file's bytes; return the document."""
    try:
        doc = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        raise BackupError(f'unreadable backup: {e}') from e
    if not isinstance(doc, dict) or doc.get('format') != FORMAT \
            or doc.get('format_version') != FORMAT_VERSION:
        raise BackupError('not a presupuesto-backup v1 file')
    _check_sources(doc.get('budget_data'), doc.get('categories'))
    if doc.get('payload_sha256') != _payload_sha256(doc['budget_data'], doc['categories']):
        raise BackupError('payload hash mismatch')
    return doc


def _read_verified(backup_dir, name):
    if parse_name(name) is None:
        raise BackupError(f'invalid backup name: {name!r}')
    with open(os.path.join(backup_dir, name), 'rb') as f:
        return verify(f.read())


def load_verified(backup_dir, name):
    """(budget_data, categories) from a backup; raises on any validation failure."""
    doc = _read_verified(backup_dir, name)
    return doc['budget_data'], doc['categories']


def _names_newest_first(backup_dir):
    parsed = [(parse_name(n), n) for n in os.listdir(backup_dir)]
    return [n for p, n in sorted((x for x in parsed if x[0]), key=lambda x: (x[0][0], x[1]),
                                 reverse=True)]


def _newest_verified(backup_dir):
    for name in _names_newest_first(backup_dir):
        try:
            return name, _read_verified(backup_dir, name)
        except (BackupError, OSError):
            continue
    return None, None


def list_backups(backup_dir):
    """Newest-first metadata (no data) for every well-named backup."""
    if not os.path.isdir(backup_dir):
        return []
    out = []
    for name in _names_newest_first(backup_dir):
        when, kind = parse_name(name)
        path = os.path.join(backup_dir, name)
        entry = {"name": name, "created_at": when.isoformat(), "kind": kind,
                 "size": None, "counts": None, "ok": False}
        try:
            entry['size'] = os.path.getsize(path)
            doc = _read_verified(backup_dir, name)
            entry.update(created_at=doc.get('created_at', entry['created_at']),
                         counts=doc.get('counts'), ok=True)
        except (BackupError, OSError):
            pass
        out.append(entry)
    return out


def retention_keep(names, always_keep=None):
    """Set of names to keep. Unparseable names are ignored here (and never
    pruned). Day/month windows count periods that have backups, not calendar
    time, so a long gap never ages everything out."""
    parsed = sorted(((parse_name(n), n) for n in names if parse_name(n)),
                    key=lambda x: (x[0][0], x[1]), reverse=True)
    keep = set()
    autos = [n for (_, k), n in parsed if k == 'auto']
    pres = [n for (_, k), n in parsed if k in ('prerestore', 'preimport')]
    keep.update(autos[:KEEP_AUTO])
    keep.update(pres[:KEEP_PRE])
    for period, limit in ((lambda d: d.date(), KEEP_DAYS),
                          (lambda d: (d.year, d.month), KEEP_MONTHS)):
        seen = set()
        for (when, _), n in parsed:
            p = period(when)
            if p not in seen and len(seen) < limit:
                seen.add(p)
                keep.add(n)
    if always_keep:
        keep.add(always_keep)
    return keep


def _is_verified(backup_dir, name):
    path = os.path.join(backup_dir, name)
    try:
        st = os.stat(path)
    except OSError:
        return False
    cached = _verified_cache.get(path)
    if cached and cached[:2] == (st.st_size, st.st_mtime_ns):
        return cached[2]
    try:
        _read_verified(backup_dir, name)
        ok = True
    except (BackupError, OSError):
        ok = False
    _verified_cache[path] = (st.st_size, st.st_mtime_ns, ok)
    return ok


def _prune(backup_dir, keep_name):
    """Only verified backups count toward (and are removed by) retention, so a
    corrupt file can never crowd a good one out of a window. Corrupt files are
    left in place and show as not-ok in list_backups."""
    names = os.listdir(backup_dir)
    now = datetime.now().timestamp()
    for name in names:
        if name.endswith('.tmp'):  # orphan of a killed write; ours finished under LOCK
            path = os.path.join(backup_dir, name)
            try:
                if now - os.path.getmtime(path) > STALE_TMP_SECONDS:
                    os.remove(path)
            except OSError:
                log.exception('Could not remove stale temp %s', path)
    verified = [n for n in names if parse_name(n) and _is_verified(backup_dir, n)]
    keep = retention_keep(verified, always_keep=keep_name)
    for name in verified:
        if name in keep:
            continue
        path = os.path.join(backup_dir, name)
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            os.remove(path)
            _verified_cache.pop(path, None)
        except OSError:
            log.exception('Could not prune backup %s', path)


def snapshot(data_file, categories_file, backup_dir, kind='auto', force=False):
    """Write a verified backup of both data files; return its name, or None
    when the sources are byte-identical to the newest verified backup."""
    if kind not in KINDS:
        raise ValueError(f'unknown backup kind: {kind!r}')
    with LOCK:
        with open(data_file, 'rb') as f:
            raw_data = f.read()
        with open(categories_file, 'rb') as f:
            raw_cats = f.read()
        try:
            budget_data = json.loads(raw_data.decode('utf-8'))
            categories = json.loads(raw_cats.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as e:
            raise BackupError(f'live data file is not valid JSON: {e}') from e
        _check_sources(budget_data, categories)

        source_sha = hashlib.sha256(raw_data + b'\0' + raw_cats).hexdigest()
        os.makedirs(backup_dir, exist_ok=True)
        if not force:
            _, newest = _newest_verified(backup_dir)
            if newest and newest.get('source_sha256') == source_sha:
                return None

        now = datetime.now()
        doc = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "created_at": now.isoformat(),
            "kind": kind,
            "source_sha256": source_sha,
            "payload_sha256": _payload_sha256(budget_data, categories),
            "counts": _counts(budget_data, categories),
            "budget_data": budget_data,
            "categories": categories,
        }
        fd, tmp_path = tempfile.mkstemp(dir=backup_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8'))
                f.flush()
                os.fsync(f.fileno())
            with open(tmp_path, 'rb') as f:
                verify(f.read())
            for _ in range(100):
                name = f"{now.strftime('%Y%m%d-%H%M%S-%f')}_{kind}.json"
                try:
                    os.rename(tmp_path, os.path.join(backup_dir, name))
                    break
                except FileExistsError:
                    now = datetime.now()
            else:
                raise BackupError('could not find a free backup name')
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        # The backup is already verified and in place; a chmod failure (AV or
        # indexer holding the new file) must not report it failed or skip pruning.
        try:
            os.chmod(os.path.join(backup_dir, name), stat.S_IREAD)
        except OSError:
            log.exception('Could not mark backup %s read-only', name)
        _prune(backup_dir, name)
        return name
