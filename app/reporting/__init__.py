"""LLM-powered analytical report generation for web-data-extractor.

This package extends the scraper with a reporting engine. It reads the
already-scraped ``reviews`` / ``products`` / ``records`` collections, computes
analytical metrics in Python (never in the LLM), persists aggregated summaries
in dedicated collections (``report_summaries`` / ``reports`` /
``report_insights`` / ``report_artifacts``), asks a provider-independent LLM to
turn the numbers into business narrative (with a deterministic fallback when no
LLM is configured), validates that narrative against the computed numbers, and
renders PDF/Excel artifacts.

Design rules (see the implementation prompt):
  * The original scraped collections are never modified.
  * Python owns every numeric KPI; the LLM only interprets.
  * Every insight is evidence-backed and validated before it is stored.
  * Sections the marketplace-review data cannot support are reported as
    unavailable, never fabricated.
"""

AGGREGATION_VERSION = "1.0"
PROMPT_VERSION = "1.0"
