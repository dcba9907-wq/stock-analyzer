# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip3 install -r requirements.txt

# Run the app
streamlit run app.py

# Syntax check a module
python3 -c "import dart_api; import calculator"
```

## Architecture

Single-page Streamlit app with three layers:

**`dart_api.py`** — DART OpenAPI integration
- `_get_corp_codes()`: Downloads and file-caches (`.corp_codes_cache.json`, 24h TTL) the full DART company list as a name→{corp_code, stock_code} dict
- `search_corp(name)`: Fuzzy name lookup — exact → starts-with → contains; returns `(corp_code, matched_name)`
- `get_financial_data(corp_code, year)`: Fetches `/fnlttSinglAcntAll.json` with `fs_div=CFS, reprt_code=11011`; matches by `account_id` (IFRS codes) with `account_nm` fallback; returns amounts in **백만원**
- `get_share_count(corp_code, year)`: Fetches `/stockTotqySttus.json`; prefers `se=="보통주"` row, falls back to 합계 or first valid entry

**`calculator.py`** — S-RIM valuation
- `calculate_srim(equity_mil, net_income_mil, shares, required_return, w)`: Pure function; takes equity/net-income in 백만원, multiplies by 1,000,000 for 원 conversion when computing per-share price
- Basic formula: `EV = equity + equity × (ROE - r) / r`
- Conservative formula: `EV_w = equity + equity × (ROE - r) × w / (1 + r/100 - w)`

**`app.py`** — Streamlit UI
- Session state keys: `results` (list of result dicts), `params` (year/req_ret/w), `show_history` (bool toggle)
- `analyze()`: orchestrates dart_api → calculator pipeline per company
- `render_card()`: HTML card with green/red background based on `recommend_basic`
- `make_excel()`: openpyxl workbook with color-coded rows
- History persisted to `history.json` (local file, append-only)

## Key Data Contracts

- DART financial amounts are in **원(KRW)**; displayed as 억원 (÷100,000,000), per-share price = EV ÷ shares (no unit conversion needed)
- `required_return` and `ROE` are always in **%** (not decimal) throughout the codebase
- `w` is a dimensionless fade factor in [0.0, 0.5]
- DART account IDs used: `ifrs-full_EquityAttributableToOwnersOfParent` (자기자본), `ifrs-full_ProfitLossAttributableToOwnersOfParent` (당기순이익)

## Environment

Requires `.env` with `DART_API_KEY=<key>` — obtain from https://opendart.fss.or.kr
