# Presupuesto

> **ES:** Rastreador de finanzas personales (Flask) que importa estados de cuenta de tarjeta de crédito (PDF/markdown) y CSV de cuenta de débito de un banco específico; interfaz en español/inglés, montos en USD, datos locales en JSON.

A single-user personal finance tracker built with Flask. It keeps a net-worth dashboard and imports one bank's credit-card statement (PDF or markdown) and checking-account CSV formats, then categorizes and analyzes the transactions. Server-rendered Jinja pages with vanilla JavaScript, Spanish/English UI, USD amounts, and local JSON storage (no database).

## Features

- **Dashboard:** accounts, credit cards, installment plans, one-time expenses, reserves, salary, and net-worth history snapshots with a chart.
- **Credit-card statements:** upload a PDF (converted with `markitdown`) or a markdown/text export. Preview first, then confirm to save. Keyword-based categorization (editable from the UI), spending trends, recurring and largest expenses, and a per-statement validation check against the statement's printed totals.
- **Checking-account CSV:** imported as one continuous ledger with duplicate detection, balance-chain self-validation, and analytics (card payments, transfers, fixed payments, interest).
- **Planning:** bills, cash flow, per-category budgets, and a 3-month projection.
- **Backups:** automatic snapshots of the data files, restore from the UI, and JSON export/import.
- **Spanish/English toggle** on every page (saved in the browser).
- **Optional email fetch:** pulls statement emails over IMAP (Gmail by default).

## Requirements

- Python 3.12 or 3.13 (the pinned `numpy` needs 3.12+; the pinned `onnxruntime` has no wheels for 3.14).
- Developed and used on Windows. `python app.py` should work elsewhere but is untested.

## Setup

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env  (optional; see Configuration)
python app.py
```

On Windows you can also double-click `start_server.bat` once the venv exists.

The app opens your browser at `http://127.0.0.1:5050`, or the next free port up to 5069. It always binds to `127.0.0.1`. Launching it a second time just opens the running instance.

On first run `data/budget_data.json` is created from the sample seed in `data/defaults.json`. Edit or reset the values from the dashboard.

## Configuration

### Environment (`.env`)

Every variable is optional for local use. See `.env.example`.

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Session-signing key. Without it the app uses a built-in development key and prints a warning. |
| `FLASK_DEBUG` | `1` enables the Werkzeug debugger. Local development only. |
| `BUDGET_APP_USER`, `BUDGET_APP_PASS` | When both are set, every route requires HTTP Basic Auth. |
| `GMAIL_EMAIL`, `GMAIL_APP_PASSWORD` | Account and app password for the email fetch feature. |
| `IMAP_HOST`, `IMAP_PORT` | IMAP server (default `imap.gmail.com:993`). |
| `STATEMENT_EMAIL_SUBJECT` | Text contained in the subject of your statement emails. Required for email fetch. |

### Cards (`data/card_map.json`)

Copy `data/card_map.example.json` to `data/card_map.json` (gitignored) and list your cards:

| Field | Meaning |
|---|---|
| `key` | Stable internal id for the card. Statements are stored under it, so don't change it later. |
| `name` | Display name. |
| `color` | Hex color used in charts and on the Cash Flow page. |
| `account_suffixes` | The masked account number printed in the credit-card statement header. This is the primary way a statement is matched to a card. |
| `payment_last4` | Last 4 digits that appear in card-payment rows of the checking-account CSV (can differ from the statement's account number). |
| `header_patterns` | Regexes matched (case-insensitive) against the statement header when the account number isn't listed. Tried in file order. |

Without `card_map.json`, card identity falls back to the header patterns (none) and then to a hash of the header line, so each card still gets a consistent key but a generic name. The Cash Flow page will also flag every card payment in the checking-account CSV as unrecognized.

### Utility providers (`data/utilities.json`)

Copy `data/utilities.example.json` to `data/utilities.json` (gitignored):

| Field | Meaning |
|---|---|
| `key` | Stable id, stored with bill payments. Don't rename it later. |
| `keyword` | Uppercase text matched in credit-card statement descriptions. |
| `label`, `label_en` | Spanish and English display names. |
| `color` | Hex color for the provider's spend-trend tile on the transactions page. |
| `bill_category` | Optional. A category id from `data/categories.json` (e.g. `agua`, `electricidad`, `internet_telefono`). When set, the provider is also a monthly line on the Bills page. Without it, its rows stay out of bill lines. |

### Categories (`data/categories.json`)

Keyword rules for categorizing statement rows (uppercase substring match, longest keyword wins). The shipped lists are short generic examples. Add your own merchants from the UI; the app saves them to this file. Keep the category ids: the UI and translations depend on them.

## Limitations

- **One bank's formats only.** The parsers understand a single bank's credit-card statement and checking-account CSV layouts. Other banks need new parsers.
- **USD amounts only.**
- **Single user, local storage.** Data lives in JSON files under `data/`. There is no database and no multi-user support. Unless `BUDGET_APP_USER`/`BUDGET_APP_PASS` are set, `/api/data` returns the whole dataset with no authentication.
- **Windows-first.** Other platforms should work via `python app.py` but are untested.
- **No automated tests** are included in this repository.

## Security

This app is meant to run locally on your own machine. Do not expose it to a network or the internet unless you set `BUDGET_APP_USER` and `BUDGET_APP_PASS`, set a strong `SECRET_KEY`, and put it behind HTTPS. Never commit `.env`, `data/budget_data.json`, `data/card_map.json`, `data/utilities.json`, or bank exports. The included `.gitignore` excludes them.

## License

[MIT](LICENSE)
