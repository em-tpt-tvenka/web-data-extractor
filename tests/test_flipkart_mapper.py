from app.platforms.flipkart.parsing import parse_spec_text, flatten_specs
from app.platforms.flipkart.mapper import _ProductContext, build_records
from app.schemas.scraping import ScrapeJobRequest

def test_flipkart_spec_text_parsing():
    sample_specs = """
Specifications
General
In The Box
Handset, USB Cable, Adapter, Quick Start Guide, Ejection Pin
Model Number
SM-E075FZGGINS
Model Name
Galaxy F07
Color
Green
Browse Type
Smartphones
SIM Type
Dual Sim
Display Features
Display Size
17.02 cm (6.7 inch)
Resolution
720 x 1600 Pixels
Memory & Storage Features
Internal Storage
64 GB
RAM
4 GB
Similar Products
"""
    parsed = parse_spec_text(sample_specs)
    assert len(parsed) >= 3
    assert "General" in parsed
    assert parsed["General"]["Model Number"] == "SM-E075FZGGINS"
    assert parsed["General"]["Color"] == "Green"
    assert "Display Features" in parsed
    assert parsed["Display Features"]["Display Size"] == "17.02 cm (6.7 inch)"
    assert "Memory & Storage Features" in parsed
    assert parsed["Memory & Storage Features"]["Internal Storage"] == "64 GB"

def test_flipkart_taxonomy_and_price_range():
    parsed = {
        "General": {
            "Model Number": "SM-E075FZGGINS",
            "Model Name": "Galaxy F07",
            "Color": "Green"
        }
    }
    model = {
        "product_id": "MOBHMGHHXHYZEAVB",
        "product_name": "Samsung Galaxy F07 (Green, 64 GB)",
        "specifications": parsed,
        "specifications_flat": flatten_specs(parsed),
        "price": {
            "selling_price": 11399.0,
            "mrp": 11999.0,
            "currency": "INR"
        },
        "breadcrumbs": ["Home", "Mobiles & Accessories", "Mobiles", "Samsung Mobiles"],
        "manufacturer": {
            "generic_name": "Mobiles",
            "country_of_origin": "India",
            "name": "Samsung"
        }
    }

    req = ScrapeJobRequest(
        platform="flipkart",
        search={"product": "mobile phone Samsung", "marketplace": "flipkart.com"}
    )

    ctx = _ProductContext(model, req)
    tax = ctx.taxonomy(req)
    comm = ctx.commercial()
    attr = ctx.product_attributes()

    # Taxonomy checks
    assert tax.industry == "Electronics"
    assert tax.category == "Mobiles & Accessories"
    assert tax.subcategory == "Mobiles"
    assert tax.product_type == "Samsung Mobiles"
    assert tax.mapping_status == "mapped"

    # Price range checks
    assert comm.price_range.minimum == 11399.0
    assert comm.price_range.maximum == 11999.0
    assert comm.price_range.currency == "INR"

    # Price checks
    assert comm.price.selling_price.amount == 11399.0
    assert comm.price.listed_price.amount == 11999.0
    assert comm.price.original_price.amount == 11999.0

    # Product attributes checks
    assert attr.model_number == "SM-E075FZGGINS"
    assert attr.color == "Green"
    assert len(attr.specifications) == 1

def test_flipkart_taxonomy_fallback():
    model = {
        "product_id": "MOBHMGHHXHYZEAVB",
        "product_name": "Samsung Galaxy F07 (Green, 64 GB)",
        "price": {"selling_price": 11399.0, "currency": "INR"},
        "breadcrumbs": [],
        "manufacturer": {"generic_name": "Mobiles"}
    }
    req = ScrapeJobRequest(
        platform="flipkart",
        search={"product": "mobile phone Samsung", "marketplace": "flipkart.com"}
    )
    ctx = _ProductContext(model, req)
    tax = ctx.taxonomy(req)
    assert tax.industry == "Electronics"
    assert tax.category == "Mobiles & Accessories"
    assert tax.subcategory == "Mobiles"
    assert tax.product_type == "Smartphones"
    assert tax.mapping_status == "mapped"

def test_flipkart_beauty_product_mapping():
    from app.platforms.flipkart.parsing import extract_manufacturer, parse_capacity
    mfg = extract_manufacturer("Manufacturing, Packaging and Import Info\nShow More", {}, brand="MAYBELLINE NEW YORK")
    assert mfg["name"] == "MAYBELLINE NEW YORK"
    assert mfg["manufactured_by"] == "MAYBELLINE NEW YORK"

    model = {
        "product_id": "LSKGH8YYG9XDWZC8",
        "product_name": "MAYBELLINE NEW YORK Superstay Vinyl Ink Liquid Lipstick",
        "specifications": {
            "General": {
                "Shade": "Vinyl Ink Red",
                "Quantity": "4.2 ml",
                "Finish": "Glossy"
            }
        },
        "specifications_flat": {
            "General | Shade": "Vinyl Ink Red",
            "General | Quantity": "4.2 ml",
            "General | Finish": "Glossy"
        },
        "price": {"selling_price": 353.0, "mrp": 849.0, "currency": "INR"},
        "brand": "MAYBELLINE NEW YORK",
        "manufacturer": mfg
    }
    req = ScrapeJobRequest(
        platform="flipkart",
        search={"product": "lipstick MAYBELLINE NEW YORK", "marketplace": "flipkart.com"}
    )
    ctx = _ProductContext(model, req)
    tax = ctx.taxonomy(req)
    attr = ctx.product_attributes()

    assert tax.industry == "Beauty & Personal Care"
    assert tax.category == "Beauty and Grooming"
    assert tax.subcategory == "Makeup"
    assert tax.product_type == "Lipstick"
    assert attr.color == "Vinyl Ink Red"
    assert attr.capacity.value == 4.2
    assert attr.capacity.unit == "ml"
    assert attr.manufacturer["name"] == "MAYBELLINE NEW YORK"

