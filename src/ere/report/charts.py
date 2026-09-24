"""Static SVG charts for the reports (matplotlib, no JavaScript, no external files).

Colours follow the validated reference palette (dataviz skill): categorical slots 1-3 (blue,
orange, aqua), recessive grid and axes. After rendering, every hex colour is swapped for a
CSS custom property (var(--series-1) etc.), so the same inline SVG follows the page's light
or dark theme. Every chart in the report sits next to a table with the same numbers (the
accessibility "table view", and the relief rule for the low-contrast aqua slot).
"""

from __future__ import annotations

import io
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
INK, INK2, MUTED, GRID, AXIS, SURFACE = ("#0b0b0b", "#52514e", "#898781", "#e1e0d9",
                                         "#c3c2b7", "#fcfcfb")
TOKENS = {
    SERIES[0]: "var(--series-1)", SERIES[1]: "var(--series-2)", SERIES[2]: "var(--series-3)",
    INK: "var(--text-primary)", INK2: "var(--text-secondary)", MUTED: "var(--text-muted)",
    GRID: "var(--grid)", AXIS: "var(--axis)", SURFACE: "var(--surface-1)",
}

plt.rcParams.update({
    "svg.fonttype": "none",
    "font.family": "sans-serif",
    "font.sans-serif": ["Inter", "Helvetica Neue", "Arial", "DejaVu Sans"],
    "font.size": 8.5,
    "axes.facecolor": "none",
    "figure.facecolor": "none",
    "savefig.facecolor": "none",
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK2,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.titlecolor": INK,
    "text.color": INK,
})


def _to_svg(fig) -> str:
    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight", facecolor="none")
    plt.close(fig)
    svg = buf.getvalue()
    svg = svg[svg.index("<svg"):]
    svg = re.sub(r'<svg([^>]*?) width="[^"]+" height="[^"]+"',
                 r'<svg\1 width="100%" preserveAspectRatio="xMinYMin meet"', svg, count=1)
    svg = re.sub(r"<metadata>.*?</metadata>", "", svg, flags=re.S)
    for hexv, var in TOKENS.items():
        svg = svg.replace(hexv, var).replace(hexv.upper(), var)
    return svg


def _style(ax, yfmt=None):
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(length=0)
    if yfmt:
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(yfmt))


def pct_fmt(x, _=None):
    return f"{x:.0%}"


def pct1_fmt(x, _=None):
    return f"{x:.1%}"


def num_fmt(x, _=None):
    return f"{x:,.0f}"


def line_chart(series: dict[str, pd.Series], title: str, yfmt=num_fmt, height=2.8,
               categorical: bool = False) -> str:
    """Up to three named series; legend above the plot plus a direct label at each line end.
    categorical=True plots the index as evenly spaced labels (e.g. fiscal years)."""
    fig, ax = plt.subplots(figsize=(10, height))
    labels = None
    for i, (name, s) in enumerate(list(series.items())[:3]):
        s = s.dropna()
        if s.empty:
            continue
        if categorical:
            labels = [str(x) for x in s.index]
            x = np.arange(len(s))
        else:
            x = s.index
        ax.plot(x, s.values, color=SERIES[i], linewidth=2, label=name,
                marker="o" if categorical else None, markersize=4)
        ax.annotate(name, (x[-1], s.values[-1]), xytext=(5, 0), textcoords="offset points",
                    va="center", color=INK2, fontsize=8)
    if categorical and labels:
        ax.set_xticks(np.arange(len(labels)), labels)
        ax.set_xlim(-0.3, len(labels) - 0.4)
    _style(ax, yfmt)
    ax.set_title(title, loc="left", fontsize=10, fontweight="bold", pad=18 if len(series) > 1
                 else 6)
    if len(series) > 1:
        ax.legend(frameon=False, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3,
                  fontsize=8, labelcolor=INK2, borderaxespad=0.1)
    return _to_svg(fig)


def bar_chart(labels: list[str], values: list[float], title: str, yfmt=num_fmt,
              highlight_last: bool = False) -> str:
    fig, ax = plt.subplots(figsize=(10, 2.6))
    x = np.arange(len(labels))
    colors = [SERIES[0]] * len(values)
    bars = ax.bar(x, values, width=0.55, color=colors, edgecolor=SURFACE, linewidth=2)
    ax.set_xticks(x, labels)
    _style(ax, yfmt)
    ax.axhline(0, color=AXIS, linewidth=0.8)
    ax.set_title(title, loc="left", fontsize=10, fontweight="bold")
    # label only the first and last bars: selective direct labels
    for i in {0, len(values) - 1} if values else set():
        v = values[i]
        if v is not None and np.isfinite(v):
            ax.annotate(yfmt(v), (bars[i].get_x() + bars[i].get_width() / 2, v),
                        xytext=(0, 3 if v >= 0 else -10), textcoords="offset points",
                        ha="center", fontsize=8, color=INK2)
    return _to_svg(fig)


def football_chart(ff: pd.DataFrame, price: float | None, labels: dict[str, str]) -> str:
    """Horizontal value ranges by method with the current price as a vertical rule.
    A method with a single value (e.g. a peer median) is drawn as a dot."""
    ff = ff.dropna(subset=["low", "high"])
    fig, ax = plt.subplots(figsize=(10, 0.42 * max(len(ff), 2) + 0.9))
    y = np.arange(len(ff))[::-1]
    for yi, r in zip(y, ff.itertuples(index=False), strict=False):
        lo, hi = min(r.low, r.high), max(r.low, r.high)
        if hi - lo > 1e-9 * max(abs(hi), 1):
            ax.barh(yi, hi - lo, left=lo, height=0.42, color=SERIES[0])
            ax.plot([r.mid, r.mid], [yi - 0.21, yi + 0.21], color=SURFACE, linewidth=2)
            txt = f"{lo:,.0f} – {hi:,.0f}"
        else:
            ax.plot([r.mid], [yi], "o", color=SERIES[0], markersize=8,
                    markeredgecolor=SURFACE, markeredgewidth=2)
            txt = f"{r.mid:,.0f}"
        ax.annotate(txt, (hi, yi), xytext=(7, 0), textcoords="offset points", va="center",
                    fontsize=8, color=INK2)
    ax.set_yticks(y, [labels.get(m, m) for m in ff.method])
    ax.set_ylim(-0.6, len(ff) - 0.4)
    if price and np.isfinite(price):
        ax.axvline(price, color=SERIES[1], linewidth=2)
        ax.text(price, 1.0, f" price {price:,.0f}", transform=ax.get_xaxis_transform(),
                ha="left", va="bottom", fontsize=8, color=INK2)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for s_ in ("top", "right", "left"):
        ax.spines[s_].set_visible(False)
    ax.tick_params(length=0)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(num_fmt))
    ax.set_title("Value per share by method (Rs)", loc="left", fontsize=10, fontweight="bold",
                 pad=16)
    return _to_svg(fig)
