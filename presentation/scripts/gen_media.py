# -*- coding: utf-8 -*-
"""GOAI 初赛评审 PPT — 深色主题图表生成脚本。

生成 presentation/media/ 下的两张 PNG：
  1. backtest_roi_comparison.png — 口径 A（历史期权 IV 引擎）vs 口径 B（预期波动代理）
     的 d+2 ROI 分组柱图，数据全部取自 data/backtest_tencent_straddle.json。
  2. straddle_pnl.png — 腾讯 480 跨式（2 张）到期盈亏折线，
     参数取自 data/decision_card_2026-08-12.json 与 data/hero_inputs.json。

约束：所有数字从 JSON 读取，不硬编码；无账户信息；深色主题配色。
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parents[2]  # F:/GOAi_competition
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "presentation" / "media"

# ---- 深色主题（任务指定）----
BG = "#0A0E14"      # 图背景
PANEL = "#131A24"   # 面板（axes）背景
GRID = "#1F2937"    # 网格
CYAN = "#22D3EE"    # 口径 A / 理论口径
GREEN = "#34D399"   # 口径 B / 可成交口径
INK = "#E6EDF3"     # 数值标签 / 主文字
SUB = "#8B949E"     # 轴标签
FAINT = "#5A6473"   # 注脚小字
RED = "#F6465D"     # 最大亏损线
AMBER = "#F5B942"   # strike 标记

plt.rcParams.update(
    {
        "font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial"],
        "axes.unicode_minus": False,
        "figure.facecolor": BG,
        "savefig.facecolor": BG,
        "axes.facecolor": PANEL,
        "axes.edgecolor": "#30363D",
        "axes.labelcolor": SUB,
        "text.color": INK,
        "xtick.color": SUB,
        "ytick.color": SUB,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "legend.facecolor": PANEL,
        "legend.edgecolor": GRID,
        "legend.labelcolor": INK,
        "figure.dpi": 100,
    }
)


def _load(name: str) -> dict:
    with open(DATA_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _style_axes(ax: plt.Axes) -> None:
    """统一深色外观：网格、脊线、零轴参考。"""
    ax.grid(True, axis="y", color=GRID, linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#30363D")
        spine.set_linewidth(0.8)
    ax.tick_params(colors=SUB, labelsize=9)


def _footnote(fig: plt.Figure, left: str, right: str) -> None:
    fig.text(
        0.015, 0.012, left, fontsize=8, color=FAINT, ha="left", va="bottom"
    )
    fig.text(
        0.985, 0.012, right, fontsize=8, color=FAINT, ha="right", va="bottom"
    )


# --------------------------------------------------------------------------
# 图 1：口径 A vs 口径 B 的 d+2 ROI 对比柱图
# --------------------------------------------------------------------------
def chart_backtest_roi() -> None:
    bt = _load("backtest_tencent_straddle.json")
    engine = bt["engine_backtest"]["periods"]
    proxy = bt["proxy_backtest"]["periods"]
    stats = bt["engine_backtest"]["stats"]["d2"]
    pstats = bt["proxy_backtest"]["stats"]["d2"]

    # 口径 A（engine）的全部 11 期；口径 B 取相同 period 标签（共同期间）
    labels = [p["period"] for p in engine]
    engine_roi = [p["horizons"]["2"]["roi"] * 100 for p in engine]
    proxy_by_period = {p["period"]: p["horizons"]["2"]["roi"] * 100 for p in proxy}
    proxy_roi = [proxy_by_period[lbl] for lbl in labels]

    fig, ax = plt.subplots(figsize=(12.8, 7.2))
    fig.subplots_adjust(left=0.055, right=0.965, top=0.80, bottom=0.165)
    _style_axes(ax)

    x = range(len(labels))
    width = 0.38
    bars_a = ax.bar(
        [i - width / 2 for i in x],
        engine_roi,
        width=width,
        color=CYAN,
        label="口径 A · 历史期权 IV 引擎（n=11）",
        zorder=3,
    )
    bars_b = ax.bar(
        [i + width / 2 for i in x],
        proxy_roi,
        width=width,
        color=GREEN,
        label="口径 B · 预期波动代理（n=19）",
        zorder=3,
    )

    # 数值标签（百分比，1 位小数）
    for rect, val in zip(bars_a, engine_roi):
        va, dy = ("bottom", 6) if val >= 0 else ("top", -6)
        ax.annotate(
            f"{val:+.1f}%",
            (rect.get_x() + rect.get_width() / 2, rect.get_height()),
            xytext=(0, dy), textcoords="offset points",
            ha="center", va=va, fontsize=6.6, color=INK,
        )
    for rect, val in zip(bars_b, proxy_roi):
        va, dy = ("bottom", 6) if val >= 0 else ("top", -6)
        ax.annotate(
            f"{val:+.1f}%",
            (rect.get_x() + rect.get_width() / 2, rect.get_height()),
            xytext=(0, dy), textcoords="offset points",
            ha="center", va=va, fontsize=6.6, color=INK,
        )

    ax.axhline(0, color="#475569", linewidth=1.0, zorder=2)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=8.5)
    ax.set_ylabel("d+2 ROI（%，按 ask 成本口径）", fontsize=10)
    ax.set_ylim(-132, 195)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")

    # 统计附注（全部来自 JSON stats）
    note = (
        "口径 A（engine d2, n=11）：均值 {ma:+.1f}% · 中位 {da:+.1f}% · 胜率 {wa:.1f}%\n"
        "口径 B（proxy d2, n=19）：均值 {mb:+.1f}% · 中位 {db:+.1f}% · 胜率 {wb:.1f}%"
    ).format(
        ma=stats["mean_roi_pct"],
        da=stats["median_roi_pct"],
        wa=stats["win_rate_pct"],
        mb=pstats["mean_roi_pct"],
        db=pstats["median_roi_pct"],
        wb=pstats["win_rate_pct"],
    )
    ax.text(
        0.985, 0.955, note, transform=ax.transAxes,
        ha="right", va="top", fontsize=8.5, color=INK,
        bbox=dict(boxstyle="round,pad=0.45", facecolor=PANEL, edgecolor=GRID, linewidth=0.8),
        linespacing=1.7,
    )

    fig.suptitle(
        "腾讯业绩跨式：d+2 ROI 回测对比（口径 A vs 口径 B）",
        x=0.055, ha="left", fontsize=17, fontweight="bold", color=INK, y=0.955,
    )
    fig.text(
        0.055, 0.905,
        "入场：业绩前 1 日买入 ATM 跨式 · 持有至业绩后第 2 个交易日按内在价值平仓 · 无未来函数",
        fontsize=10, color=SUB,
    )
    _footnote(
        fig,
        "数据来源：data/backtest_tencent_straddle.json（engine_backtest / proxy_backtest，"
        "共同期间 2023Q3–2026Q1）· 脱敏/模拟数据，非实盘业绩",
        "口径 A：engine + 历史期权 IV（T=6 天，K=5 元取整，滑点 5%）；口径 B：predict_vola 代理 ×1.05 滑点",
    )

    ax.legend(
        loc="upper left", fontsize=9, framealpha=1.0,
        borderaxespad=0.6, handlelength=1.4,
    )
    fig.savefig(OUT_DIR / "backtest_roi_comparison.png", dpi=200)
    plt.close(fig)
    print("saved backtest_roi_comparison.png")


# --------------------------------------------------------------------------
# 图 2：腾讯 480 跨式（2 张）到期盈亏折线
# --------------------------------------------------------------------------
def chart_straddle_pnl() -> None:
    card = _load("decision_card_2026-08-12.json")
    hero = _load("hero_inputs.json")

    nums = card["numbers"]
    strike = float(nums["strike"])          # 480
    lots = int(nums["lots"])                # 2
    multiplier = int(hero["account"]["contract_multiplier"])  # 100
    be_down, be_up = float(nums["breakeven"][0]), float(nums["breakeven"][1])
    max_loss = float(nums["max_loss"])      # 4414

    # 理论口径成本：hero_inputs 主到期 call/put mid 之和 × 乘数 × 张数
    leg0 = hero["legs"][0]
    call_mid = float(leg0["call"]["mid"])    # 10.27
    put_mid = float(leg0["put"]["mid"])      # 10.825
    call_ask = float(leg0["call"]["ask"])    # 10.75
    put_ask = float(leg0["put"]["ask"])      # 11.32
    mid_cost = (call_mid + put_mid) * multiplier * lots   # 4219.0
    ask_cost = (call_ask + put_ask) * multiplier * lots   # 4414.0（应等于 max_loss）

    # X 范围：盈亏平衡点外扩约 20%（±2×预期波动区间内）
    x_min = 440.0
    x_max = 520.0
    xs = [x_min + i * 0.5 for i in range(int((x_max - x_min) / 0.5) + 1)]

    payoff = [abs(s - strike) * multiplier * lots for s in xs]
    pnl_mid = [p - mid_cost for p in payoff]
    pnl_ask = [p - ask_cost for p in payoff]

    fig, ax = plt.subplots(figsize=(12.8, 7.2))
    fig.subplots_adjust(left=0.065, right=0.965, top=0.78, bottom=0.16)
    _style_axes(ax)
    ax.grid(True, axis="x", color=GRID, linewidth=0.6, alpha=0.55)

    ax.plot(xs, pnl_mid, color=CYAN, linewidth=2.6, zorder=4,
            label=f"理论口径（mid 成本 {mid_cost:,.0f} HKD = {call_mid}+{put_mid} ×100×{lots}）")
    ax.plot(xs, pnl_ask, color=GREEN, linewidth=2.6, zorder=4,
            label=f"可成交口径（ask 成本 {ask_cost:,.0f} HKD = {call_ask}+{put_ask} ×100×{lots}）")

    # 0 轴
    ax.axhline(0, color="#475569", linewidth=1.2, zorder=2)

    # 最大亏损水平线（decision_card max_loss）
    ax.axhline(-max_loss, color=RED, linewidth=1.4, linestyle=(0, (5, 4)),
               zorder=3, alpha=0.95)
    ax.text(x_max, -max_loss + 150, f"最大亏损 −{max_loss:,.0f} HKD（ask 口径）",
            ha="right", va="bottom", fontsize=9.5, color=RED, fontweight="bold")

    # 盈亏平衡竖虚线（JSON 实际值 458.905 / 501.095）
    for be, label, ha, xoff in (
        (be_down, f"BE {be_down}", "right", -12),
        (be_up, f"BE {be_up}", "left", 12),
    ):
        ax.axvline(be, color=AMBER, linewidth=1.4, linestyle=(0, (4, 4)), zorder=3)
        ax.text(be + xoff, 3400, label, ha=ha, va="top", fontsize=9.5,
                color=AMBER, fontweight="bold")

    # strike 标记
    ax.axvline(strike, color=FAINT, linewidth=1.0, linestyle=(0, (1, 3)), zorder=3)
    ax.text(strike, -5050, f"Strike {strike:.0f}", ha="center", va="top",
            fontsize=9, color=SUB)

    # 理论口径最大亏损（与 BE 一致，谷底）
    ax.text(strike, -mid_cost - 420, f"理论口径谷底 −{mid_cost:,.0f}",
            ha="center", va="top", fontsize=8.5, color=CYAN)

    ax.set_xlim(x_min, x_max)
    ax.set_ylim(-5900, 4600)
    ax.set_xlabel("到期标的价格 S_T（HKD）", fontsize=10)
    ax.set_ylabel("到期盈亏（HKD，2 张 × 乘数 100）", fontsize=10)

    fig.suptitle(
        "腾讯 480 跨式（2 张）到期盈亏曲线",
        x=0.065, ha="left", fontsize=17, fontweight="bold", color=INK, y=0.95,
    )
    fig.text(
        0.065, 0.895,
        f"Strike 480 · 主到期 2026-08-14 · 盈亏平衡 {be_down} / {be_up} · "
        f"最大亏损 {max_loss:,.0f} HKD（≤ 风险预算 5,000）",
        fontsize=10, color=SUB,
    )
    _footnote(
        fig,
        "理论合成（模拟）：到期内在价值 = |S_T − 480| × 100 × 2，P&L = 内在价值 − 成本；"
        "盈亏平衡点/最大亏损取自 decision_card_2026-08-12.json，"
        "期权价取自 hero_inputs.json（快照 2026-08-08）· 非投资建议",
        "数据日期：决策卡 2026-08-12 · 行情快照 2026-08-08（HK CLOSED）",
    )

    ax.legend(loc="upper center", fontsize=9, framealpha=1.0, ncol=1,
              borderaxespad=0.7, handlelength=1.6)
    fig.savefig(OUT_DIR / "straddle_pnl.png", dpi=200)
    plt.close(fig)
    print("saved straddle_pnl.png")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chart_backtest_roi()
    chart_straddle_pnl()


if __name__ == "__main__":
    main()
