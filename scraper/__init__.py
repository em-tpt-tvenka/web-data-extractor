"""
Samsung Laptop Scraper Package.
"""

from .catalog import get_all_laptop_urls
from .parser import parse_laptop_page
from .reviews import fetch_laptop_reviews

__all__ = ["get_all_laptop_urls", "parse_laptop_page", "fetch_laptop_reviews"]

