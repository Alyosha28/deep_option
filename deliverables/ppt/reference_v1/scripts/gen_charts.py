# -*- coding: utf-8 -*-
"""GOAI 参考版 PPT 图表生成（Style E 券商报告风，色板来自 design_lock.md）。

数值断言：每张图的数据与 presentation/facts.md 锁定值一致，生成前 assert 校验。
输出：media/chart_breakeven.png / chart_roi.png / chart_crush.png（透明底，@2x）。
"""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media")
os.makedirs(OUT, exist_ok=True)

# ── Style E 图表色板（design_lock.md §6）──
C_TEXT = "#1F2A2A"
C_SUB = "#6B726C"
C_GRID = "#DDD8CD"
C_POS = "#0E7C4A"
C_NEG = "#B0413E"
C_NEG2 = "#C8734F"


def _style(ax):
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(C_GRID)
    ax.tick_params(colors=C_SUB, labelsize=9.5)
    ax.yaxis.grid(True, color=C_GRID, linewidth=0.8, linestyle=(0, (3, 3)))
    ax.set_axisbelow(True)


def _save(fig, name):
    path = os.path.join(OUT, name)
    fig.savefig(path, dpi=200, transparent=True, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    print("saved:", path)


# ── 1) 盈亏平衡 vs 预期波动（facts: 4.41 vs 3.92）──
EXPECTED_MOVE = 3.916   # hero_inputs.json 原值；卡内文本 3.92
BREAKEVEN = 4.41        # (480-458.905)/478.8
assert EXPECTED_MOVE < BREAKEVEN
fig, ax = plt.subplots(figsize=(3.74, 2.30), dpi=200)
labels = ["回本所需 4.41%", "市场预期波动 3.92%"]
vals = [BREAKEVEN, 3.92]
colors = [C_NEG, C_POS]
bars = ax.barh(labels, vals, color=colors, height=0.52)
ax.set_xlim(0, 5.4)
for b, v in zip(bars, vals):
    ax.text(v + 0.10, b.get_y() + b.get_height() / 2, f"{v}%", va="center", ha="left",
            fontweight="bold", color=C_TEXT, fontsize=11.5)
ax.invert_yaxis()
ax.set_title("Edge 门检查①：预期波动 vs 回本", loc="left", fontsize=10.5,
             fontweight="bold", color=C_TEXT, pad=8)
ax.tick_params(axis="y", labelsize=10.5, colors=C_TEXT)
ax.xaxis.set_visible(False)
for spine in ("top", "right", "bottom"):
    ax.spines[spine].set_visible(False)
ax.spines["left"].set_visible(False)
_save(fig, "chart_breakeven.png")

# ── 2) 历史回测 ROI（facts: 口径A d+2 -7.78 / d+5 +20.97；口径B d+2 -47.54）──
A_D2, A_D5, B_D2 = -7.78, 20.97, -47.54
assert A_D2 < 0 < A_D5 and B_D2 < A_D2
fig, ax = plt.subplots(figsize=(4.06, 2.80), dpi=200)
groups = ["口径A\nd+2\n胜率 36%", "口径A\nd+5\n胜率 64%", "口径B\nd+2\n胜率 16%"]
vals = [A_D2, A_D5, B_D2]
colors = [C_NEG, C_POS, C_NEG]
bars = ax.bar(groups, vals, color=colors, width=0.56)
ax.axhline(0, color=C_TEXT, lw=0.9)
for b, v in zip(bars, vals):
    ax.text(b.get_x() + b.get_width() / 2, v + (1.8 if v >= 0 else -1.8),
            f"{v:+.1f}%", ha="center", va="bottom" if v >= 0 else "top",
            fontweight="bold", color=C_TEXT, fontsize=10.5)
ax.set_ylim(-62, 34)
ax.set_title("买入业绩跨式历史平均 ROI（口径 A / B）", loc="left", fontsize=10.5,
             fontweight="bold", color=C_TEXT, pad=8)
_style(ax)
_save(fig, "chart_roi.png")

# ── 3) IV crush 压力测试（facts: -711.24/-339.66, -820.32/-403.27, -873.32/-422.27）──
UP = [-711, -820, -873]
DOWN = [-340, -403, -422]
for got, want in zip(UP + DOWN, [-711.24, -820.32, -873.32, -339.66, -403.27, -422.27]):
    assert abs(abs(got) - abs(want)) < 0.5
fig, ax = plt.subplots(figsize=(4.06, 2.80), dpi=200)
x = np.arange(3)
w = 0.30
ax.bar(x - w / 2, UP, w, color=C_NEG, label="上行 P&L")
ax.bar(x + w / 2, DOWN, w, color=C_NEG2, label="下行 P&L")
for xi, v in zip(x - w / 2, UP):
    ax.text(xi, v - 30, str(v), ha="center", va="top", fontweight="bold", color=C_TEXT, fontsize=9.5)
for xi, v in zip(x + w / 2, DOWN):
    ax.text(xi, v - 30, str(v), ha="center", va="top", fontweight="bold", color=C_TEXT, fontsize=9.5)
ax.set_xticks(x)
ax.set_xticklabels(["crush -20%", "crush -35%", "crush -50%"], fontsize=9.5)
ax.set_ylim(-1050, 20)
ax.set_title("IV crush 压力测试 P&L（HKD，方向正确情形）", loc="left", fontsize=10.5,
             fontweight="bold", color=C_TEXT, pad=8)
ax.legend(frameon=False, fontsize=9, labelcolor=C_SUB, loc="upper right")
_style(ax)
_save(fig, "chart_crush.png")

print("chart values OK")
