"""Configurable 2-level topic taxonomy for e-commerce reviews.

Modelled on the reference report's keyword taxonomy (Purchase / Product /
After-sales / Platform / Campaign). Keywords include a few Hinglish spellings as
the report does. The taxonomy is data, not code: callers may extend ``TAXONOMY``
or pass their own mapping to the aspect extractor.
"""

from __future__ import annotations

# level_1 -> { level_2_theme: [keywords...] }
TAXONOMY: dict[str, dict[str, list[str]]] = {
    "Purchase": {
        "Price & offers": ["price", "offer", "discount", "deal", "expensive", "cheap",
                            "value for money", "overpriced", "worth", "sasta", "mehenga"],
        "EMI": ["emi", "no cost emi", "no-cost emi", "installment", "finance"],
        "Exchange": ["exchange", "trade-in", "trade in", "exchange offer"],
        "Delivery": ["delivery", "shipping", "courier", "shipped", "delivered late",
                     "delivery late", "fast delivery", "on time", "arrived"],
    },
    "Product": {
        "Quality": ["quality", "build quality", "durable", "durability", "sturdy",
                    "cheap material", "broke", "broken", "defective", "faulty"],
        "Performance": ["performance", "slow", "lag", "fast", "smooth", "hang",
                        "heating", "heats up", "speed", "cooling", "noise", "noisy"],
        "Design": ["design", "look", "looks", "beautiful", "sleek", "color", "colour",
                   "compact", "bulky", "premium feel"],
        "Energy": ["energy", "power consumption", "star rating", "bijli bill",
                   "electricity", "power saving", "efficient", "inverter"],
        "Battery": ["battery", "charge", "charging", "backup", "drains", "battery life"],
        "Camera": ["camera", "photo", "picture quality", "lens", "megapixel", "zoom"],
    },
    "After-sales": {
        "Installation": ["installation", "install", "installed", "technician", "fitting",
                         "demo", "installation nahi hua", "setup"],
        "Repair": ["repair", "service center", "service centre", "repaired", "fix",
                   "technician visit", "service request"],
        "Spares": ["spare", "spare part", "parts", "replacement part", "accessory"],
        "Warranty": ["warranty", "guarantee", "warranty claim", "warranty reject",
                     "warranty rejected", "extended warranty"],
    },
    "Platform": {
        "Trust": ["fake", "fake product", "genuine", "original", "counterfeit",
                  "duplicate", "scam", "trust"],
        "Returns": ["return", "refund", "replace", "replacement", "return pickup",
                    "return policy", "money back"],
        "Packaging": ["packaging", "package", "box", "damaged box", "sealed",
                      "poorly packed", "tampered"],
    },
    "Campaign": {
        "Ads": ["ad", "advertisement", "promo", "campaign"],
        "Influencers": ["influencer", "youtuber", "review video", "unboxing"],
        "Launches": ["launch", "new launch", "newly launched", "pre-order", "preorder"],
    },
}


def iter_themes(taxonomy: dict[str, dict[str, list[str]]] | None = None):
    """Yield ``(level_1, theme, keywords)`` for every leaf theme."""
    tax = taxonomy or TAXONOMY
    for level_1, themes in tax.items():
        for theme, keywords in themes.items():
            yield level_1, theme, keywords


# Precomputed longest-first keyword index so multi-word phrases match before
# their single-word substrings (e.g. "no cost emi" before "emi").
def build_keyword_index(
    taxonomy: dict[str, dict[str, list[str]]] | None = None,
) -> list[tuple[str, str, str]]:
    """Return ``[(keyword, level_1, theme), ...]`` sorted longest-first."""
    index: list[tuple[str, str, str]] = []
    for level_1, theme, keywords in iter_themes(taxonomy):
        for kw in keywords:
            index.append((kw.lower(), level_1, theme))
    index.sort(key=lambda t: len(t[0]), reverse=True)
    return index
