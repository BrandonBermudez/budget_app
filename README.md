# Presupuesto

> **ES:** Rastreador de finanzas personales (Flask) que importa estados de cuenta de tarjeta de crédito (PDF/markdown) y CSV de cuenta de débito de un banco específico; interfaz en español/inglés, montos en USD, datos locales en JSON.

A single-user personal finance tracker built with Flask. It keeps a net-worth dashboard and imports one bank's credit-card statement (PDF or markdown) and checking-account CSV formats, then categorizes and analyzes the transactions. Server-rendered Jinja pages with vanilla JavaScript, Spanish/English UI, USD amounts, and local JSON storage (no database).

> [!IMPORTANT]
> **This is a template, not a ready-to-use app for your bank.**
> The statement and CSV importers were written for one specific bank's file layouts and will reject files from any other bank ("format not recognized"). Out of the box you get the manual features (dashboard, bills, budgets, projection). The transactions, checking-account and cash-flow pages stay empty until you write importers for your own bank. See [Adding your bank](#adding-your-bank), which includes a prompt you can give an AI coding assistant.
>
> **ES:** Esto es una plantilla. Los importadores solo leen el formato de un banco; para el tuyo hay que escribir parsers nuevos (ver [Adding your bank](#adding-your-bank)).

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

On first run the app shows the sample seed from `data/defaults.json`. `data/budget_data.json` is created the first time you save a change. Edit or reset the values from the dashboard.

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

## Adding your bank

The rest of the app never reads bank files. It only consumes the dicts returned by two parser functions. Replace those functions (keep their names and return shapes) and everything downstream works: categorization, spend analytics, validation banners, the cash-flow page.

### What works without a parser

| Works as is | Needs your parser |
|---|---|
| Dashboard, net-worth history, bills (manual entry), category budgets, projection, backups, export/import | Credit-card statement upload → Transactions page and statement detail |
| | Checking-account CSV upload → Debit Account and Cash Flow pages |
| | Email fetch (also needs `.env` settings and `STATEMENT_EMAIL_SUBJECT`) |

### Credit card: `parser.py`

`parse_statement(content: str) -> dict`. `content` is the statement as text: a PDF is converted to markdown first by `convert_pdf_to_markdown` (markitdown), so look at that output, not the PDF, when writing the parser. Required keys:

| Key | Type | Meaning |
|---|---|---|
| `card_name`, `card_key` | str | Display name and stable id. Use `_normalise_card_name()` so `data/card_map.json` is honored. |
| `fecha_corte` | str `YYYY-MM-DD` | Statement closing date. **Empty string = upload rejected.** |
| `fecha_limite`, `fecha_bonif` | str | Payment due date, grace date (`''` if absent). |
| `saldo_dolares` | float or None | Closing balance. |
| `saldo_anterior_usd` | float, optional | Previous balance (defaults to `0.0`). |
| `pago_minimo_usd`, `pago_contado_usd` | float | Minimum payment and full-payment amount. `pago_contado_usd` drives the validation check. |
| `transactions` | list | See below. |
| `unparsed` | list[str] | Lines that looked like transactions but didn't parse. Shown to the user, never dropped silently. |

Each transaction: `ref` (str, unique within the statement), `fecha` (`YYYY-MM-DD`), `descripcion` (str), `monto_usd` (float, negative for credits), `tipo` (`cargo` purchase, `credito` refund/payment, `cuota` installment payment, `bonificacion` reward/interest line), `subtarjeta` (str: which card on the account made the charge, e.g. `principal`; any constant if your bank doesn't split them).

`validate_statement()` compares the sum of `cargo` + `cuota` with `pago_contado_usd`. If your bank prints no equivalent total, return `0.0`: the check is skipped and the UI shows "no printed total".

Bank-specific rules to revisit in `parser.py` and `static/spend.js`: payment rows are recognized by `PAGO RECIBIDO` and installment-plan reversals by `CR COMPRA PLZ`. Change those patterns to your bank's wording.

### Checking account: `debit_parser.py`

`decode_bank_csv(raw_bytes) -> str` (the current one tries UTF-8, then cp1252) and `parse_debit_csv(content: str) -> dict`. Raise `DebitParseError(code, message)` when the file can't be trusted. Required keys:

| Key | Type | Meaning |
|---|---|---|
| `cuenta`, `nombre`, `moneda` | str | Account number, holder label, currency. Shown in the upload preview. |
| `numero_cliente` | str | Customer number (`''` if absent). |
| `saldo_inicial`, `saldo_final`, `saldo_disponible` | float | Opening, closing and available balance. `saldo_inicial` seeds the balance-chain check. |
| `fecha_snapshot` | str `YYYY-MM-DD` | Export date. |
| `transactions` | list, at least 1 | See below. |
| `resumen_codigos`, `resumen_total` | dict / None | Bank footer totals per code. Use `{}` and `None` if the bank prints none. |
| `warnings` | list | Rows you couldn't parse (`{"code", "line", "row"}`). Never drop rows silently. |

Each transaction: `fecha` (`YYYY-MM-DD`), `referencia` (str), `codigo` (str, transaction type code or `''`), `descripcion` (str), `debito` and `credito` (float ≥ 0, `0.0` when blank), `balance` (float, running balance after the row). Duplicate detection uses every field except `descripcion` (`_DEBIT_DEDUP_FIELDS` in `app.py`), so re-uploading an overlapping export is safe.

`validate_debit_csv()` replays `saldo_inicial` through every row's `debito`/`credito` and compares with `balance`. It only works if your CSV has a running-balance column. If not, compute `balance` yourself from the opening balance.

Card payments in the CSV are matched to cards by the last 4 digits (`payment_last4` in `card_map.json`). The row pattern is `PAGO_CARD_RE` in `static/debit.js` (also used by `static/cashflow.js`).

### Prompt template for an AI coding assistant

Remove names, account numbers and addresses from your sample files before sharing them with any AI tool. Replace real digits with the same number of `0`s, keeping the layout exact.

```text
I'm adapting the open-source "Presupuesto" Flask app (this repo) to my bank.
The app is a template: parser.py and debit_parser.py only understand the
original author's bank. Rewrite them for my bank. Keep every function name,
signature and return shape documented in README.md "Adding your bank".

My bank: <bank name / country>
Currency on statements: <e.g. USD only | USD and local currency>

1. Credit-card statement. Here is the text markitdown produces from my PDF
   (python -c "from markitdown import MarkItDown; print(MarkItDown().convert('statement.pdf').text_content)"),
   with personal data replaced:
   <paste>
   - The statement date is labeled: <label>
   - The full-payment / new-balance total is labeled: <label>
   - Payments to the card look like: <example row>
   - Refunds look like: <example row>
   - Installment ("cuotas") rows look like: <example row, or "none">

2. Checking-account export (<CSV | XLSX | other>, encoding if known):
   <paste the header and ~10 rows, personal data replaced>
   - Date format: <DD/MM/YYYY | MM/DD/YYYY | ...>
   - Does it have a running-balance column? <yes/no>
   - A card-payment row looks like: <example row>

Requirements:
- Never drop a row silently: anything that looks like a transaction but
  fails to parse goes into `unparsed` (credit) or `warnings` (debit).
- Reject non-finite amounts (inf/nan).
- Update the payment / plan-reversal patterns (PAGO RECIBIDO,
  CR COMPRA PLZ) in parser.py and static/spend.js to my bank's wording.
- Update the card-payment pattern PAGO_CARD_RE in static/debit.js.
- Show me the parsed output for my samples and confirm the statement total
  and the CSV balance chain both validate.
- Replace the default categories in data/categories.json with merchants that
  appear in my sample (keep the category ids).
```

## Limitations

- **Template: one bank's formats only.** The parsers understand a single bank's credit-card statement and checking-account CSV layouts and reject everything else. See [Adding your bank](#adding-your-bank).
- **USD amounts only.**
- **Single user, local storage.** Data lives in JSON files under `data/`. There is no database and no multi-user support. Unless `BUDGET_APP_USER`/`BUDGET_APP_PASS` are set, `/api/data` returns the whole dataset with no authentication.
- **Windows-first.** Other platforms should work via `python app.py` but are untested.
- **No automated tests** are included in this repository.

## Security

This app is meant to run locally on your own machine. Do not expose it to a network or the internet unless you set `BUDGET_APP_USER` and `BUDGET_APP_PASS`, set a strong `SECRET_KEY`, and put it behind HTTPS. Never commit `.env`, `data/budget_data.json`, `data/card_map.json`, `data/utilities.json`, or bank exports. The included `.gitignore` excludes them.

## License

[MIT](LICENSE)
