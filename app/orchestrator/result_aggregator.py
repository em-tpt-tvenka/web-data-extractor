from __future__ import annotations


def dedupe_products(models: list[dict]) -> list[dict]:
    """Drop duplicate products that appear on more than one search page,
    keeping the first occurrence by product id."""
    seen: set[str] = set()
    unique: list[dict] = []
    for model in models:
        pid = model.get("product_details", {}).get("product_id")
        if pid and pid in seen:
            continue
        if pid:
            seen.add(pid)
        unique.append(model)
    return unique
