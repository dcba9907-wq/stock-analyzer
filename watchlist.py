"""관심종목 찜하기 — watchlist.json 읽기/쓰기"""

import json
from datetime import datetime
from pathlib import Path

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"


def load_watchlist() -> list:
    if not WATCHLIST_FILE.exists():
        return []
    return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")).get("items", [])


def save_watchlist(items: list):
    WATCHLIST_FILE.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_to_watchlist(ticker: str, name: str, price: int, score, ratio):
    items = load_watchlist()
    if any(i["ticker"] == ticker for i in items):
        return
    items.append({
        "ticker": ticker,
        "name": name,
        "added_dt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "added_price": price,
        "added_score": score,
        "added_ratio": ratio,
    })
    save_watchlist(items)


def remove_from_watchlist(ticker: str):
    items = [i for i in load_watchlist() if i["ticker"] != ticker]
    save_watchlist(items)


def is_in_watchlist(ticker: str) -> bool:
    return any(i["ticker"] == ticker for i in load_watchlist())
