"""GOAI 期权自研定价引擎。

项目铁律：所有 Greeks / IV / 情景损益只能由本引擎产出，禁止 LLM 估算数值。
- 欧式期权：Black-Scholes 解析解
- 美式期权：二叉树（港股个股期权默认美式、可提前行权）
- IV：二分法按市场价迭代求解
- Greeks：bump-and-reprice（价格对 S / sigma / T / r 数值差分）

价格单位：每股（港币）。张数层面的金额 = 每股价格 x 合约乘数（0700 默认 100）。
"""

from __future__ import annotations

import math
from typing import Callable, Dict


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
) -> float:
    """欧式 Black-Scholes 价格（option_type: CALL / PUT）。"""
    if T <= 0:
        intrinsic = max(S - K, 0.0) if option_type == "CALL" else max(K - S, 0.0)
        return intrinsic
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    df_s = math.exp(-q * T)
    df_r = math.exp(-r * T)
    if option_type == "CALL":
        return S * df_s * norm_cdf(d1) - K * df_r * norm_cdf(d2)
    return K * df_r * norm_cdf(-d2) - S * df_s * norm_cdf(-d1)


def binomial_american(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
    steps: int = 300,
) -> float:
    """美式期权二叉树价格（支持提前行权）。"""
    if T <= 0:
        return max(S - K, 0.0) if option_type == "CALL" else max(K - S, 0.0)
    dt = T / steps
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    p_up = (math.exp((r - q) * dt) - d) / (u - d)
    p_up = min(max(p_up, 0.0), 1.0)
    p_down = 1.0 - p_up
    disc = math.exp(-r * dt)

    values = [0.0] * (steps + 1)
    for i in range(steps + 1):
        price = S * (u ** i) * (d ** (steps - i))
        if option_type == "CALL":
            values[i] = max(price - K, 0.0)
        else:
            values[i] = max(K - price, 0.0)

    for j in range(steps - 1, -1, -1):
        for i in range(j + 1):
            price = S * (u ** i) * (d ** (j - i))
            hold = disc * (p_up * values[i + 1] + p_down * values[i])
            if option_type == "CALL":
                values[i] = max(hold, price - K)
            else:
                values[i] = max(hold, K - price)
    return values[0]


def price(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
    american: bool = True,
    steps: int = 300,
) -> float:
    if american:
        return binomial_american(S, K, T, r, q, sigma, option_type, steps)
    return black_scholes(S, K, T, r, q, sigma, option_type)


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    option_type: str,
    american: bool = True,
    steps: int = 300,
    lo: float = 0.0001,
    hi: float = 5.0,
) -> float:
    """二分法由市场价反解 IV（百分比小数，如 0.42 表示 42%）。"""
    if T <= 0 or market_price <= 0:
        return 0.0

    def f(sig: float) -> float:
        return price(S, K, T, r, q, sig, option_type, american, steps) - market_price

    if f(lo) >= 0:
        return lo
    if f(hi) <= 0:
        return hi
    for _ in range(120):
        mid = 0.5 * (lo + hi)
        val = f(mid)
        if abs(val) < 1e-10:
            return mid
        if val > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
    american: bool = True,
    steps: int = 500,
) -> Dict[str, float]:
    """bump-and-reprice 计算 Greeks（每股口径）。"""
    price_fn: Callable[[float, float, float, float], float] = lambda ss, tt, rr, sg: price(
        ss, K, tt, rr, q, sg, option_type, american, steps
    )
    v0 = price_fn(S, T, r, sigma)

    h_s = max(S * 0.005, 0.05)
    delta = (price_fn(S + h_s, T, r, sigma) - price_fn(S - h_s, T, r, sigma)) / (2 * h_s)
    gamma = (
        price_fn(S + h_s, T, r, sigma)
        - 2 * v0
        + price_fn(S - h_s, T, r, sigma)
    ) / (h_s * h_s)

    h_v = 0.005
    vega = price_fn(S, T, r, sigma + h_v) - price_fn(S, T, r, sigma - h_v)

    dt = 1.0 / 365.0
    t_prev = max(T - dt, 1e-6)
    theta = price_fn(S, t_prev, r, sigma) - v0

    h_r = 0.005
    rho = price_fn(S, T, r + h_r, sigma) - price_fn(S, T, r - h_r, sigma)

    return {
        "price": v0,
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "rho": rho,
    }
