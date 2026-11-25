"""Matplotlib charts rendered to PNG bytes for embedding in the PDF.

Matplotlib is imported lazily; if it is not installed, every method returns
``None`` and the PDF simply omits charts (text/tables still render).
"""

from __future__ import annotations

import io
from typing import Any

from app.core.logging import get_logger
from app.reporting.constants import BRAND_SERIES_COLORS, PALETTE, SENTIMENT_COLORS

_log = get_logger("reporting.charts")


def _plt():
    import logging  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    import matplotlib.pyplot as plt  # noqa: PLC0415
    return plt


class ChartGenerator:
    def __init__(self) -> None:
        try:
            self._plt = _plt()
            self.available = True
        except Exception as exc:  # pragma: no cover
            _log.warning("event=matplotlib_unavailable error=%s", exc)
            self._plt = None
            self.available = False

    def _render(self, fig) -> bytes:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        self._plt.close(fig)
        return buf.getvalue()

    # ── individual charts ───────────────────────────────────────────────────

    def sentiment_donut(self, sentiment: dict[str, Any]) -> bytes | None:
        if not self.available:
            return None
        labels, sizes, colors = [], [], []
        for key in ("positive", "neutral", "negative", "mixed"):
            val = sentiment.get(key, 0)
            if val:
                labels.append(f"{key.title()} ({val})")
                sizes.append(val)
                colors.append(SENTIMENT_COLORS.get(key, PALETTE["neutral"]))
        if not sizes:
            return None
        fig, ax = self._plt.subplots(figsize=(4.2, 3.2))
        ax.pie(sizes, labels=labels, colors=colors, startangle=90,
               wedgeprops=dict(width=0.42), textprops={"fontsize": 8})
        ax.set_title("Sentiment split", fontsize=10)
        return self._render(fig)

    def share_of_voice(self, brands: list[dict]) -> bytes | None:
        if not self.available or not brands:
            return None
        brands = brands[:7]
        labels = [f"{b['brand']} ({b['share_of_voice_pct']}%)" for b in brands]
        sizes = [b["mentions"] for b in brands]
        colors = BRAND_SERIES_COLORS[:len(brands)]
        fig, ax = self._plt.subplots(figsize=(4.2, 3.2))
        ax.pie(sizes, labels=labels, colors=colors, startangle=90,
               wedgeprops=dict(width=0.42), textprops={"fontsize": 8})
        ax.set_title("Share of voice", fontsize=10)
        return self._render(fig)

    def brand_net_sentiment(self, brands: list[dict]) -> bytes | None:
        if not self.available or not brands:
            return None
        brands = sorted(brands, key=lambda b: b["net_sentiment"])[:10]
        names = [b["brand"] for b in brands]
        vals = [b["net_sentiment"] for b in brands]
        colors = [PALETTE["blue"] if v >= 0 else PALETTE["coral"] for v in vals]
        fig, ax = self._plt.subplots(figsize=(5.2, 3.2))
        ax.barh(names, vals, color=colors)
        ax.axvline(0, color=PALETTE["ink"], linewidth=0.6)
        ax.set_title("Net sentiment by brand", fontsize=10)
        ax.tick_params(labelsize=8)
        return self._render(fig)

    def monthly_trend(self, points: list[dict]) -> bytes | None:
        if not self.available or len(points) < 2:
            return None
        periods = [p["period"] for p in points]
        reviews = [p["reviews"] for p in points]
        net = [p["net_sentiment"] for p in points]
        fig, ax1 = self._plt.subplots(figsize=(5.6, 3.0))
        ax1.plot(periods, reviews, color=PALETTE["blue"], marker="o", label="Reviews")
        ax1.set_ylabel("Reviews", fontsize=8, color=PALETTE["blue"])
        ax2 = ax1.twinx()
        ax2.plot(periods, net, color=PALETTE["coral"], marker="s", label="Net sentiment")
        ax2.set_ylabel("Net sentiment", fontsize=8, color=PALETTE["coral"])
        ax1.set_title("Monthly volume & sentiment", fontsize=10)
        ax1.tick_params(axis="x", labelrotation=45, labelsize=7)
        ax1.tick_params(axis="y", labelsize=8)
        return self._render(fig)

    def complaint_bar(self, complaints: list[dict]) -> bytes | None:
        if not self.available or not complaints:
            return None
        complaints = complaints[:8]
        names = [c["category"] for c in complaints][::-1]
        vals = [c["count"] for c in complaints][::-1]
        fig, ax = self._plt.subplots(figsize=(5.2, 3.0))
        ax.barh(names, vals, color=PALETTE["coral"])
        ax.set_title("Complaints by category", fontsize=10)
        ax.tick_params(labelsize=8)
        return self._render(fig)

    def topic_bar(self, topics: list[dict]) -> bytes | None:
        if not self.available or not topics:
            return None
        topics = topics[:10]
        names = [t["theme"] for t in topics][::-1]
        vals = [t["net_sentiment"] for t in topics][::-1]
        colors = [PALETTE["blue"] if v >= 0 else PALETTE["coral"] for v in vals]
        fig, ax = self._plt.subplots(figsize=(5.2, 3.2))
        ax.barh(names, vals, color=colors)
        ax.axvline(0, color=PALETTE["ink"], linewidth=0.6)
        ax.set_title("Theme net sentiment", fontsize=10)
        ax.tick_params(labelsize=8)
        return self._render(fig)

    def build_all(self, summary) -> dict[str, bytes]:
        """Render the standard chart set; skips any that have no data."""
        out: dict[str, bytes] = {}
        def put(name, data):
            if data:
                out[name] = data
        put("sentiment_donut", self.sentiment_donut(summary.sentiment_summary.model_dump()))
        put("share_of_voice", self.share_of_voice([b.model_dump() for b in summary.brand_metrics]))
        put("brand_net_sentiment", self.brand_net_sentiment([b.model_dump() for b in summary.brand_metrics]))
        put("monthly_trend", self.monthly_trend([p.model_dump() for p in summary.monthly_trends.points]))
        put("complaint_bar", self.complaint_bar([c.model_dump() for c in summary.complaint_summary]))
        put("topic_bar", self.topic_bar([t.model_dump() for t in summary.topic_summary]))
        return out
