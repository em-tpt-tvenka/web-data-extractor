"""products / retailers / reviews split for every platform record shape.

Records come from each platform's real mapper (no network). The MongoDB test
uses a throwaway database and is skipped when MongoDB is not reachable."""

from __future__ import annotations

import asyncio
import copy

import pytest

from app.platforms.amazon import mapper as amz
from app.platforms.bestbuy import mapper as bby
from app.platforms.flipkart import mapper as flp
from app.platforms.samsung import mapper as sms
from app.platforms.walmart import mapper as wmt
from app.processing.collection_splitter import split_records
from app.schemas.scraping import ScrapeJobRequest

WIDE = {"date_range": {"type": "custom", "start_date": "2000-01-01", "end_date": "2100-01-01"}}


def _req(platform, marketplace):
    return ScrapeJobRequest(platform=platform, search={"product": "laptop", "marketplace": marketplace},
                            scraping_options=WIDE)


def _review(i, **over):
    r = {"review_id": f"RV{i}", "rating": 4.0, "title": f"Title {i}", "review_text": f"Review body {i}",
         "reviewer_name": f"User {i}", "verified_purchase": True, "posted_date": "2025-06-10",
         "helpful_count": i}
    r.update(over)
    return r


def _generic_model(pid, **over):
    m = {"product_id": pid, "product_name": f"Product {pid}", "url": f"https://shop.example/{pid}",
         "brand": "Acme", "description": "A product.", "model_number": "M-1",
         # Flipkart reads "mrp"; Walmart / Best Buy read "original_price".
         "price": {"currency": "USD", "selling_price": 90.0, "mrp": 100.0, "original_price": 100.0},
         "ratings": {"average_rating": 4.1, "total_reviews": 2},
         "specifications": {"General": {"Colour": "Black"}},
         "specifications_flat": {"General | Colour": "Black"},
         "images": ["https://img.example/1.jpg"], "videos": [],
         "reviews": [_review(1), _review(2)], "scraped_at": "2026-11-20T00:00:00+00:00"}
    m.update(over)
    return m


def flipkart_records():
    model = _generic_model("MOBX1", price={"currency": "INR", "selling_price": 900.0, "mrp": 1000.0},
                           reviews=[_review(1, review_id=None), _review(2, review_id=None)],
                           ratings={"average_rating": 4.2, "total_ratings": 50, "total_reviews": 2,
                                    "breakdown": {"5_star": 40, "1_star": 10}})
    return flp.build_records(model, _req("flipkart", "flipkart.com"), "flp_job_t")


def amazon_records():
    from tests.test_amazon_mapper import _model
    return amz.build_records(_model(), _req("amazon", "amazon.in"), "amz_job_t")


def samsung_records():
    from tests.test_samsung_mapper import _model, _review as s_review
    model = _model(reviews=[s_review(review_id="S1"), s_review(review_id="S2", review_text="Second one")])
    return sms.build_records(model, _req("samsung", "samsung.com"), "sms_job_t")


def walmart_records():
    return wmt.build_records(_generic_model("19978269940"), _req("walmart", "walmart.com"), "wmt_job_t")


def bestbuy_records():
    return bby.build_records(_generic_model("6571234"), _req("bestbuy", "bestbuy.com"), "bb_job_t")


def alibaba_record_dicts():
    """Alibaba review-per-record shape incl. supplier feedback (mapper input is
    a large nested model, so the record dicts are built directly)."""
    base = {
        "record_type": "social_mention", "tenant": {"tenant_id": "ten_x", "project_id": "prj_x"},
        "entities": {"brands": [], "products": [{"product_id": "1601597779772", "product_name": "Steel Bottle",
                                                 "model_number": "JKW-DK750",
                                                 "product_url": "https://www.alibaba.com/p/1601597779772.html"}],
                     "retailer": {"retailer_id": "ali-200267213", "retailer_name": "Zhejiang Jiakang"}},
        "product_attributes": {"model_number": "JKW-DK750", "specifications": {"Material": "Steel"}},
        "commercial": {"price": {"selling_price": {"amount": 2.5, "currency": "USD"}},
                       "availability": {"status": "in_stock"}},
        "taxonomy": {"category": "Bottles"}, "metrics": {"rating": {"value": 5.0}},
        "quality": {"status": "accepted"}, "provenance": {"collection_run_id": "ali_job_t"},
    }
    out = []
    for i, kind in enumerate(["review", "supplier_feedback"]):
        d = copy.deepcopy(base)
        d["_id"] = f"ali_mnt_{i}"
        d["source"] = {"platform": "alibaba", "source_type": kind, "source_item_id": f"gen:{i}",
                       "parent_item_id": "1601597779772", "published_at": "2025-01-01"}
        d["content"] = {"text": f"very good {i}", "media": [{"media_type": "image", "url": f"https://i/{i}.jpg"}]}
        d["author"] = {"display_name": "P****s", "author_type": "buyer"}
        out.append(d)
    return out


PLATFORMS = {
    "flipkart": flipkart_records, "amazon": amazon_records, "samsung": samsung_records,
    "walmart": walmart_records, "bestbuy": bestbuy_records, "alibaba": alibaba_record_dicts,
}
EXPECTED_REVIEWS = {"flipkart": 2, "amazon": 2, "samsung": 2, "walmart": 2, "bestbuy": 2, "alibaba": 2}


@pytest.mark.parametrize("platform", list(PLATFORMS))
def test_split_one_product_one_retailer_all_reviews(platform):
    split = split_records(PLATFORMS[platform]())
    assert len(split["products"]) == 1 and len(split["retailers"]) == 1
    assert len(split["reviews"]) == EXPECTED_REVIEWS[platform]
    p, rt = split["products"][0], split["retailers"][0]
    assert p["platform"] == rt["platform"] == platform
    assert p["_id"] == f"{platform}:{p['product_id']}"
    assert rt["product_id"] == p["product_id"] and rt["retailer_id"]
    assert p["title"] and p["product_url"]
    for r in split["reviews"]:
        assert r["platform"] == platform and r["product_id"] == p["product_id"]
        assert r["_id"] == f"{platform}:{p['product_id']}:{r['review_id']}"
        assert r["review_text"]


def test_no_review_data_lost_vs_record():
    """Every embedded review field in the original JSON reaches the reviews docs."""
    rec = amazon_records()[0].to_mongo()
    docs = {d["review_id"]: d for d in split_records([rec])["reviews"]}
    for r in rec["reviews"]:
        d = docs[r["review_id"]]
        assert (d["reviewer_name"], d["title"], d["review_text"], d["rating"], d["review_date"],
                d["verified_purchase"], d["helpful_votes"]) == (
            r["reviewer_name"], r["title"], r["review_text"], r["rating"], r["posted_date"],
            r["verified_purchase"], r["helpful_count"])
        assert d["platform_attributes"]["variant"] == r["variant"]  # platform-specific kept


def test_product_and_retailer_carry_listing_data():
    rec = amazon_records()[0].to_mongo()
    split = split_records([rec])
    p, rt = split["products"][0], split["retailers"][0]
    assert p["specifications"] == rec["product_attributes"]["specifications"]
    assert p["images"] and p["videos"] and p["rating_distribution"] == rec["metrics"]["rating_distribution"]
    assert rt["current_price"] == rec["commercial"]["price"]["selling_price"]["amount"]
    assert rt["seller"]["seller_name"] == "Darshita Etel" and rt["currency"] == "INR"


def test_same_id_on_two_platforms_is_two_products():
    a = walmart_records()
    b = bestbuy_records()
    for r in b:  # force an identical raw product id on another platform
        r.entities.products[0].product_id = "19978269940"
        r.source.parent_item_id = "19978269940"
    ids = {p["_id"] for p in split_records(a + b)["products"]}
    assert ids == {"walmart:19978269940", "bestbuy:19978269940"}


def test_placeholder_product_ids_do_not_merge_products():
    a = walmart_records()
    b = walmart_records()
    for r in a:
        r.entities.products[0].product_id = "product"
    for r in b:
        r.entities.products[0].product_id = "product"
        r.entities.products[0].product_url = "https://shop.example/other"
    assert len(split_records(a + b)["products"]) == 2


def test_rejected_records_are_skipped():
    recs = walmart_records()
    for r in recs:
        r.quality.status = "rejected"
    assert split_records(recs) == {"products": [], "retailers": [], "reviews": []}


# ── MongoDB (throwaway database) ────────────────────────────────────────────

def test_mongo_upsert_dedupe_and_null_preservation():
    from motor.motor_asyncio import AsyncIOMotorClient
    from app.core.config import settings
    from app.storage.repositories import collections_repository as cr

    records = [r for f in PLATFORMS.values() for r in f()]
    wm = walmart_records()
    for r in wm:  # a later scrape that lacks some values
        if r.record_type == "product_listing":
            r.content.text = None
        r.commercial.price.original_price = None

    async def run():
        client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=1500)
        try:
            await client.admin.command("ping")
        except Exception:
            pytest.skip("MongoDB not reachable")
        db = client["wde_test_collections"]
        await client.drop_database(db.name)
        repo = cr.CollectionsRepository()
        repo.__class__._indexed_dbs = set()
        orig = cr.get_db
        cr.get_db = lambda: db
        try:
            first = await repo.save_scraped_data(records)
            counts1 = {c: await db[c].count_documents({}) for c in ("products", "retailers", "reviews")}
            second = await repo.save_scraped_data(records)  # repeated scrape
            counts2 = {c: await db[c].count_documents({}) for c in ("products", "retailers", "reviews")}

            # A later scrape that lacks a value must not wipe the stored one.
            await repo.save_scraped_data(wm)
            stored = await db.retailers.find_one({"_id": "walmart:19978269940:RETAILER_WALMART"})
            idx = await db.reviews.index_information()
            return first, second, counts1, counts2, stored, idx
        finally:
            cr.get_db = orig
            await client.drop_database(db.name)

    first, second, c1, c2, stored, idx = asyncio.run(run())
    assert c1 == {"products": 6, "retailers": 6, "reviews": sum(EXPECTED_REVIEWS.values())}
    assert c2 == c1  # no duplicates on re-run
    assert first["products"]["inserted"] == 6 and second["products"]["inserted"] == 0
    assert second["reviews"]["updated"] == c1["reviews"]
    assert stored["original_price"] == 100.0  # null in the later scrape did not overwrite
    assert stored["first_seen_at"] <= stored["last_seen_at"] and stored["source_record_ids"]
    assert idx["uq_platform_product_review"]["unique"] is True
