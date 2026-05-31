"""유니버스 스냅샷 — 분기별 종목 리스트 저장/비교"""

import json
from datetime import datetime
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).parent / "universe_snapshots"


def get_quarter(dt: datetime) -> str:
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}_Q{q}"


def save_snapshot(tickers: list[dict], scan_dt: str):
    SNAPSHOT_DIR.mkdir(exist_ok=True)

    dt = datetime.strptime(scan_dt, "%Y-%m-%d %H:%M:%S")
    quarter = get_quarter(dt)
    path = SNAPSHOT_DIR / f"{quarter}.json"

    snapshot_entry = {
        "scan_dt": scan_dt,
        "ticker_count": len(tickers),
        "tickers": [
            {
                "ticker": t["ticker"],
                "name": t["name"],
                "sector": t.get("sector", ""),
                "market_cap": t.get("market_cap", 0),
                "current_price": t.get("current_price", 0),
            }
            for t in tickers
        ],
    }

    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data["scans"].append(snapshot_entry)
    else:
        data = {"quarter": quarter, "scans": [snapshot_entry]}

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return quarter


def load_snapshot(quarter: str) -> dict | None:
    path = SNAPSHOT_DIR / f"{quarter}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_snapshots() -> list[str]:
    if not SNAPSHOT_DIR.exists():
        return []
    quarters = [p.stem for p in SNAPSHOT_DIR.glob("*.json")]
    return sorted(quarters)


def compare_universes(q1: str, q2: str) -> dict:
    s1 = load_snapshot(q1)
    s2 = load_snapshot(q2)

    if not s1 or not s2:
        return {"added": [], "removed": [], "maintained": []}

    # 각 분기의 마지막 스캔 기준
    tickers1 = {t["ticker"]: t for t in s1["scans"][-1]["tickers"]}
    tickers2 = {t["ticker"]: t for t in s2["scans"][-1]["tickers"]}

    set1, set2 = set(tickers1), set(tickers2)

    return {
        "added":      [{"ticker": t, "name": tickers2[t]["name"]} for t in sorted(set2 - set1)],
        "removed":    [{"ticker": t, "name": tickers1[t]["name"]} for t in sorted(set1 - set2)],
        "maintained": [{"ticker": t, "name": tickers1[t]["name"]} for t in sorted(set1 & set2)],
    }
