"""
MongoDB & JSON/CSV Persistence Manager (Schema v2.0.0).
Stores separate Schema v2.0.0 Product Details (`products` collection / `*_products.json`)
and Review Details (`reviews` collection / `*_reviews.json`), with unified filtering & Excel CSV sync.
"""

import os
import csv
import json
import logging
from typing import List, Dict, Any, Optional, Tuple
import pymongo
from pymongo import MongoClient, ReplaceOne
from pymongo.errors import ConnectionFailure, PyMongoError

from config import MONGODB_URI, MONGODB_DB_NAME, MONGODB_COLLECTION, MONGODB_REVIEWS_COLLECTION
from models import (
    is_v2_product_doc,
    is_v2_review_doc,
    convert_v1_to_v2,
    extract_flat_summary,
    make_review_key,
    get_reviews_json_path,
    normalize_metadata_scraped_at,
)
from scraper.filters import ProductFilter, filter_and_sort_products, to_mongo_query

logger = logging.getLogger(__name__)


def _build_reviews_map(review_docs: List[Dict[str, Any]]) -> Dict[Any, Dict[str, Any]]:
    """Builds fast lookup dict keyed by (platform, platform_product_id) and platform_product_id."""
    rev_map: Dict[Any, Dict[str, Any]] = {}
    for doc in review_docs:
        if not is_v2_review_doc(doc):
            continue
        key = make_review_key(doc)
        if key[1]:
            rev_map[key] = doc
            if key[1] not in rev_map:
                rev_map[key[1]] = doc
    return rev_map


class MongoManager:
    """Optimized MongoDB client storing Schema v2.0.0 products and reviews in separate collections."""

    def __init__(
        self,
        uri: str = MONGODB_URI,
        db_name: str = MONGODB_DB_NAME,
        collection_name: str = MONGODB_COLLECTION,
        reviews_collection_name: str = MONGODB_REVIEWS_COLLECTION,
    ):
        self.uri = uri
        self.db_name = db_name
        self.collection_name = collection_name
        self.reviews_collection_name = reviews_collection_name
        self._client: Optional[MongoClient] = None
        self._db = None
        self._collection = None
        self._reviews_collection = None

    def connect(self) -> bool:
        """Establishes connection pool and initializes collections/indexes."""
        try:
            self._client = MongoClient(
                self.uri,
                serverSelectionTimeoutMS=4000,
                maxPoolSize=50,
                minPoolSize=5,
                retryWrites=True,
            )
            self._client.admin.command("ping")
            self._db = self._client[self.db_name]
            self._collection = self._db[self.collection_name]
            self._reviews_collection = self._db[self.reviews_collection_name]
            self._create_indexes()
            logger.info(
                f"Connected to MongoDB database '{self.db_name}', collections '{self.collection_name}' & '{self.reviews_collection_name}'"
            )
            return True
        except ConnectionFailure as e:
            logger.warning(f"Could not connect to MongoDB at {self.uri}: {e}")
            self._client = None
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to MongoDB: {e}")
            self._client = None
            return False

    def is_connected(self) -> bool:
        """Check if connected to MongoDB."""
        if not self._client:
            return False
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False

    def _create_indexes(self):
        """Migrates away legacy unique v1 index and creates Schema v2.0.0 indexes."""
        try:
            existing_prod_indexes = self._collection.index_information()
            if "idx_unique_product_id" in existing_prod_indexes:
                self._collection.drop_index("idx_unique_product_id")
                logger.info("Dropped legacy 'idx_unique_product_id' index on products collection.")

            self._collection.create_index(
                [("metadata.platform", pymongo.ASCENDING), ("product.identifiers.platform_product_id", pymongo.ASCENDING)],
                unique=True,
                name="idx_v2_platform_product_id",
                partialFilterExpression={"product.identifiers.platform_product_id": {"$type": "string"}},
            )
            self._collection.create_index([("product.core.brand", pymongo.ASCENDING)], name="idx_v2_brand")
            self._collection.create_index([("product.core.category_path", pymongo.ASCENDING)], name="idx_v2_category")
            self._collection.create_index(
                [("product.pricing_and_availability.current_price", pymongo.ASCENDING)],
                name="idx_v2_current_price",
            )

            self._reviews_collection.create_index(
                [("metadata.platform", pymongo.ASCENDING), ("metadata.platform_product_id", pymongo.ASCENDING)],
                unique=True,
                name="idx_v2_reviews_platform_product_id",
                partialFilterExpression={"metadata.platform_product_id": {"$type": "string"}},
            )
            self._reviews_collection.create_index(
                [("reviews.product_reviews.summary.average_rating", pymongo.DESCENDING)],
                name="idx_v2_avg_rating",
            )
            logger.debug("Schema v2.0.0 MongoDB indexes created successfully.")
        except PyMongoError as e:
            logger.warning(f"Failed to create indexes: {e}")

    def purge_legacy_v1_docs(self) -> int:
        """Removes legacy v1 documents lacking metadata.schema_version during clean migration."""
        if not self.is_connected() and not self.connect():
            return 0
        res = self._collection.delete_many({"metadata.schema_version": {"$exists": False}})
        return int(res.deleted_count or 0)

    def bulk_upsert_products(
        self,
        product_list: List[Dict[str, Any]],
        review_list: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, int]:
        """
        Batch upserts Schema v2.0.0 Product Details into `products` collection and
        Review Details into `reviews` collection cleanly without extra wrapper timestamps.
        """
        if not product_list and not review_list:
            return {"inserted": 0, "modified": 0, "matched": 0, "total": 0, "reviews_upserted": 0}

        if not self.is_connected() and not self.connect():
            raise ConnectionError(f"Cannot connect to MongoDB at {self.uri}")

        prod_ops: List[ReplaceOne] = []
        auto_reviews: List[Dict[str, Any]] = []

        for raw in (product_list or []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item.pop("_id", None)
            if is_v2_product_doc(item):
                v2_prod = normalize_metadata_scraped_at(item)
            else:
                v2_prod, v2_rev = convert_v1_to_v2(item)
                auto_reviews.append(v2_rev)

            plat, pid = make_review_key(v2_prod)
            if not pid:
                continue
            prod_ops.append(
                ReplaceOne(
                    {"metadata.platform": plat, "product.identifiers.platform_product_id": pid},
                    v2_prod,
                    upsert=True,
                )
            )

        rev_ops: List[ReplaceOne] = []
        for raw_r in (review_list if review_list is not None else auto_reviews):
            if not isinstance(raw_r, dict) or not is_v2_review_doc(raw_r):
                continue
            r_doc = normalize_metadata_scraped_at(dict(raw_r))
            r_doc.pop("_id", None)
            r_plat, r_pid = make_review_key(r_doc)
            if not r_pid:
                continue
            rev_ops.append(
                ReplaceOne(
                    {"metadata.platform": r_plat, "metadata.platform_product_id": r_pid},
                    r_doc,
                    upsert=True,
                )
            )

        inserted_count = 0
        modified_count = 0
        matched_count = 0
        rev_upserted = 0

        try:
            if prod_ops:
                bulk_result = self._collection.bulk_write(prod_ops, ordered=False)
                inserted_count = len(bulk_result.upserted_ids) if bulk_result.upserted_ids else 0
                modified_count = bulk_result.modified_count
                matched_count = bulk_result.matched_count

            if rev_ops and self._reviews_collection is not None:
                rev_res = self._reviews_collection.bulk_write(rev_ops, ordered=False)
                rev_upserted = (len(rev_res.upserted_ids) if rev_res.upserted_ids else 0) + rev_res.modified_count

            logger.info(
                f"Bulk write successful! Products upserted: {inserted_count}, updated: {modified_count}; "
                f"Reviews synced: {rev_upserted}"
            )
            return {
                "inserted": inserted_count,
                "modified": modified_count,
                "matched": matched_count,
                "total": len(prod_ops),
                "reviews_upserted": rev_upserted,
            }
        except PyMongoError as e:
            logger.error(f"Bulk write error: {e}")
            raise

    bulk_upsert_laptops = bulk_upsert_products

    def get_all_laptops(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve product documents from MongoDB without internal ObjectId."""
        if not self.is_connected() and not self.connect():
            return []
        return list(self._collection.find({}, {"_id": 0}).limit(limit))

    def get_all_reviews(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Retrieve review documents from MongoDB without internal ObjectId."""
        if not self.is_connected() and not self.connect():
            return []
        return list(self._reviews_collection.find({}, {"_id": 0}).limit(limit))

    def get_reviews_map(self, product_ids: Optional[List[str]] = None) -> Dict[Any, Dict[str, Any]]:
        """Loads paired Schema v2.0.0 review docs keyed by (platform, id) and id."""
        if not self.is_connected() and not self.connect():
            return {}
        q: Dict[str, Any] = {}
        if product_ids:
            q["metadata.platform_product_id"] = {"$in": product_ids}
        docs = list(self._reviews_collection.find(q, {"_id": 0}))
        return _build_reviews_map(docs)

    def get_laptop_by_id(self, product_id: str) -> Optional[Dict[str, Any]]:
        """Find a product by platform_product_id (or legacy product_id)."""
        if not self.is_connected() and not self.connect():
            return None
        return self._collection.find_one(
            {"$or": [{"product.identifiers.platform_product_id": product_id}, {"product_id": product_id}]},
            {"_id": 0},
        )

    def get_review_by_id(self, product_id: str, platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Find paired Schema v2.0.0 review document by product ID."""
        if not self.is_connected() and not self.connect():
            return None
        q: Dict[str, Any] = {"metadata.platform_product_id": product_id}
        if platform:
            q["metadata.platform"] = platform
        return self._reviews_collection.find_one(q, {"_id": 0})

    def query_products_and_reviews(
        self,
        flt: Optional[ProductFilter] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[Any, Dict[str, Any]]]:
        """Query products using MongoDB pre-filtering plus paired Review Details for rating filters."""
        if not self.is_connected() and not self.connect():
            return [], {}
        mongo_query = to_mongo_query(flt)
        docs = list(self._collection.find(mongo_query, {"_id": 0}))
        pids = [make_review_key(d)[1] for d in docs if make_review_key(d)[1]]
        reviews_by_key = self.get_reviews_map(pids)
        matched = filter_and_sort_products(docs, flt, reviews_by_key=reviews_by_key)
        return matched, reviews_by_key

    def query_products(self, flt: Optional[ProductFilter] = None) -> List[Dict[str, Any]]:
        """Query products using MongoDB pre-filtering plus unified ProductFilter rules."""
        matched, _ = self.query_products_and_reviews(flt)
        return matched

    def export_to_json(self, filepath: str = "output/samsung_products.json") -> str:
        """Export all stored products & reviews from MongoDB into paired Schema v2.0.0 JSON & CSV files."""
        products = self.get_all_laptops(limit=1000)
        reviews = self.get_all_reviews(limit=1000)
        return save_laptops_to_json(products, filepath, merge=False, review_list=reviews)

    def close(self):
        """Close MongoDB connection pool."""
        if self._client:
            self._client.close()
            logger.info("MongoDB connection closed.")


def _unique_review_docs(reviews_by_key: Dict[Any, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicates a review lookup map (which contains both tuple and str keys) into unique docs."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for k, doc in reviews_by_key.items():
        if isinstance(k, tuple) and k not in seen:
            seen.add(k)
            out.append(doc)
    return out


def save_laptops_to_json(
    laptop_list: List[Dict[str, Any]],
    filepath: str = "output/samsung_products.json",
    merge: bool = True,
    review_list: Optional[List[Dict[str, Any]]] = None,
    export_csv: bool = False,
) -> str:
    """
    Saves/merges Schema v2.0.0 Product Details to `filepath` AND paired Review Details to
    `get_reviews_json_path(filepath)`. Pass `export_csv=True` to also export CSVs.
    """
    if not laptop_list:
        return filepath

    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    reviews_filepath = get_reviews_json_path(filepath)

    existing_products: List[Dict[str, Any]] = []
    if merge and os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    existing_products = loaded
        except Exception:
            existing_products = []

    existing_reviews: List[Dict[str, Any]] = []
    if merge and os.path.exists(reviews_filepath):
        try:
            with open(reviews_filepath, "r", encoding="utf-8") as f:
                loaded_r = json.load(f)
                if isinstance(loaded_r, list):
                    existing_reviews = loaded_r
        except Exception:
            existing_reviews = []

    product_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    review_map: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for item in laptop_list + existing_products:
        if not isinstance(item, dict):
            continue
        item_copy = dict(item)
        item_copy.pop("_id", None)
        if is_v2_product_doc(item_copy):
            v2_p = normalize_metadata_scraped_at(item_copy)
            key = make_review_key(v2_p)
            if key[1] and key not in product_map:
                product_map[key] = v2_p
        else:
            v2_p, v2_r = convert_v1_to_v2(item_copy)
            key = make_review_key(v2_p)
            if key[1]:
                if key not in product_map:
                    product_map[key] = v2_p
                review_map.setdefault(key, v2_r)

    for rev in (review_list or []) + existing_reviews:
        if not isinstance(rev, dict) or not is_v2_review_doc(rev):
            continue
        r_copy = normalize_metadata_scraped_at(dict(rev))
        r_copy.pop("_id", None)
        r_key = make_review_key(r_copy)
        if r_key[1] and r_key not in review_map:
            review_map[r_key] = r_copy

    final_products = list(product_map.values())
    final_reviews = list(review_map.values())

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(final_products, f, indent=2, ensure_ascii=False)
    logger.info(
        f"📁 Stored {len(laptop_list)} product(s) [Schema v2.0.0] in JSON file: '{filepath}' (Total: {len(final_products)})"
    )

    if final_reviews:
        os.makedirs(os.path.dirname(reviews_filepath) or ".", exist_ok=True)
        with open(reviews_filepath, "w", encoding="utf-8") as rf:
            json.dump(final_reviews, rf, indent=2, ensure_ascii=False)
        logger.info(
            f"📁 Stored {len(final_reviews)} review bundle(s) [Schema v2.0.0] in JSON file: '{reviews_filepath}'"
        )

    if export_csv:
        csv_file = filepath.replace(".json", ".csv")
        reviews_csv_name = "samsung_product_reviews.csv" if "product" in filepath else "samsung_laptop_reviews.csv"
        reviews_csv = os.path.join(os.path.dirname(filepath) or ".", reviews_csv_name)
        save_laptops_to_csv(final_products, csv_file, reviews_csv, review_list=final_reviews)

    return filepath


def save_laptops_to_csv(
    laptop_list: List[Dict[str, Any]],
    csv_filepath: str = "output/samsung_products.csv",
    reviews_csv_filepath: str = "output/samsung_product_reviews.csv",
    review_list: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """
    Saves Schema v2.0.0 (or legacy v1) product and review bundles into Excel-compatible CSVs.
    """
    if not laptop_list:
        return {"laptops_csv": csv_filepath, "reviews_csv": reviews_csv_filepath}

    os.makedirs(os.path.dirname(csv_filepath) or ".", exist_ok=True)
    reviews_map = _build_reviews_map(review_list or [])

    product_fieldnames = [
        "platform",
        "marketplace_type",
        "region",
        "brand",
        "category",
        "product_id",
        "product_name",
        "current_price",
        "list_price",
        "discount_percentage",
        "currency",
        "is_in_stock",
        "introduced_date",
        "seller_name",
        "average_rating",
        "total_ratings",
        "total_written_reviews",
        "five_star_reviews",
        "four_star_reviews",
        "three_star_reviews",
        "two_star_reviews",
        "one_star_reviews",
        "images_count",
        "videos_count",
        "primary_image_url",
        "product_url",
    ]

    product_rows: List[Dict[str, Any]] = []
    reviews_rows: List[Dict[str, Any]] = []

    for item in laptop_list:
        if not isinstance(item, dict):
            continue
        key = make_review_key(item)
        paired_rev = reviews_map.get(key) or reviews_map.get(key[1])
        flat = extract_flat_summary(item, paired_rev)

        imgs = flat.get("images") or []
        primary_img = ""
        if imgs:
            first_im = imgs[0]
            primary_img = first_im.get("url", "") if isinstance(first_im, dict) else str(first_im)

        product_rows.append({
            "platform": flat["platform"],
            "marketplace_type": flat["marketplace_type"],
            "region": flat["region"],
            "brand": flat["brand"],
            "category": " > ".join(flat["category_path"]) if flat["category_path"] else flat["category"],
            "product_id": flat["product_id"],
            "product_name": flat["title"],
            "current_price": flat["current_price"] if flat["current_price"] is not None else "",
            "list_price": flat["list_price"] if flat["list_price"] is not None else "",
            "discount_percentage": flat["discount_percentage"] if flat["discount_percentage"] is not None else "",
            "currency": flat["currency"],
            "is_in_stock": flat["in_stock"],
            "introduced_date": flat["introduced_date"] or "",
            "seller_name": flat["seller_name"] or "",
            "average_rating": flat["average_rating"] if flat["average_rating"] is not None else "",
            "total_ratings": flat["total_ratings"],
            "total_written_reviews": flat["total_written_reviews"],
            "five_star_reviews": flat["five_star_count"],
            "four_star_reviews": flat["four_star_count"],
            "three_star_reviews": flat["three_star_count"],
            "two_star_reviews": flat["two_star_count"],
            "one_star_reviews": flat["one_star_count"],
            "images_count": flat["images_count"],
            "videos_count": flat["videos_count"],
            "primary_image_url": primary_img,
            "product_url": flat["product_url"],
        })

        for rev in flat.get("reviews_list") or []:
            if not isinstance(rev, dict):
                continue
            r_user = rev.get("reviewer") or rev.get("user_details") or {}
            reviews_rows.append({
                "platform": flat["platform"],
                "product_id": flat["product_id"],
                "product_name": flat["title"],
                "review_id": rev.get("review_id") or "",
                "star_rating": rev.get("rating") if rev.get("rating") is not None else "",
                "reviewer_name": r_user.get("name") or r_user.get("user_name") or "",
                "reviewer_id": r_user.get("user_id") or "",
                "country_code": r_user.get("country_code") or r_user.get("location") or "",
                "published_at": rev.get("published_at") or rev.get("submission_date") or "",
                "review_title": rev.get("title") or "",
                "review_body": rev.get("body") or rev.get("comment") or "",
            })

    try:
        with open(csv_filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=product_fieldnames)
            writer.writeheader()
            writer.writerows(product_rows)
        logger.info(f"📊 Stored {len(product_rows)} product(s) in CSV file: '{csv_filepath}'")
    except PermissionError:
        fallback_csv = csv_filepath.replace(".csv", "_latest.csv")
        try:
            with open(fallback_csv, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=product_fieldnames)
                writer.writeheader()
                writer.writerows(product_rows)
            logger.info(f"📊 Stored updated product(s) in: '{fallback_csv}'")
        except Exception:
            pass
        logger.warning(f"⚠️ Could not overwrite '{csv_filepath}' (open in Excel). Close Excel to overwrite.")

    if reviews_rows and reviews_csv_filepath:
        review_fieldnames = [
            "platform",
            "product_id",
            "product_name",
            "review_id",
            "star_rating",
            "reviewer_name",
            "reviewer_id",
            "country_code",
            "published_at",
            "review_title",
            "review_body",
        ]
        try:
            with open(reviews_csv_filepath, "w", newline="", encoding="utf-8-sig") as f:
                r_writer = csv.DictWriter(f, fieldnames=review_fieldnames)
                r_writer.writeheader()
                r_writer.writerows(reviews_rows)
            logger.info(f"📊 Stored {len(reviews_rows)} customer review(s) in CSV file: '{reviews_csv_filepath}'")
        except PermissionError:
            logger.warning(f"⚠️ Could not write to '{reviews_csv_filepath}' (open in Excel).")

    return {"laptops_csv": csv_filepath, "reviews_csv": reviews_csv_filepath}


def load_reviews_map_for_products_json(filepath: str) -> Dict[Any, Dict[str, Any]]:
    """Loads paired Schema v2.0.0 review JSON file matching a products JSON path."""
    rev_path = get_reviews_json_path(filepath)
    if not os.path.exists(rev_path):
        return {}
    try:
        with open(rev_path, "r", encoding="utf-8") as f:
            docs = json.load(f)
        if isinstance(docs, list):
            return _build_reviews_map(docs)
    except Exception:
        pass
    return {}


def query_json_with_reviews(
    filepath: str,
    flt: Optional[ProductFilter] = None,
) -> Tuple[List[Dict[str, Any]], Dict[Any, Dict[str, Any]]]:
    """Loads products + paired reviews from JSON files and applies ProductFilter rules."""
    if not os.path.exists(filepath):
        logger.warning(f"JSON file not found: '{filepath}'")
        return [], {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return [], {}
        reviews_by_key = load_reviews_map_for_products_json(filepath)
        return filter_and_sort_products(data, flt, reviews_by_key=reviews_by_key), reviews_by_key
    except Exception as e:
        logger.error(f"Failed to query JSON file '{filepath}': {e}")
        return [], {}


def query_json_file(
    filepath: str,
    flt: Optional[ProductFilter] = None,
) -> List[Dict[str, Any]]:
    """Load products from a local JSON file and apply ProductFilter search/filter/sort rules."""
    matched, _ = query_json_with_reviews(filepath, flt)
    return matched


save_products_to_csv = save_laptops_to_csv
save_products_to_json = save_laptops_to_json
