"""Fetch credit-card statement emails from Gmail via IMAP, download the
PDF attachments, and convert each to markdown via the `markitdown` library.

Credentials come from .env: GMAIL_EMAIL, GMAIL_APP_PASSWORD, plus
STATEMENT_EMAIL_SUBJECT (text the bank's statement email subject contains).
Optional overrides: IMAP_HOST (default imap.gmail.com), IMAP_PORT (default 993).
"""
import os
import email
import imaplib
import tempfile
from datetime import datetime, timedelta
from email.header import decode_header

from dotenv import load_dotenv
from markitdown import MarkItDown

load_dotenv()

IMAP_HOST = os.environ.get('IMAP_HOST', 'imap.gmail.com')
IMAP_PORT = int(os.environ.get('IMAP_PORT', '993'))
SUBJECT_CONTAINS = os.environ.get('STATEMENT_EMAIL_SUBJECT', '')
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # matches app.py's MAX_CONTENT_LENGTH for uploads

_md_converter = MarkItDown()


def _connect():
    email_addr   = os.environ.get('GMAIL_EMAIL')
    app_password = os.environ.get('GMAIL_APP_PASSWORD')
    if not email_addr or not app_password:
        raise RuntimeError('GMAIL_EMAIL / GMAIL_APP_PASSWORD not set in .env')
    if not SUBJECT_CONTAINS:
        raise RuntimeError('STATEMENT_EMAIL_SUBJECT not set in .env')

    # timeout so a hung/unreachable server can't block the request thread forever.
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    try:
        imap.login(email_addr, app_password)
    except Exception:
        # login raised after the socket was opened — close it before
        # re-raising so a bad-password attempt doesn't leak the connection.
        try:
            imap.shutdown()
        except Exception:
            pass
        raise
    return imap


def _decode(value):
    if not value:
        return ''
    decoded = ''
    for text, enc in decode_header(value):
        decoded += text.decode(enc or 'utf-8', errors='replace') if isinstance(text, bytes) else text
    return decoded


def _search_statement_emails(imap, days):
    """Search by subject, not sender — these arrive as forwards, so the From
    header is the user's own address, not the bank's."""
    since_date = (datetime.now() - timedelta(days=days)).strftime('%d-%b-%Y')
    status, data = imap.search(None, f'(SUBJECT "{SUBJECT_CONTAINS}" SINCE {since_date})')
    if status != 'OK':
        raise RuntimeError(f'IMAP search failed: {status}')
    return data[0].split()


def test_connection(days=90):
    """Verify IMAP login works and count matching statement emails in the last `days` days."""
    imap = _connect()
    try:
        imap.select('INBOX', readonly=True)
        msg_ids = _search_statement_emails(imap, days)

        matching = 0
        for msg_id in msg_ids:
            status, msg_data = imap.fetch(msg_id, '(BODY.PEEK[HEADER.FIELDS (SUBJECT)])')
            if status != 'OK' or not msg_data or not msg_data[0]:
                continue
            header = email.message_from_bytes(msg_data[0][1])
            if SUBJECT_CONTAINS in _decode(header.get('Subject')):
                matching += 1

        return {
            "ok": True,
            "email": os.environ.get('GMAIL_EMAIL'),
            "imap_host": IMAP_HOST,
            "days": days,
            "total_matching_subject": len(msg_ids),
            "matching_subject": matching,
        }
    finally:
        imap.logout()


def fetch_statements(days=90):
    """
    Connect to Gmail, find statement emails from the last `days` days,
    download each PDF attachment, and convert it to markdown.

    Returns a list of dicts: {subject, date, filename, markdown}
    """
    imap = _connect()
    results = []
    try:
        imap.select('INBOX', readonly=True)
        msg_ids = _search_statement_emails(imap, days)

        for msg_id in msg_ids:
            status, msg_data = imap.fetch(msg_id, '(RFC822)')
            if status != 'OK' or not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get('Subject'))
            if SUBJECT_CONTAINS not in subject:
                continue

            date_str = msg.get('Date', '')

            for part in msg.walk():
                filename = part.get_filename()
                if not filename or not _decode(filename).lower().endswith('.pdf'):
                    continue
                filename = _decode(filename)

                payload = part.get_payload(decode=True)
                if not payload or len(payload) > MAX_ATTACHMENT_BYTES:
                    continue

                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    with open(tmp_path, 'wb') as f:
                        f.write(payload)
                    markdown_text = _md_converter.convert(tmp_path).text_content
                finally:
                    os.remove(tmp_path)

                results.append({
                    "subject":  subject,
                    "date":     date_str,
                    "filename": filename,
                    "markdown": markdown_text,
                })

        return results
    finally:
        imap.logout()


if __name__ == '__main__':
    import json
    print(json.dumps(test_connection(), indent=2, ensure_ascii=False))
