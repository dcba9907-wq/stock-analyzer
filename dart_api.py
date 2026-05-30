import io
import json
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

DART_API_KEY = os.getenv("DART_API_KEY", "")
BASE_URL = "https://opendart.fss.or.kr/api"
CACHE_FILE = Path(__file__).parent / ".corp_codes_cache.json"
CACHE_TTL = 86400  # 24h

EQUITY_IDS = {"ifrs-full_EquityAttributableToOwnersOfParent"}
INCOME_IDS = {"ifrs-full_ProfitLossAttributableToOwnersOfParent"}


def _get_corp_codes() -> dict:
    if CACHE_FILE.exists() and (time.time() - CACHE_FILE.stat().st_mtime) < CACHE_TTL:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))

    if not DART_API_KEY:
        raise RuntimeError("DART_API_KEY가 설정되지 않았습니다.")

    resp = requests.get(
        f"{BASE_URL}/corpCode.xml",
        params={"crtfc_key": DART_API_KEY},
        timeout=60,
    )
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_bytes)
    corps = {}
    for item in root.findall(".//list"):
        name = item.findtext("corp_name", "").strip()
        code = item.findtext("corp_code", "").strip()
        stock = item.findtext("stock_code", "").strip()
        if name and code:
            corps[name] = {"corp_code": code, "stock_code": stock}

    CACHE_FILE.write_text(json.dumps(corps, ensure_ascii=False), encoding="utf-8")
    return corps


def search_corp(name: str) -> tuple:
    """Returns (corp_code, stock_code, matched_name) or (None, None, None)."""
    corps = _get_corp_codes()
    name = name.strip()

    if name in corps:
        d = corps[name]
        return d["corp_code"], d["stock_code"], name

    # Starts-with: prefer shortest (most specific) match
    matches = [(k, v) for k, v in corps.items() if k.startswith(name)]
    if matches:
        best = min(matches, key=lambda x: len(x[0]))
        return best[1]["corp_code"], best[1]["stock_code"], best[0]

    # Contains
    matches = [(k, v) for k, v in corps.items() if name in k]
    if matches:
        best = min(matches, key=lambda x: len(x[0]))
        return best[1]["corp_code"], best[1]["stock_code"], best[0]

    return None, None, None


def get_consensus_target_price(stock_code: str) -> int | None:
    """Fetch average analyst target price from FnGuide. Returns None on failure."""
    if not stock_code:
        return None
    try:
        import json as _json
        url = f"https://comp.fnguide.com/SVO2/json/data/01_06/03_A{stock_code}.json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": f"https://comp.fnguide.com/SVO2/asp/SVD_Consensus.asp?gicode=A{stock_code}",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = _json.loads(resp.content.decode("utf-8-sig"))
        comp = data.get("comp", [])
        if not comp:
            return None
        raw = str(comp[0].get("AVG_PRC", "")).replace(",", "").strip()
        return int(float(raw)) if raw else None
    except Exception:
        return None


def get_current_price(stock_code: str) -> int | None:
    """Fetch latest closing price via pykrx. Returns None on failure."""
    if not stock_code:
        return None
    try:
        from pykrx import stock as krx
        from datetime import datetime, timedelta
        today = datetime.now()
        from_dt = (today - timedelta(days=14)).strftime("%Y%m%d")
        to_dt = today.strftime("%Y%m%d")
        df = krx.get_market_ohlcv_by_date(from_dt, to_dt, stock_code)
        if df is None or df.empty:
            return None
        return int(df["종가"].iloc[-1])
    except Exception:
        return None


def _parse_num(s) -> float:
    s = str(s or "").replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "N/A"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def get_financial_data(corp_code: str, year: str) -> dict:
    """Returns {'equity': float, 'net_income': float} in 백만원, or None."""
    resp = requests.get(
        f"{BASE_URL}/fnlttSinglAcntAll.json",
        params={
            "crtfc_key": DART_API_KEY,
            "corp_code": corp_code,
            "bsns_year": year,
            "reprt_code": "11011",
            "fs_div": "CFS",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        return None

    equity = net_income = None
    for item in data.get("list", []):
        acc_id = item.get("account_id", "")
        acc_nm = item.get("account_nm", "")
        amount = _parse_num(item.get("thstrm_amount"))

        if acc_id in EQUITY_IDS or "지배기업의 소유주에게 귀속되는 자본" in acc_nm:
            if equity is None:
                equity = amount
        if acc_id in INCOME_IDS or (
            "지배기업의 소유주에게 귀속되는" in acc_nm and "당기순이익" in acc_nm
        ):
            if net_income is None:
                net_income = amount

    if equity is None or net_income is None:
        return None

    return {"equity": equity, "net_income": net_income}


def _fetch_income_fields(corp_code: str, year: int, reprt_code: str) -> dict | None:
    """
    Fetches net income amounts for one report in a single API call.
    Returns {"cumul": YTD_amount, "add": single_quarter_amount} or None if report missing.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/fnlttSinglAcntAll.json",
            params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": reprt_code,
                "fs_div": "CFS",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            return None
        for item in data.get("list", []):
            acc_id = item.get("account_id", "")
            acc_nm = item.get("account_nm", "")
            if acc_id in INCOME_IDS or (
                "지배기업의 소유주에게 귀속되는" in acc_nm and "당기순이익" in acc_nm
            ):
                def _val(field):
                    raw = str(item.get(field, "")).strip()
                    return _parse_num(raw) if raw and raw not in ("-", "N/A", "") else None
                return {"cumul": _val("thstrm_amount"), "add": _val("thstrm_add_amount")}
        return None
    except Exception:
        return None


def get_quarterly_income(corp_code: str, start_year: int, max_quarters: int = 4) -> list:
    """
    Returns up to max_quarters quarters (most recent first).
    Each entry: {"year", "label", "income" (individual quarter), "ttm" (TTM net income)}
    TTM = prev_annual - prev_YTD_same_period + curr_YTD
    Falls back to prior years automatically.
    """
    candidates = []
    for yr in range(start_year, start_year - 3, -1):
        for reprt_code, label in [("11014", "3분기"), ("11012", "반기"), ("11013", "1분기")]:
            candidates.append((yr, label, reprt_code))

    # Cache to avoid duplicate API calls for the same (year, reprt_code)
    _cache: dict = {}

    def fetch(year, reprt_code):
        key = (year, reprt_code)
        if key not in _cache:
            _cache[key] = _fetch_income_fields(corp_code, year, reprt_code)
        return _cache[key]

    results = []
    for yr, label, reprt_code in candidates:
        if len(results) >= max_quarters:
            break

        curr = fetch(yr, reprt_code)
        if curr is None or curr["cumul"] is None:
            continue

        # Individual quarter: Q1 report has no separate "add" field (cumul == individual)
        income = curr["cumul"] if reprt_code == "11013" else curr["add"]

        # TTM = prev_annual - prev_YTD_same_period + curr_YTD
        prev_annual = fetch(yr - 1, "11011")
        prev_same = fetch(yr - 1, reprt_code)

        ttm = None
        if (prev_annual and prev_annual["cumul"] is not None
                and prev_same and prev_same["cumul"] is not None):
            ttm = prev_annual["cumul"] - prev_same["cumul"] + curr["cumul"]

        results.append({"year": yr, "label": label, "income": income, "ttm": ttm})

    return results


def get_share_count(corp_code: str, year: str) -> int:
    """Returns total issued common shares, or None."""
    resp = requests.get(
        f"{BASE_URL}/stockTotqySttus.json",
        params={
            "crtfc_key": DART_API_KEY,
            "corp_code": corp_code,
            "bsns_year": year,
            "reprt_code": "11011",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        return None

    items = data.get("list", [])

    def parse_shares(item):
        v = str(item.get("istc_totqy", "0")).replace(",", "").strip()
        if v in ("", "-"):
            return 0
        try:
            return int(float(v))
        except ValueError:
            return 0

    for item in items:
        if "보통주" in item.get("se", ""):
            n = parse_shares(item)
            if n > 0:
                return n

    for item in items:
        if "합계" in item.get("se", ""):
            n = parse_shares(item)
            if n > 0:
                return n

    for item in items:
        n = parse_shares(item)
        if n > 0:
            return n

    return None
