"""멀티팩터 퀀트 스코어 계산 (추가 API 호출 없음)"""


def _clip(v: float) -> float:
    return max(0.0, min(100.0, v))


def _value_score(item: dict) -> float:
    if item.get("low_roe"):
        return 0.0
    ratio = item.get("ratio_basic")
    if ratio is None:
        return 0.0
    if ratio <= 0.3:
        return 100.0
    if ratio >= 0.8:
        return 0.0
    return _clip((0.8 - ratio) / (0.8 - 0.3) * 100)


def _quality_score(item: dict) -> float:
    roe_3yr = item.get("roe_3yr_avg")
    roe_cur = item.get("roe")
    debt = item.get("debt_ratio")

    # ROE 점수: 5%→0, 25%→100
    if roe_3yr is not None:
        roe_score = _clip((roe_3yr - 5.0) / (25.0 - 5.0) * 100)
    else:
        roe_score = 0.0

    # 부채비율 점수: 200%→0, 0%→100 (None이면 50)
    if debt is not None:
        debt_score = _clip((200.0 - debt) / 200.0 * 100)
    else:
        debt_score = 50.0

    # ROE 일관성: |roe - roe_3yr_avg| 차이 0→100, ≥10%→0
    if roe_3yr is not None and roe_cur is not None:
        diff = abs(roe_cur - roe_3yr)
        consistency_score = _clip((10.0 - diff) / 10.0 * 100)
    else:
        consistency_score = 50.0

    return (roe_score + debt_score + consistency_score) / 3.0


def _momentum_score(item: dict) -> float:
    mom_6m = item.get("mom_6m")
    mom_12m = item.get("mom_12m")

    # 6개월 수익률: -20%→0, +40%→100 선형
    if mom_6m is not None:
        s6 = _clip((mom_6m - (-20.0)) / (40.0 - (-20.0)) * 100)
    else:
        s6 = 50.0

    # 12개월 수익률: -30%→0, +60%→100 선형
    if mom_12m is not None:
        s12 = _clip((mom_12m - (-30.0)) / (60.0 - (-30.0)) * 100)
    else:
        s12 = 50.0

    return (s6 + s12) / 2.0


def _size_score(item: dict) -> float:
    cap = item.get("market_cap", 0) or 0
    lo = 500_000_000_000    # 5천억
    hi = 10_000_000_000_000  # 10조
    if cap >= hi:
        return 100.0
    if cap <= lo:
        return 0.0
    return _clip((cap - lo) / (hi - lo) * 100)


def compute_scores(item: dict) -> dict:
    v = _value_score(item)
    q = _quality_score(item)
    m = _momentum_score(item)
    s = _size_score(item)
    total = round(v * 0.35 + q * 0.35 + m * 0.20 + s * 0.10, 1)
    return {
        "value_score":    round(v, 1),
        "quality_score":  round(q, 1),
        "momentum_score": round(m, 1),
        "size_score":     round(s, 1),
        "total_score":    total,
    }
