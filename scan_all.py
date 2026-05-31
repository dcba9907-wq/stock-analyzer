"""
코스피 전종목 S-RIM 배치 스캔
실행: python scan_all.py
"""

import io
import json
import os
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import requests
from dotenv import load_dotenv

import FinanceDataReader as fdr
import pandas as pd

from calculator import calculate_srim
from scorer import compute_scores
from universe_snapshot import save_snapshot

load_dotenv()

DART_API_KEY = os.getenv("DART_API_KEY", "")
BASE_URL = "https://opendart.fss.or.kr/api"
RESULTS_FILE = Path(__file__).parent / "scan_results.json"
ERRORS_FILE = Path(__file__).parent / "scan_errors.json"

MIN_MARKET_CAP = 500_000_000_000  # 5,000억원

# 완전 제외 업종 키워드 (sector 이름에 포함된 경우 스킵)
EXCLUDE_KEYWORDS = [
    # 금융
    "은행", "보험", "증권", "금융", "캐피탈", "저축", "카드", "자산운용", "투자",
    # 유틸리티/공기업
    "전기", "가스", "수도", "열에너지", "집단에너지",
    # 지주
    "지주", "홀딩스",
    # 건설
    "건설", "건축", "토건", "주택",
]

# 경기민감 업종 — 포함하되 경고 표시
CYCLICAL_KEYWORDS = ["조선", "해운", "항공", "운송"]

# 재무 필터 기준
MAX_DEBT_RATIO = 200.0   # 부채비율 상한 (%)
MIN_ROE_YEARS = 3        # 연속 흑자 확인 연수


# ─── DART 계정과목 ID ─────────────────────────────────────────────────────────

EQUITY_IDS         = {"ifrs-full_EquityAttributableToOwnersOfParent"}
INCOME_IDS         = {"ifrs-full_ProfitLossAttributableToOwnersOfParent"}
TOTAL_LIAB_IDS     = {"ifrs-full_Liabilities"}
TOTAL_EQUITY_IDS   = {"ifrs-full_Equity"}


# ─── KIND 업종 매핑 ───────────────────────────────────────────────────────────

def _get_kind_sector_map() -> dict:
    cache = Path(__file__).parent / ".kind_sector_cache.json"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 86400:
        return json.loads(cache.read_text(encoding="utf-8"))

    print("KRX KIND 업종 정보 다운로드 중...")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(
            "https://kind.krx.co.kr/corpgeneral/corpList.do",
            params={"method": "download", "searchType": 13},
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        df = pd.read_html(StringIO(resp.content.decode("euc-kr", errors="replace")))[0]

        mapping = {}
        for _, row in df.iterrows():
            code = str(row.get("종목코드", "")).strip().zfill(6)
            sector = str(row.get("업종", "")).strip()
            market = str(row.get("시장구분", "")).strip()
            if code and sector:
                mapping[code] = {"sector": sector, "market": market}

        cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        print(f"  → {len(mapping)}개 종목 업종 매핑 완료")
        return mapping
    except Exception as e:
        print(f"  KIND 업종 조회 실패: {e} — 업종 필터링 없이 진행")
        return {}


# ─── DART 기업코드 매핑 ────────────────────────────────────────────────────────

def _get_stock_code_map() -> dict:
    cache = Path(__file__).parent / ".stock_code_map_cache.json"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 86400:
        return json.loads(cache.read_text(encoding="utf-8"))

    if not DART_API_KEY:
        raise RuntimeError("DART_API_KEY가 설정되지 않았습니다.")

    print("DART 기업코드 XML 다운로드 중...")
    resp = requests.get(
        f"{BASE_URL}/corpCode.xml",
        params={"crtfc_key": DART_API_KEY},
        timeout=60,
    )
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_bytes)
    mapping = {}
    for item in root.findall(".//list"):
        stock = item.findtext("stock_code", "").strip()
        code  = item.findtext("corp_code", "").strip()
        name  = item.findtext("corp_name", "").strip()
        if stock and code:
            mapping[stock] = {"corp_code": code, "corp_name": name}

    cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    print(f"  → {len(mapping)}개 종목 DART 매핑 완료")
    return mapping


# ─── DART 재무 데이터 ─────────────────────────────────────────────────────────

def _parse_num(s) -> float:
    s = str(s or "").replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "N/A"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def get_financial_data_extended(corp_code: str, year: str) -> dict | None:
    """
    Returns {equity, net_income, total_liabilities, total_equity} or None.
    equity / net_income: 지배주주지분 기준 (연결재무제표)
    total_liabilities / total_equity: 연결 전체 기준 (부채비율 계산용)
    """
    try:
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
    except Exception:
        return None

    equity = net_income = total_liabilities = total_equity = None

    for item in data.get("list", []):
        acc_id = item.get("account_id", "")
        acc_nm = item.get("account_nm", "").strip()
        amount = _parse_num(item.get("thstrm_amount"))

        if equity is None and (
            acc_id in EQUITY_IDS
            or "지배기업의 소유주에게 귀속되는 자본" in acc_nm
        ):
            equity = amount

        if net_income is None and (
            acc_id in INCOME_IDS
            or ("지배기업의 소유주에게 귀속되는" in acc_nm and "당기순이익" in acc_nm)
        ):
            net_income = amount

        if total_liabilities is None and (
            acc_id in TOTAL_LIAB_IDS
            or acc_nm in ("부채총계", "부채 합계", "총부채")
        ):
            total_liabilities = amount

        if total_equity is None and (
            acc_id in TOTAL_EQUITY_IDS
            or acc_nm in ("자본총계", "자본 합계", "총자본")
        ):
            total_equity = amount

    if equity is None or net_income is None:
        return None

    return {
        "equity": equity,
        "net_income": net_income,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
    }


def get_share_count(corp_code: str, year: str) -> int | None:
    try:
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
    except Exception:
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


# ─── 모멘텀 수집 ─────────────────────────────────────────────────────────────

def get_momentum_map(tickers: list[str]) -> dict:
    """
    FDR로 종목별 6개월/12개월 수익률 수집.
    {ticker: {mom_6m, mom_12m}}
    """
    today = datetime.now()
    start = (today - timedelta(days=370)).strftime("%Y-%m-%d")
    end   = today.strftime("%Y-%m-%d")
    d6m   = (today - timedelta(days=183)).strftime("%Y-%m-%d")
    d12m  = (today - timedelta(days=365)).strftime("%Y-%m-%d")

    result = {}
    ok = 0
    print(f"FDR 모멘텀 데이터 수집 중 ({len(tickers)}개)...")
    for ticker in tickers:
        try:
            df = fdr.DataReader(ticker, start, end)
            if df is None or df.empty:
                result[ticker] = {"mom_6m": None, "mom_12m": None}
                continue

            p_now = float(df["Close"].iloc[-1])

            def _closest_close(target: str) -> float | None:
                idx = df.index.searchsorted(target)
                if idx >= len(df):
                    idx = len(df) - 1
                if idx < 0:
                    return None
                return float(df["Close"].iloc[idx])

            p6  = _closest_close(d6m)
            p12 = _closest_close(d12m)

            result[ticker] = {
                "mom_6m":  round((p_now / p6  - 1) * 100, 2) if p6  and p6  > 0 else None,
                "mom_12m": round((p_now / p12 - 1) * 100, 2) if p12 and p12 > 0 else None,
            }
            ok += 1
        except Exception:
            result[ticker] = {"mom_6m": None, "mom_12m": None}
        time.sleep(0.1)

    print(f"  → {ok}/{len(tickers)}개 모멘텀 수집 완료")
    return result


# ─── 코스피 종목 필터링 ────────────────────────────────────────────────────────

def get_kospi_tickers_filtered(sector_map: dict) -> tuple[list[dict], dict]:
    """필터링된 코스피 종목 목록 반환. (results, counter_dict)"""
    print("코스피 종목 목록 조회 중 (FinanceDataReader)...")
    df = fdr.StockListing("KOSPI")
    if df is None or df.empty:
        raise RuntimeError("FinanceDataReader 코스피 종목 조회 실패")

    cnt = {"total": len(df), "cap": 0, "sector": 0, "status": 0}
    results = []

    for _, row in df.iterrows():
        ticker  = str(row.get("Code", "")).strip().zfill(6)
        name    = str(row.get("Name", ticker)).strip()
        marcap  = row.get("Marcap", 0) or 0
        close   = int(row.get("Close", 0) or 0)

        if marcap < MIN_MARKET_CAP:
            cnt["cap"] += 1
            continue
        if close == 0:
            cnt["status"] += 1
            continue

        sector_info = sector_map.get(ticker, {})
        sector = sector_info.get("sector", "")

        if any(kw in sector for kw in EXCLUDE_KEYWORDS):
            cnt["sector"] += 1
            continue

        cyclical = any(kw in sector for kw in CYCLICAL_KEYWORDS)

        results.append({
            "ticker": ticker,
            "name": name,
            "market_cap": int(marcap),
            "current_price": close,
            "sector": sector,
            "cyclical_warning": cyclical,
        })

    print(
        f"  → 전체: {cnt['total']}개 | 시가총액미달: {cnt['cap']} | "
        f"업종제외: {cnt['sector']} | 거래정지: {cnt['status']} | 대상: {len(results)}개"
    )
    return results, cnt


# ─── 메인 스캔 ────────────────────────────────────────────────────────────────

def scan_all():
    if not DART_API_KEY:
        print("오류: DART_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
        sys.exit(1)

    scan_dt  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    year     = str(datetime.now().year - 1)   # 전년도 연간 기준
    w        = 0.2
    req_ret  = 5.0

    print(f"\n{'='*60}")
    print(f"S-RIM 전종목 스캔 시작: {scan_dt}")
    print(f"기준연도: {year}  요구수익률: {req_ret}%  w: {w}")
    print(f"{'='*60}\n")

    sector_map           = _get_kind_sector_map()
    tickers, phase1_cnt  = get_kospi_tickers_filtered(sector_map)
    saved_quarter        = save_snapshot(tickers, scan_dt)
    stock_map            = _get_stock_code_map()
    momentum_map         = get_momentum_map([s["ticker"] for s in tickers])

    total = len(tickers)
    results = []
    errors  = []

    # 재무필터 카운터
    cnt_dart_miss    = 0
    cnt_fin_fail     = 0
    cnt_equity_neg   = 0
    cnt_debt_ratio   = 0
    cnt_roe_hist     = 0
    cnt_analyzed     = 0

    for idx, stock in enumerate(tickers, 1):
        ticker = stock["ticker"]
        name   = stock["name"]
        pct    = idx / total * 100

        dart_info = stock_map.get(ticker)
        if not dart_info:
            cnt_dart_miss += 1
            errors.append({"ticker": ticker, "name": name, "reason": "DART corp_code 매핑 없음"})
            print(f"[{idx:4d}/{total}] {name} 건너뜀 — DART 매핑 없음")
            continue

        corp_code = dart_info["corp_code"]

        try:
            # ── 1. 최근연도 재무데이터 (주 연도 → 전년 fallback) ──────────────────
            time.sleep(0.5)
            fin_primary = get_financial_data_extended(corp_code, year)
            base_year = year
            if not fin_primary:
                time.sleep(0.3)
                base_year = str(int(year) - 1)
                fin_primary = get_financial_data_extended(corp_code, base_year)
                if not fin_primary:
                    cnt_fin_fail += 1
                    raise ValueError(f"{year}년 재무데이터 없음")

            # ── 2. 자본잠식 체크 ──────────────────────────────────────────────────
            if fin_primary["equity"] <= 0:
                cnt_equity_neg += 1
                errors.append({"ticker": ticker, "name": name, "reason": "자본잠식 (equity ≤ 0)"})
                print(f"[{idx:4d}/{total}] {name} 제외 — 자본잠식")
                continue

            # ── 3. 부채비율 체크 ─────────────────────────────────────────────────
            tl = fin_primary.get("total_liabilities")
            te = fin_primary.get("total_equity")
            debt_ratio = None
            if tl is not None and te and te > 0:
                debt_ratio = tl / te * 100
                if debt_ratio > MAX_DEBT_RATIO:
                    cnt_debt_ratio += 1
                    errors.append({
                        "ticker": ticker, "name": name,
                        "reason": f"부채비율 {debt_ratio:.0f}% > {MAX_DEBT_RATIO}%"
                    })
                    print(f"[{idx:4d}/{total}] {name} 제외 — 부채비율 {debt_ratio:.0f}%")
                    continue

            # ── 4. 3개년 ROE 연속 흑자 체크 ──────────────────────────────────────
            base_yr_int = int(base_year)
            fin_history = [fin_primary]
            for offset in [1, 2]:
                time.sleep(0.3)
                fd = get_financial_data_extended(corp_code, str(base_yr_int - offset))
                if fd and fd["equity"] and fd["equity"] != 0:
                    fin_history.append(fd)

            roes_history = [
                fd["net_income"] / fd["equity"] * 100
                for fd in fin_history
                if fd["equity"] != 0
            ]
            roe_3yr_positive = len(roes_history) >= MIN_ROE_YEARS and all(r > 0 for r in roes_history[:MIN_ROE_YEARS])

            if not roe_3yr_positive:
                cnt_roe_hist += 1
                available = len(roes_history)
                neg_count = sum(1 for r in roes_history[:MIN_ROE_YEARS] if r <= 0)
                errors.append({
                    "ticker": ticker, "name": name,
                    "reason": f"3개년 연속흑자 미충족 (확인 {available}년 중 적자 {neg_count}년)"
                })
                print(f"[{idx:4d}/{total}] {name} 제외 — 3년 연속흑자 미충족")
                continue

            roe_3yr_avg = round(sum(roes_history[:MIN_ROE_YEARS]) / MIN_ROE_YEARS, 2)

            # ── 5. 주식총수 ───────────────────────────────────────────────────────
            time.sleep(0.3)
            shares = get_share_count(corp_code, base_year)
            if not shares:
                shares = get_share_count(corp_code, str(base_yr_int - 1))
                if not shares:
                    raise ValueError("주식총수 데이터 없음")

            # ── 6. S-RIM 계산 ─────────────────────────────────────────────────────
            calc = calculate_srim(fin_primary["equity"], fin_primary["net_income"], shares, req_ret, w)

            ratio_basic = None
            ratio_w     = None
            if not calc["low_roe"] and calc["fair_basic"] > 0:
                ratio_basic = stock["current_price"] / calc["fair_basic"]
            if not calc["low_roe"] and calc["fair_w"] > 0:
                ratio_w = stock["current_price"] / calc["fair_w"]

            mom = momentum_map.get(ticker, {})
            cnt_analyzed += 1
            results.append({
                "scan_dt":         scan_dt,
                "ticker":          ticker,
                "name":            name,
                "sector":          stock["sector"],
                "cyclical_warning": stock["cyclical_warning"],
                "market_cap":      stock["market_cap"],
                "current_price":   stock["current_price"],
                "equity":          fin_primary["equity"],
                "net_income":      fin_primary["net_income"],
                "roe":             round(calc["roe"], 4),
                "roe_3yr_avg":     roe_3yr_avg,
                "debt_ratio":      round(debt_ratio, 1) if debt_ratio is not None else None,
                "low_roe":         calc["low_roe"],
                "fair_basic":      round(calc["fair_basic"]),
                "fair_w":          round(calc["fair_w"]),
                "ratio_basic":     round(ratio_basic, 4) if ratio_basic is not None else None,
                "ratio_w":         round(ratio_w, 4) if ratio_w is not None else None,
                "mom_6m":          mom.get("mom_6m"),
                "mom_12m":         mom.get("mom_12m"),
            })

            scores = compute_scores(results[-1])
            results[-1].update(scores)

            ratio_str   = f"{ratio_basic:.2f}" if ratio_basic is not None else "N/A"
            dr_str      = f"부채비율={debt_ratio:.0f}%" if debt_ratio is not None else "부채비율=N/A"
            warn_str    = " ⚠️경기민감" if stock["cyclical_warning"] else ""
            print(
                f"[{idx:4d}/{total}] {name} 완료 ({pct:.1f}%)"
                f"  ROE={calc['roe']:.1f}%  3yrROE={roe_3yr_avg:.1f}%"
                f"  {dr_str}  비율={ratio_str}{warn_str}"
            )

        except Exception as e:
            errors.append({"ticker": ticker, "name": name, "reason": str(e)})
            print(f"[{idx:4d}/{total}] {name} 오류: {e}")

    # ── 저장 ──────────────────────────────────────────────────────────────────
    RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ERRORS_FILE.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 요약 출력 ─────────────────────────────────────────────────────────────
    undervalued = sum(
        1 for r in results
        if not r["low_roe"] and r.get("ratio_basic") and r["ratio_basic"] < 0.8 and r["roe"] > req_ret
    )

    print(f"\n{'='*60}")
    print(f"{'스캔 완료':^58}")
    print(f"{'='*60}")
    print(f"  전체 대상    : {total:>4}개  (시총 5천억↑, 거래정지·우선주 제외)")
    print(f"  업종 제외    : {phase1_cnt['sector']:>4}개  (금융·유틸리티·지주·건설)")
    print(f"  DART 미매핑  : {cnt_dart_miss:>4}개")
    print(f"  재무필터 제외: {cnt_equity_neg + cnt_debt_ratio + cnt_roe_hist:>4}개"
          f"  (자본잠식 {cnt_equity_neg} | 부채비율 {cnt_debt_ratio} | ROE연속흑자 {cnt_roe_hist})")
    print(f"  API 오류     : {len(errors) - cnt_dart_miss - cnt_equity_neg - cnt_debt_ratio - cnt_roe_hist:>4}개")
    print(f"  최종 분석    : {cnt_analyzed:>4}개")
    print(f"  저평가 발굴  : {undervalued:>4}개  (ROE>{req_ret}%, 비율<0.8)")
    print(f"{'='*60}")
    print(f"  결과: {RESULTS_FILE}")
    print(f"  오류: {ERRORS_FILE}")
    print(f"  스냅샷: universe_snapshots/{saved_quarter}.json (유니버스 {len(tickers)}개)")
    print(f"{'='*60}")
    print("""
매일 자동실행 설정방법:
crontab -e 입력 후
0 6 * * * cd /Users/jaewonpark/Desktop/취미/stock-analyzer && python scan_all.py
위 줄 추가하면 매일 오전 6시 자동실행됩니다""")


if __name__ == "__main__":
    scan_all()
