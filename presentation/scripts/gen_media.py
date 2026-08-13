# -*- coding: utf-8 -*-
"""GOAI 初赛评审 PPT — 深色主题图表生成脚本（QA 修复版）。

生成 presentation/media/ 下的两张 PNG：
  1. backtest_roi_comparison.png — 口径 A（引擎+历史 IV）vs 口径 B（预期波动代理）
     的 d+2 平均 ROI 横向双条图，数据全部取自 data/backtest_tencent_straddle.json。
  2. straddle_pnl.png — 腾讯 480 跨式（2 张）到期盈亏曲线（理论合成），
     参数取自 data/decision_card_2026-08-12.json 与 data/hero_inputs.json。

QA 修复要点（相对旧版 12.8in 全幅图）：
  - 按实际嵌入尺寸出图：figsize = 嵌入 pt / 72（1pt = 1/72 in），dpi 200。
    straddle_pnl.png 按 07 页 372×176pt 设计（10 页 416×234pt 同文件放大嵌入）；
    backtest_roi_comparison.png 按 14 页 376×186pt 设计。
    图内文字直接以 pt 指定（标签 ≥ 9pt、刻度 ≥ 8pt），嵌入后等效字号达标，无需缩放。
  - 颜色只用 design-lock.md §2 色板 token；废弃旧版色板外 HEX
    （#F6465D / #F5B942 / #5A6473 / #30363D / #475569）。
  - 口径 B（负收益对比）条色由绿改为次文字灰 #8B949E；straddle 图 ask 可成交成本
    曲线改用红 #F87171（成本/风险语义），mid 理论成本曲线用青 #22D3EE。

约束：所有数字从 JSON 读取，不硬编码；无账户信息；透明底输出（页面 $bg 透出）。
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[2]  # F:/GOAi_competition
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "presentation" / "media"

# ---- design-lock.md §2 色板（唯一合法色值，禁止新增 HEX）----
BG = "#0A0E14"      # $bg      页面背景
PANEL = "#131A24"   # $panel   坐标系面板
GRID = "#1F2937"    # $border  轴脊/网格
INK = "#E6EDF3"     # $text    主文字/数值标签
SUB = "#8B949E"     # $textSub 次文字/轴刻度/零轴
CYAN = "#22D3EE"    # $accent  主系列（口径 A / mid 理论口径 / strike）
GREEN = "#34D399"   # $success 正收益区填充（8% alpha）
AMBER = "#F59E0B"   # $warn    盈亏平衡竖虚线
RED = "#F87171"     # $danger  负收益/最大亏损/ask 可成交口径

# 嵌入尺寸（pt）与 figsize/dpi（1pt = 1/72 in）
STRADDLE_PT = (372, 176)    # 07 页 372×176pt；10 页 416×234pt 同文件放大嵌入
BACKTEST_PT = (376, 186)    # 14 页 376×186pt
DPI = 200

plt.rcParams.update(
    {
        "font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial"],
        "font.family": "sans-serif",
        "axes.unicode_minus": False,
        "figure.facecolor": BG,
        "savefig.facecolor": "none",  # 透明底保存（面板另由 _style_axes 的显式矩形承载）
        "axes.facecolor": PANEL,
        "axes.edgecolor": GRID,
        "axes.labelcolor": SUB,
        "axes.titlecolor": INK,
        "text.color": INK,
        "xtick.color": SUB,
        "ytick.color": SUB,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "legend.facecolor": PANEL,
        "legend.edgecolor": GRID,
        "legend.labelcolor": INK,
        "figure.dpi": DPI,
    }
)


def _load(name: str) -> dict:
    with open(DATA_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _neg(text: str) -> str:
    """ASCII 负号 → Unicode 负号（与 axes.unicode_minus=False 的刻度一致）。"""
    return text.replace("-", "\u2212")


def _style_axes(ax: plt.Axes) -> None:
    """统一深色外观：去掉 top/right 脊，脊线/刻度用色板 token。

    面板底色用显式不透明矩形承载（而非依赖 axes patch）：
    本环境 matplotlib 3.11.1 的 `savefig(transparent=True)` 会把所有 axes patch
    临时置为透明，直接导致 $panel 面板与半透明填充渲染异常；
    手动矩形不受该临时修改影响，透明底 + 面板 + 正确 alpha 合成可兼得。
    """
    ax.patch.set_facecolor("none")
    ax.add_artist(
        plt.Rectangle(
            (0, 0), 1, 1, transform=ax.transAxes,
            facecolor=PANEL, edgecolor="none", zorder=0, clip_on=False,
        )
    )
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)
        ax.spines[spine].set_linewidth(0.8)
    ax.tick_params(colors=SUB, labelsize=8)


# --------------------------------------------------------------------------
# 图 1：口径 A vs 口径 B 的 d+2 平均 ROI 横向双条图（14 页，376×186pt）
# --------------------------------------------------------------------------
def chart_backtest_roi() -> None:
    bt = _load("backtest_tencent_straddle.json")
    stats_a = bt["engine_backtest"]["stats"]["d2"]   # 口径 A（引擎+历史 IV，n=11）
    stats_b = bt["proxy_backtest"]["stats"]["d2"]    # 口径 B（预期波动代理，n=19）
    mean_a = float(stats_a["mean_roi_pct"])
    mean_b = float(stats_b["mean_roi_pct"])
    win_a = float(stats_a["win_rate_pct"])
    win_b = float(stats_b["win_rate_pct"])
    n_a = int(stats_a["n"])
    n_b = int(stats_b["n"])

    fig, ax = plt.subplots(figsize=(BACKTEST_PT[0] / 72, BACKTEST_PT[1] / 72))
    fig.subplots_adjust(left=0.37, right=0.94, top=0.80, bottom=0.16)
    _style_axes(ax)
    ax.grid(True, axis="x", color=GRID, linewidth=0.6, alpha=0.5)

    # 两根横向条：B 在上（跌幅更大），A 在下。负收益对比不用绿。
    ax.barh(1, mean_b, height=0.5, color=SUB, zorder=3)
    ax.barh(0, mean_a, height=0.5, color=CYAN, zorder=3)

    # 0 轴
    ax.axvline(0, color=SUB, linewidth=1.0, zorder=2)

    # 条形末端数值标签
    ax.text(
        mean_a - 0.8, 0, _neg(f"{mean_a:.2f}%"),
        ha="right", va="center", fontsize=9.5, fontweight="bold",
        color=INK, fontfamily="Consolas", zorder=4,
    )
    ax.text(
        mean_b - 0.8, 1, _neg(f"{mean_b:.2f}%"),
        ha="right", va="center", fontsize=9.5, fontweight="bold",
        color=INK, fontfamily="Consolas", zorder=4,
    )

    # 胜率标注（右侧小字）
    ax.text(1.2, 0, f"胜率 {win_a:.1f}%", ha="left", va="center",
            fontsize=9, color=SUB, fontfamily="Microsoft YaHei")
    ax.text(1.2, 1, f"胜率 {win_b:.1f}%", ha="left", va="center",
            fontsize=9, color=SUB, fontfamily="Microsoft YaHei")

    # 类别标签（两行：口径名 + 样本/期间）
    ax.set_yticks([1, 0])
    ax.set_yticklabels(
        [
            f"口径 B · 预期波动代理（n={n_b}）\n共同期间 2023Q3\u20132026Q1",
            f"口径 A · 引擎+历史 IV（n={n_a}）\n2023Q3\u20132026Q1",
        ],
        fontsize=9, color=INK, fontfamily="Microsoft YaHei",
    )

    ax.set_xlim(-72, 30)
    ax.set_ylim(-0.55, 1.75)
    ax.set_xticks([-60, -40, -20, 0])
    ax.xaxis.set_major_formatter(
        lambda v, _: _neg(f"{v:.0f}%")
    )
    plt.setp(ax.get_xticklabels(), fontfamily="Consolas")

    # 标题（结论式；本版 matplotlib 的 set_title(loc="left") 有丢文本 bug，
    # 故用默认位置 + 手动左对齐定位）
    ax.set_title(
        "d+2 平均 ROI · 口径 A vs 口径 B",
        fontsize=11, fontweight="bold", color=INK,
        pad=10, fontfamily="Microsoft YaHei",
    )
    ax.title.set_position((0, 1.02))
    ax.title.set_ha("left")
    ax.title.set_va("bottom")

    # 角落小字
    fig.text(
        0.985, 0.02, f"脱敏/模拟回测 · 非实盘业绩 · 样本 {n_a}/{n_b} 期",
        ha="right", va="bottom", fontsize=9, color=SUB,
        fontfamily="Microsoft YaHei",
    )

    fig.savefig(OUT_DIR / "backtest_roi_comparison.png", dpi=DPI, transparent=True)
    plt.close(fig)
    print(f"saved backtest_roi_comparison.png "
          f"(figsize {BACKTEST_PT[0]/72:.3f}x{BACKTEST_PT[1]/72:.3f} in @ {DPI} dpi)")


# --------------------------------------------------------------------------
# 图 2：腾讯 480 跨式（2 张）到期盈亏曲线（07/10 页，按 372×176pt 设计）
# --------------------------------------------------------------------------
def chart_straddle_pnl() -> None:
    card = _load("decision_card_2026-08-12.json")
    hero = _load("hero_inputs.json")

    nums = card["numbers"]
    strike = float(nums["strike"])               # 480
    lots = int(nums["lots"])                     # 2
    multiplier = int(hero["account"]["contract_multiplier"])  # 100
    be_down, be_up = (float(v) for v in nums["breakeven"])    # 458.905 / 501.095
    max_loss = float(nums["max_loss"])           # 4414

    # 理论口径成本（mid）与可成交口径成本（ask），均来自 hero_inputs 主到期盘口
    leg0 = hero["legs"][0]                       # 主到期 2026-08-14
    call_mid = float(leg0["call"]["mid"])        # 10.27
    put_mid = float(leg0["put"]["mid"])          # 10.825
    call_ask = float(leg0["call"]["ask"])        # 10.75
    put_ask = float(leg0["put"]["ask"])          # 11.32
    mid_cost = (call_mid + put_mid) * multiplier * lots   # 4219.0
    ask_cost = (call_ask + put_ask) * multiplier * lots   # 4414.0（应等于 max_loss）
    assert abs(ask_cost - max_loss) < 1e-6, "ask 口径成本应等于决策卡 max_loss"

    x_min, x_max = 400.0, 560.0
    xs = [x_min + i * 0.5 for i in range(int((x_max - x_min) / 0.5) + 1)]
    payoff = [abs(s - strike) * multiplier * lots for s in xs]
    pnl_mid = [p - mid_cost for p in payoff]
    pnl_ask = [p - ask_cost for p in payoff]

    fig, ax = plt.subplots(figsize=(STRADDLE_PT[0] / 72, STRADDLE_PT[1] / 72))
    fig.subplots_adjust(left=0.165, right=0.965, top=0.84, bottom=0.22)
    _style_axes(ax)
    ax.grid(True, axis="y", color=GRID, linewidth=0.6, alpha=0.5)

    # 盈亏区填充：正区绿 8%、负区红 8%（半透明 token，无新 HEX）
    ax.fill_between(xs, pnl_mid, 0, where=[v >= 0 for v in pnl_mid],
                    color=GREEN, alpha=0.08, zorder=1)
    ax.fill_between(xs, pnl_ask, 0, where=[v <= 0 for v in pnl_ask],
                    color=RED, alpha=0.08, zorder=1)

    # 双成本口径曲线：ask 可成交成本红（成本/风险语义）、mid 理论成本青
    ax.plot(xs, pnl_ask, color=RED, linewidth=2.2, zorder=4)
    ax.plot(xs, pnl_mid, color=CYAN, linewidth=2.2, zorder=4)

    # 0 轴
    ax.axhline(0, color=SUB, linewidth=1.0, zorder=2)

    # 最大亏损水平虚线（红）+ 标签
    ax.axhline(-max_loss, color=RED, linewidth=1.3, linestyle=(0, (5, 4)), zorder=3)
    ax.text(
        x_max, -4250, f"最大亏损 \u2212{max_loss:,.0f}",
        ha="right", va="top", fontsize=9, color=RED,
        fontfamily="Microsoft YaHei",
    )

    # 盈亏平衡两条竖虚线（琥珀）+ 数值标签
    for be, xoff, ha in ((be_down, -4, "right"), (be_up, 4, "left")):
        ax.axvline(be, color=AMBER, linewidth=1.3, linestyle=(0, (4, 4)), zorder=3)
        ax.text(
            be + xoff, -250, f"BE {be:.3f}",
            ha=ha, va="top", fontsize=9, color=AMBER,
            fontfamily="Consolas",
        )

    # Strike 480 竖标：青虚线 + 谷底青点 + 标签
    ax.axvline(strike, color=CYAN, linewidth=1.0, linestyle=(0, (1, 3)), zorder=3)
    ax.plot(strike, -mid_cost, marker="o", ms=4, color=CYAN, zorder=6)
    ax.text(
        strike, -4990, f"Strike {strike:.0f}",
        ha="center", va="top", fontsize=9, color=CYAN,
        fontfamily="Consolas",
    )

    ax.set_xlim(x_min, x_max)
    ax.set_ylim(-5200, 4400)
    ax.set_xticks([400, 440, 480, 520, 560])
    ax.set_yticks([-4000, -2000, 0, 2000, 4000])
    ax.yaxis.set_major_formatter(
        lambda v, _: "0" if v == 0 else _neg(f"{v:,.0f}")
    )
    plt.setp(ax.get_xticklabels(), fontfamily="Consolas")
    plt.setp(ax.get_yticklabels(), fontfamily="Consolas")

    # Y 轴标签：本版 matplotlib 的 ylabel 定位有 bug（锚点落在画布边缘），
    # 改用 fig.text 手动旋转放置，位置与坐标轴左缘对齐
    fig.text(
        0.035, 0.53, "到期盈亏（HKD）",
        ha="center", va="center", rotation=90, fontsize=9, color=SUB,
        fontfamily="Microsoft YaHei",
    )

    # 顶行图例（单行双色，替代图例框，避免与曲线重叠）
    fig.text(
        0.165, 0.955, f"\u2500\u2500 mid 成本 {mid_cost:,.0f}（理论口径）",
        ha="left", va="top", fontsize=9, color=CYAN,
        fontfamily="Microsoft YaHei",
    )
    fig.text(
        0.55, 0.955, f"\u2500\u2500 ask 成本 {ask_cost:,.0f}（可成交口径）",
        ha="left", va="top", fontsize=9, color=RED,
        fontfamily="Microsoft YaHei",
    )

    # 底行：左 x 轴说明、右角落小字
    fig.text(
        0.165, 0.06, "标的价格 S_T",
        ha="left", va="top", fontsize=9, color=SUB,
        fontfamily="Microsoft YaHei",
    )
    fig.text(
        0.985, 0.06, "理论合成（模拟）· 2026-08-08 · 非实盘业绩",
        ha="right", va="top", fontsize=9, color=SUB,
        fontfamily="Microsoft YaHei",
    )

    fig.savefig(OUT_DIR / "straddle_pnl.png", dpi=DPI, transparent=True)
    plt.close(fig)
    print(f"saved straddle_pnl.png "
          f"(figsize {STRADDLE_PT[0]/72:.3f}x{STRADDLE_PT[1]/72:.3f} in @ {DPI} dpi)")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chart_backtest_roi()
    chart_straddle_pnl()


if __name__ == "__main__":
    main()
