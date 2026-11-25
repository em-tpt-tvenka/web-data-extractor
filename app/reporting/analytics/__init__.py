"""Deterministic analytics over enriched reviews.

Every function here is pure: it takes the list of enriched review dicts produced
by ``report_data_service`` and returns metric blocks plus a flat ``metric_index``
fragment (``{metric_id: value}``) that the response validator uses to confirm any
number the LLM cites is real. The enriched review dict has these keys:

    review_id, source_record_id, platform, product_id, brand, category,
    subcategory, product_type, industry, product_name, rating (float|None),
    title, text, date (datetime.date|None), country_code, verified_purchase,
    helpful_votes, language, sentiment (str), aspects (list[dict]),
    complaints (list[str])
"""
