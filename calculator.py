def calculate_srim(
    equity: float,
    net_income: float,
    shares: int,
    required_return: float,
    w: float,
) -> dict:
    """
    S-RIM (Sustainable Rate of Income Model) valuation.

    equity, net_income: amounts in 원 (as returned by DART)
    required_return: required rate of return in % (e.g. 7.78)
    w: excess-return fade factor, 0.0 ~ 0.5
    """
    roe = net_income / equity * 100  # %

    low_roe = roe < required_return

    if low_roe:
        # 초과이익 음수: 순자산가치(BPS)로 표시
        nav = equity / shares
        return {
            "roe": roe,
            "low_roe": True,
            "ev_basic": equity,
            "fair_basic": nav,
            "ev_w": equity,
            "fair_w": nav,
        }

    # Basic fair value
    ev_basic = equity + equity * (roe - required_return) / required_return
    fair_basic = ev_basic / shares

    # Conservative fair value with excess-return fade
    fade = 1 - w  # persistence factor
    r_d = required_return / 100  # decimal
    roe_d = roe / 100  # decimal
    denom = 1 + r_d - fade  # = r_d + w
    if denom > 0:
        ev_w = equity + equity * (roe_d - r_d) * fade / denom
    else:
        ev_w = equity
    fair_w = ev_w / shares

    if fair_w > fair_basic:
        print(f"WARNING: fair_w ({fair_w:.0f}) > fair_basic ({fair_basic:.0f}) — 공식 검토 필요")

    return {
        "roe": roe,
        "low_roe": False,
        "ev_basic": ev_basic,
        "fair_basic": fair_basic,
        "ev_w": ev_w,
        "fair_w": fair_w,
    }
