"""Topic / theme aggregation with sentiment distribution per theme."""

from __future__ import annotations

from collections import defaultdict

from app.reporting.schemas.report_metrics import TopicMetric
from app.reporting.utils.report_helpers import safe_pct


def topic_summary(reviews: list[dict]) -> tuple[list[TopicMetric], dict]:
    # theme -> {category, reviews:set-of-indices via list, sentiment counts}
    themes: dict[str, dict] = defaultdict(
        lambda: {"category": "", "pos": 0, "neg": 0, "neu": 0, "mixed": 0, "count": 0}
    )
    total_theme_hits = 0
    for r in reviews:
        sent = r.get("sentiment")
        seen: set[str] = set()
        for a in r.get("aspects", []):
            theme = a["theme"]
            if theme in seen:
                continue
            seen.add(theme)
            t = themes[theme]
            t["category"] = a["category"]
            t["count"] += 1
            total_theme_hits += 1
            if sent == "positive":
                t["pos"] += 1
            elif sent == "negative":
                t["neg"] += 1
            elif sent == "mixed":
                t["mixed"] += 1
            elif sent == "neutral":
                t["neu"] += 1

    metrics: list[TopicMetric] = []
    for theme, t in themes.items():
        classified = t["pos"] + t["neg"] + t["neu"] + t["mixed"]
        pos_pct = safe_pct(t["pos"], classified)
        neg_pct = safe_pct(t["neg"], classified)
        net = round(pos_pct - neg_pct, 2)
        dominant = max(
            (("positive", t["pos"]), ("negative", t["neg"]),
             ("neutral", t["neu"]), ("mixed", t["mixed"])),
            key=lambda kv: kv[1],
        )[0]
        metrics.append(TopicMetric(
            theme=theme, category=t["category"], mentions=t["count"],
            share_pct=safe_pct(t["count"], total_theme_hits),
            positive=t["pos"], negative=t["neg"], neutral=t["neu"], mixed=t["mixed"],
            net_sentiment=net, dominant_sentiment=dominant,
        ))
    metrics.sort(key=lambda m: m.mentions, reverse=True)

    index = {"topic.total_hits": total_theme_hits, "topic.count": len(metrics)}
    for m in metrics:
        index[f"topic.{m.theme}.mentions"] = m.mentions
        index[f"topic.{m.theme}.net_sentiment"] = m.net_sentiment
    return metrics, index
