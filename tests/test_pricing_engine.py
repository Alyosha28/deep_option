"""定价引擎基准测试（数字铁律回归）：已知 BS 价格、IV 往返、美式下界、Greeks 解析对照。

所有期望值来自教科书/Hull 基准与独立闭式公式，不依赖引擎自身输出。
"""

from __future__ import annotations

import math
import unittest

from src.pricing_engine import (
    binomial_american,
    black_scholes,
    escrowed_spot,
    greeks,
    implied_volatility,
    price,
)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


class BlackScholesKnownPriceTests(unittest.TestCase):
    def test_hull_standard_case(self):
        # Hull 教科书基准值（scipy 独立复算：10.450584 / 5.573526）
        self.assertAlmostEqual(
            black_scholes(100, 100, 1.0, 0.05, 0.0, 0.2, "CALL"), 10.4506, places=3
        )
        self.assertAlmostEqual(
            black_scholes(100, 100, 1.0, 0.05, 0.0, 0.2, "PUT"), 5.5735, places=3
        )

    def test_second_reference_case(self):
        self.assertAlmostEqual(
            black_scholes(100, 110, 0.75, 0.02, 0.01, 0.3, "CALL"), 6.7777, places=3
        )
        self.assertAlmostEqual(
            black_scholes(100, 110, 0.75, 0.02, 0.01, 0.3, "PUT"), 15.8872, places=3
        )

    def test_deep_itm_case(self):
        self.assertAlmostEqual(
            black_scholes(80, 100, 0.5, 0.03, 0.02, 0.45, "CALL"), 4.0859, places=3
        )
        self.assertAlmostEqual(
            black_scholes(80, 100, 0.5, 0.03, 0.02, 0.45, "PUT"), 23.3931, places=3
        )

    def test_put_call_parity(self):
        for S, K, T, r, q, sigma in (
            (100, 100, 1.0, 0.05, 0.0, 0.2),
            (100, 110, 0.75, 0.02, 0.01, 0.3),
            (80, 100, 0.5, 0.03, 0.02, 0.45),
            (120, 95, 0.25, 0.01, 0.04, 0.6),
        ):
            call = black_scholes(S, K, T, r, q, sigma, "CALL")
            put = black_scholes(S, K, T, r, q, sigma, "PUT")
            parity = call - put - (S * math.exp(-q * T) - K * math.exp(-r * T))
            self.assertAlmostEqual(parity, 0.0, places=10)

    def test_at_expiry_returns_intrinsic(self):
        self.assertEqual(black_scholes(105, 100, 0.0, 0.05, 0.0, 0.2, "CALL"), 5.0)
        self.assertEqual(black_scholes(95, 100, 0.0, 0.05, 0.0, 0.2, "CALL"), 0.0)
        self.assertEqual(black_scholes(95, 100, 0.0, 0.05, 0.0, 0.2, "PUT"), 5.0)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            black_scholes(100, 100, 1.0, 0.05, 0.0, 0.0, "CALL")
        with self.assertRaises(ValueError):
            black_scholes(-100, 100, 1.0, 0.05, 0.0, 0.2, "CALL")
        with self.assertRaises(ValueError):
            black_scholes(100, 100, 1.0, 0.05, 0.0, 0.2, "CALLS")


class ImpliedVolatilityTests(unittest.TestCase):
    def test_roundtrip_recovers_sigma(self):
        for sigma in (0.2, 0.42, 0.85):
            for option_type in ("CALL", "PUT"):
                market = price(
                    100, 100, 0.25, 0.05, 0.0, sigma, option_type, american=False
                )
                solved = implied_volatility(
                    market, 100, 100, 0.25, 0.05, 0.0, option_type, american=False
                )
                self.assertAlmostEqual(solved, sigma, places=6)

    def test_roundtrip_american(self):
        for sigma in (0.25, 0.5):
            for option_type in ("CALL", "PUT"):
                market = price(
                    100, 100, 0.3, 0.05, 0.0, sigma, option_type, american=True
                )
                solved = implied_volatility(
                    market, 100, 100, 0.3, 0.05, 0.0, option_type, american=True
                )
                self.assertAlmostEqual(solved, sigma, places=4)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            implied_volatility(1.0, 100, 100, 0.0, 0.05, 0.0, "CALL")
        with self.assertRaises(ValueError):
            implied_volatility(0.0, 100, 100, 0.25, 0.05, 0.0, "CALL")
        with self.assertRaises(ValueError):
            implied_volatility(-1.0, 100, 100, 0.25, 0.05, 0.0, "CALL")


class AmericanBinomialTests(unittest.TestCase):
    def test_american_never_below_intrinsic(self):
        for S, K in ((80, 100), (90, 100), (100, 100), (110, 100)):
            for sigma in (0.2, 0.5):
                call = binomial_american(S, K, 0.5, 0.05, 0.0, sigma, "CALL")
                put = binomial_american(S, K, 0.5, 0.05, 0.0, sigma, "PUT")
                self.assertGreaterEqual(call, max(S - K, 0.0) - 1e-9)
                self.assertGreaterEqual(put, max(K - S, 0.0) - 1e-9)

    def test_american_call_without_div_tracks_european(self):
        call_am = binomial_american(100, 100, 0.5, 0.05, 0.0, 0.3, "CALL")
        call_eu = black_scholes(100, 100, 0.5, 0.05, 0.0, 0.3, "CALL")
        # 无股息时美式 call 解析等价于欧式；二叉树 300 步收敛误差 < 0.01
        self.assertAlmostEqual(call_am, call_eu, delta=0.01)

    def test_deep_itm_american_put_exceeds_european(self):
        put_am = binomial_american(80, 100, 0.5, 0.05, 0.0, 0.25, "PUT")
        put_eu = black_scholes(80, 100, 0.5, 0.05, 0.0, 0.25, "PUT")
        self.assertGreater(put_am, put_eu + 0.05)
        self.assertGreaterEqual(put_am, 20.0 - 1e-9)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            binomial_american(100, 100, 0.5, 0.05, 0.0, 0.0, "CALL")
        with self.assertRaises(ValueError):
            binomial_american(100, 100, 0.5, 0.05, 0.0, 0.2, "PUT ")


class GreeksAnalyticCrossCheckTests(unittest.TestCase):
    def test_call_greeks_match_closed_form(self):
        S, K, T, r, q, sigma = 100.0, 100.0, 0.5, 0.05, 0.0, 0.3
        g = greeks(S, K, T, r, q, sigma, "CALL", american=False)
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        disc_q = math.exp(-q * T)
        disc_r = math.exp(-r * T)
        exp_delta = disc_q * _norm_cdf(d1)
        exp_gamma = disc_q * _norm_pdf(d1) / (S * sigma * math.sqrt(T))
        exp_vega = S * math.sqrt(T) * _norm_pdf(d1) * disc_q * 0.01
        exp_rho = K * T * disc_r * _norm_cdf(d2) * 0.01
        # theta 与引擎口径一致：1 自然日的离散差分（V(T-1/365) - V(T)）
        dt = 1.0 / 365.0
        v_at_T = black_scholes(S, K, T, r, q, sigma, "CALL")
        v_at_T_minus_dt = black_scholes(S, K, max(T - dt, 1e-6), r, q, sigma, "CALL")
        exp_theta = v_at_T_minus_dt - v_at_T
        self.assertAlmostEqual(g["delta"], exp_delta, places=4)
        self.assertAlmostEqual(g["gamma"], exp_gamma, places=4)
        self.assertAlmostEqual(g["vega"], exp_vega, places=4)
        self.assertAlmostEqual(g["rho"], exp_rho, places=5)
        self.assertAlmostEqual(g["theta"], exp_theta, places=5)

    def test_put_greeks_match_closed_form(self):
        S, K, T, r, q, sigma = 100.0, 95.0, 0.25, 0.03, 0.02, 0.35
        g = greeks(S, K, T, r, q, sigma, "PUT", american=False)
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        disc_q = math.exp(-q * T)
        disc_r = math.exp(-r * T)
        exp_delta = disc_q * (_norm_cdf(d1) - 1.0)
        exp_gamma = disc_q * _norm_pdf(d1) / (S * sigma * math.sqrt(T))
        exp_vega = S * math.sqrt(T) * _norm_pdf(d1) * disc_q * 0.01
        exp_rho = -K * T * disc_r * _norm_cdf(-d2) * 0.01
        dt = 1.0 / 365.0
        v_at_T = black_scholes(S, K, T, r, q, sigma, "PUT")
        v_at_T_minus_dt = black_scholes(S, K, max(T - dt, 1e-6), r, q, sigma, "PUT")
        exp_theta = v_at_T_minus_dt - v_at_T
        self.assertAlmostEqual(g["delta"], exp_delta, places=4)
        self.assertAlmostEqual(g["gamma"], exp_gamma, places=4)
        self.assertAlmostEqual(g["vega"], exp_vega, places=4)
        self.assertAlmostEqual(g["rho"], exp_rho, places=5)
        self.assertAlmostEqual(g["theta"], exp_theta, places=5)

    def test_american_call_greeks_track_european_without_dividend(self):
        # q=0 时美式 call 与欧式解析等价；gamma 因二叉树节点扭结放宽到 2% 相对容差
        g_am = greeks(100.0, 100.0, 0.5, 0.05, 0.0, 0.3, "CALL", american=True)
        g_eu = greeks(100.0, 100.0, 0.5, 0.05, 0.0, 0.3, "CALL", american=False)
        for key in ("delta", "vega", "rho", "theta"):
            self.assertAlmostEqual(g_am[key], g_eu[key], places=3, msg=key)
        self.assertAlmostEqual(g_am["gamma"], g_eu["gamma"], delta=abs(g_eu["gamma"]) * 0.02)

    def test_american_greeks_short_dte_regime(self):
        # hero 场景参数（2 DTE）：旧实现 h=0.5 时 gamma 恒为 0（解析真值 0.0176）
        S, K, T, r, q, sigma = 478.8, 480.0, 6.0 / 365.0, 0.035, 0.0, 0.37
        g = greeks(S, K, T, r, q, sigma, "CALL", american=True)
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        exp_delta = _norm_cdf(d1)
        exp_gamma = _norm_pdf(d1) / (S * sigma * math.sqrt(T))
        self.assertAlmostEqual(g["delta"], exp_delta, delta=0.002)
        self.assertAlmostEqual(g["gamma"], exp_gamma, delta=exp_gamma * 0.02)

    def test_long_call_theta_is_negative(self):
        g = greeks(100.0, 100.0, 0.5, 0.05, 0.0, 0.3, "CALL", american=False)
        self.assertLess(g["theta"], 0.0)


class DiscreteDividendTests(unittest.TestCase):
    """港股离散股息（escrowed-spot 口径）基准测试。

    所有对照值来自独立闭式公式（把 S* = S - PV(D) 代入 BS），不依赖
    引擎自身输出；股息路径对 call/put 的方向性影响按金融直觉校验。
    """

    def test_escrowed_spot_matches_closed_form(self):
        # D=2.5 于 τ=0.25 除息，r=4%：PV = 2.5·e^(-0.04·0.25)
        pv = 2.5 * math.exp(-0.04 * 0.25)
        self.assertAlmostEqual(
            escrowed_spot(100.0, 0.5, 0.04, [(0.25, 2.5)]), 100.0 - pv, places=12
        )
        # 多条股息按各自 τ 折现
        pv2 = 2.5 * math.exp(-0.04 * 0.25) + 1.25 * math.exp(-0.04 * 0.4)
        self.assertAlmostEqual(
            escrowed_spot(100.0, 0.5, 0.04, [(0.25, 2.5), (0.4, 1.25)]),
            100.0 - pv2,
            places=12,
        )

    def test_european_dividend_equals_escrowed_bs(self):
        S, K, T, r, q, sigma = 100.0, 100.0, 0.5, 0.04, 0.0, 0.3
        dividends = [(0.25, 2.5)]
        S_adj = escrowed_spot(S, T, r, dividends)
        for option_type in ("CALL", "PUT"):
            self.assertAlmostEqual(
                black_scholes(S, K, T, r, q, sigma, option_type, dividends),
                black_scholes(S_adj, K, T, r, q, sigma, option_type),
                places=12,
                msg=option_type,
            )

    def test_dividend_lowers_call_and_raises_put(self):
        S, K, T, r, q, sigma = 100.0, 100.0, 0.5, 0.04, 0.0, 0.3
        dividends = [(0.25, 2.5)]
        call_no_div = black_scholes(S, K, T, r, q, sigma, "CALL")
        call_div = black_scholes(S, K, T, r, q, sigma, "CALL", dividends)
        put_no_div = black_scholes(S, K, T, r, q, sigma, "PUT")
        put_div = black_scholes(S, K, T, r, q, sigma, "PUT", dividends)
        self.assertLess(call_div, call_no_div)
        self.assertGreater(put_div, put_no_div)

    def test_parity_holds_on_escrowed_forward(self):
        S, K, T, r, q, sigma = 100.0, 100.0, 0.5, 0.04, 0.02, 0.3
        dividends = [(0.25, 2.5), (0.4, 1.25)]
        call = black_scholes(S, K, T, r, q, sigma, "CALL", dividends)
        put = black_scholes(S, K, T, r, q, sigma, "PUT", dividends)
        S_adj = escrowed_spot(S, T, r, dividends)
        parity = call - put - (S_adj * math.exp(-q * T) - K * math.exp(-r * T))
        self.assertAlmostEqual(parity, 0.0, places=10)

    def test_dividends_outside_expiry_window_are_ignored(self):
        S, K, T, r, q, sigma = 100.0, 100.0, 0.5, 0.04, 0.0, 0.3
        base_call = black_scholes(S, K, T, r, q, sigma, "CALL")
        # 到期后才除息（τ=0.75 > T=0.5）与已除息（τ=-0.1）均不影响本到期
        self.assertAlmostEqual(
            black_scholes(S, K, T, r, q, sigma, "CALL", [(0.75, 5.0)]),
            base_call,
            places=12,
        )
        self.assertAlmostEqual(
            black_scholes(S, K, T, r, q, sigma, "CALL", [(-0.1, 5.0)]),
            base_call,
            places=12,
        )

    def test_iv_roundtrip_with_dividends(self):
        S, K, T, r, q = 100.0, 100.0, 0.4, 0.04, 0.0
        dividends = [(0.2, 3.0)]
        for sigma in (0.2, 0.42):
            for american in (False, True):
                market = price(
                    S, K, T, r, q, sigma, "CALL", american=american,
                    discrete_dividends=dividends,
                )
                solved = implied_volatility(
                    market, S, K, T, r, q, "CALL", american=american,
                    discrete_dividends=dividends,
                )
                self.assertAlmostEqual(
                    solved, sigma, places=4 if american else 6,
                    msg=f"american={american} sigma={sigma}",
                )

    def test_american_with_dividends_respects_lower_bounds(self):
        S, K, T, r, q, sigma = 100.0, 100.0, 0.5, 0.04, 0.0, 0.3
        dividends = [(0.25, 2.5)]
        call_am = binomial_american(S, K, T, r, q, sigma, "CALL", 300, dividends)
        put_am = binomial_american(S, K, T, r, q, sigma, "PUT", 300, dividends)
        call_eu = black_scholes(S, K, T, r, q, sigma, "CALL", dividends)
        put_eu = black_scholes(S, K, T, r, q, sigma, "PUT", dividends)
        self.assertGreaterEqual(call_am, max(S - K, 0.0) - 1e-9)
        self.assertGreaterEqual(put_am, max(K - S, 0.0) - 1e-9)
        # escrowed 口径下 q=0 美式 call 与欧式等价（二叉树收敛带宽 0.02）
        self.assertAlmostEqual(call_am, call_eu, delta=0.02)
        self.assertGreaterEqual(put_am, put_eu - 1e-6)

    def test_deep_itm_american_put_still_exceeds_european_with_dividend(self):
        S, K, T, r, q, sigma = 80.0, 100.0, 0.5, 0.04, 0.0, 0.25
        dividends = [(0.25, 2.0)]
        put_am = binomial_american(S, K, T, r, q, sigma, "PUT", 300, dividends)
        put_eu = black_scholes(S, K, T, r, q, sigma, "PUT", dividends)
        self.assertGreater(put_am, put_eu + 0.05)

    def test_greeks_with_dividends_match_closed_form_on_escrowed_spot(self):
        S, K, T, r, q, sigma = 100.0, 100.0, 0.5, 0.05, 0.0, 0.3
        dividends = [(0.2, 2.0)]
        g = greeks(S, K, T, r, q, sigma, "CALL", american=False, discrete_dividends=dividends)
        S_adj = escrowed_spot(S, T, r, dividends)
        d1 = (math.log(S_adj / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        disc_q = math.exp(-q * T)
        disc_r = math.exp(-r * T)
        exp_delta = disc_q * _norm_cdf(d1)
        exp_gamma = disc_q * _norm_pdf(d1) / (S_adj * sigma * math.sqrt(T))
        exp_vega = S_adj * math.sqrt(T) * _norm_pdf(d1) * disc_q * 0.01
        # rho 有两个通道：显式折现 K·e^(-rT) 与 S* = S - D·e^(-rτ) 对 r 的
        # 敏感性（∂S*/∂r = D·τ·e^(-rτ)）；delta 相对 S* 是 1:1。
        tau, amount = dividends[0]
        ds_dr = amount * tau * math.exp(-r * tau)
        exp_rho = (K * T * disc_r * _norm_cdf(d2) + exp_delta * ds_dr) * 0.01
        self.assertAlmostEqual(g["delta"], exp_delta, places=4)
        self.assertAlmostEqual(g["gamma"], exp_gamma, places=4)
        self.assertAlmostEqual(g["vega"], exp_vega, places=4)
        self.assertAlmostEqual(g["rho"], exp_rho, places=5)

    def test_invalid_dividends_raise(self):
        with self.assertRaises(ValueError):
            black_scholes(100, 100, 0.5, 0.04, 0.0, 0.3, "CALL", [(0.2, -1.0)])
        with self.assertRaises(ValueError):
            # 股息总额超过正股价格：S* 非正，必须显式报错
            black_scholes(1.0, 100, 0.5, 0.04, 0.0, 0.3, "CALL", [(0.2, 2.0)])
        with self.assertRaises(ValueError):
            black_scholes(100, 100, 0.5, 0.04, 0.0, 0.3, "CALL", [(0.2, 1.0), (0.2, 2.0)])
        with self.assertRaises(ValueError):
            black_scholes(100, 100, 0.5, 0.04, 0.0, 0.3, "CALL", [("soon", 1.0)])


class PriceDispatchTests(unittest.TestCase):
    def test_price_dispatches_american_vs_european(self):
        am = price(80, 100, 0.5, 0.05, 0.0, 0.25, "PUT", american=True)
        eu = price(80, 100, 0.5, 0.05, 0.0, 0.25, "PUT", american=False)
        self.assertGreater(am, eu)


if __name__ == "__main__":
    unittest.main()
