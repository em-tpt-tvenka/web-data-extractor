


REVIEWS_JS = r"""
() => {
    const tabs = {};
    document.querySelectorAll('*').forEach(el => {
        if (el.children.length > 0) return;
        const m = el.textContent.trim().match(/^(.+?)\s*\((\d+)\)$/);
        if (m && /review|feedback|evaluate|store|product|supplier/i.test(m[1]))
            tabs[m[1].trim()] = parseInt(m[2]);
    });

    const bodyText = document.body ? (document.body.innerText || '') : '';
    let overallRating = 'N/A';
    const ratingM = bodyText.match(/\b([1-5]\.[0-9])\b/);
    if (ratingM) overallRating = ratingM[1];

    const MASK = /\b[A-Za-z]\*{2,}[a-z]?\b/;
    const META = /^(verified purchase|helpful|repeat buyer|show (more|original)|with photos|report abuse|translated|original|\d+\s*(star|sold)|model\s*(number|no))/i;

    const seen = new Set();
    let cards = [];
    const listSels = ['.product-review-list', '.review-layout', '[class*="product-review-list"]',
                      '[class*="feedback-list"]', '[class*="review-list"]'];
    function looksReview(el) {
        if (!el) return false;
        const t = (el.innerText || '').trim();
        if (t.length < 8) return false;
        return MASK.test(t) || /verified\s+purchase/i.test(t) ||
               !!el.querySelector('.star-rating-list,[class*="star-rating"]');
    }
    function pushCard(el) {
        if (!el || seen.has(el)) return;
        if (!looksReview(el)) return;
        seen.add(el); cards.push(el);
    }
    for (const sel of listSels) {
        document.querySelectorAll(sel).forEach(list => {
            Array.from(list.children).forEach(pushCard);
        });
        if (cards.length) break;
    }
    if (cards.length === 0) {
        const tw = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, null);
        let tn;
        while ((tn = tw.nextNode())) {
            if (!MASK.test(tn.textContent)) continue;
            let el = tn.parentElement;
            for (let i = 0; i < 5 && el && el !== document.body; i++) {
                const t = (el.innerText || '');
                if (t.length > 40 && (/verified\s+purchase/i.test(t) ||
                    /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}/.test(t) ||
                    el.querySelector('.star-rating-list,[class*="star-rating"]'))) { pushCard(el); break; }
                el = el.parentElement;
            }
        }
    }

    function parseStars(card) {
        const lists = card.querySelectorAll('.star-rating-list, [class*="star-rating"]');
        for (const lst of lists) {
            let fills = 0;
            lst.querySelectorAll('use').forEach(u => {
                const href = u.getAttribute('xlink:href') || u.getAttribute('href') || '';
                if (/star-fill|star_on|-on$/i.test(href)) fills++;
            });
            if (fills >= 1 && fills <= 5) return fills;
        }
        const ov = card.querySelector('[class*="star"] [style*="width"],[class*="rate"] [style*="width"]');
        if (ov) { const w = (ov.getAttribute('style') || '').match(/width:\s*([\d.]+)%/);
                  if (w) { const p = parseFloat(w[1]); if (p > 0 && p <= 100) return Math.round(p / 20); } }
        const a = card.querySelector('[aria-label*="out of"],[aria-label*="star"],[aria-label*="rating"],[title*="star"]');
        if (a) { const v = (a.getAttribute('aria-label') || a.getAttribute('title') || '')
                    .match(/([1-5])(?:\.\d)?\s*(?:out\s*of\s*5|stars?)/i); if (v) return parseInt(v[1]); }
        const tm = (card.innerText || '').match(/\b([1-5])\s*-?\s*stars?\b/i);
        if (tm) return parseInt(tm[1]);
        return null;
    }

    const reviews = cards.slice(0, 2000).map(card => {
        const raw   = (card.innerText || card.textContent || '').trim();
        const lines = raw.split('\n').map(s => s.trim()).filter(Boolean);

        const maskedM  = raw.match(MASK);
        const reviewer = maskedM ? maskedM[0]
            : (lines.find(l => l.length < 30 && /^[A-Z]/.test(l) && !META.test(l)) || 'N/A');

        const dateMatch = raw.match(/\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|\d{4}[-\/]\d{2}[-\/]\d{2}/i);
        const flagM   = raw.match(/[\u{1F1E0}-\u{1F1FF}]{2}\s*([A-Za-z][a-z ]{1,20})/u);
        const ctLines = lines.filter(l => l.length < 28 && /^[A-Z][a-z]/.test(l) && l !== reviewer
                                      && !META.test(l) && !/\d{4}/.test(l));
        const country = (flagM ? flagM[1].trim() : null) || ctLines[0] || 'N/A';

        const textCands = lines.filter(l => l.length > 12 && l !== reviewer && l !== country
            && !META.test(l) && !(dateMatch && l.indexOf(dateMatch[0]) >= 0) && !/^helpful/i.test(l));
        const reviewText = textCands.length ? textCands.reduce((a, b) => a.length >= b.length ? a : b) : 'N/A';

        const stars = parseStars(card);
        const modelMatch = raw.match(/model\s*(?:number|no\.?|num\.?)?[:\s]+([A-Z0-9][\w\-]{2,})/i);

        const imgs = Array.from(card.querySelectorAll('img')).map(img =>
            img.src || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || ''
        ).filter(s => s.startsWith('http') && !/flag|avatar|\.svg|tps-\d{1,2}-\d{1,2}\./i.test(s));
        const vids = Array.from(card.querySelectorAll('video[src],video source,[class*="video"] source'))
            .map(v => v.src || v.getAttribute('src') || v.getAttribute('data-src') || '')
            .filter(s => s.startsWith('http'));

        return {
            reviewer,
            country,
            date:              dateMatch ? dateMatch[0] : 'N/A',
            stars:             stars === null ? 'N/A' : String(stars),
            text:              reviewText,
            model_number:      modelMatch ? modelMatch[1] : 'N/A',
            verified_purchase: /verified\s+purchase/i.test(raw),
            repeat_buyer:      /repeat\s+buyer/i.test(raw),
            review_media:      { images: imgs, videos: vids },
        };
    });

    let datePosted = 'N/A';
    const dtPatterns = [
        /(?:posted|published|post\s*date)[:\s]+([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})/i,
        /(?:posted|published|post\s*date)[:\s]+(\d{4}[-\/]\d{2}[-\/]\d{2})/i,
        /(?:updated|update\s*date|last\s*updated)[:\s]+([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})/i,
        /(?:updated|update\s*date|last\s*updated)[:\s]+(\d{4}[-\/]\d{2}[-\/]\d{2})/i,
    ];
    for (const pat of dtPatterns) {
        const m = bodyText.match(pat);
        if (m) { datePosted = m[1].trim(); break; }
    }

    return { overall_rating: overallRating, tabs, reviews,
             total_reviews: reviews.length, date_posted: datePosted };
}
"""

SELLER_JS = """
() => {
    const dd = window.detailData;
    if (!dd || !dd.globalData) return null;
    const g = dd.globalData;
    const s = g.seller || {};
    const out = { seller: {}, store_review_summary: null };

    ['companyId','companyName','companyBusinessType','companyJoinYears',
     'companyRegisterCountry','companyEncryptId','companyProfileUrl',
     'companyVideoUrl','supplierOnTimeDeliveryRate','responseTimeText',
     'subDomain','localCompanyJoinYears','feedbackUrl',
     'supplierReorderRate','reorderRate','repeatPurchaseRate','repeatBuyerRate',
     'buyerRepeatRate'].forEach(k => {
        if (s[k] !== undefined && s[k] !== null) out.seller[k] = s[k];
    });
    if (!out.seller.feedbackUrl && s.subDomain)
        out.seller.feedbackUrl = 'https://' + s.subDomain + '/company_profile/feedback.html';
    if (s.baoAccountIsDisplayAssurance !== undefined)
        out.seller.tradeAssurance = !!s.baoAccountIsDisplayAssurance;
    if (s.topRatedSupplier) {
        out.seller.topRatedSupplier = !!s.topRatedSupplier.text;
        if (s.topRatedSupplier.text) out.seller.topRatedSupplierText = s.topRatedSupplier.text;
    }
    if (s.tradeHalfYear) {
        out.seller.tradeOrderAmount6m   = s.tradeHalfYear.ordAmt6m;
        out.seller.tradeOrderCount6m    = s.tradeHalfYear.ordCnt6m;
        out.seller.tradeOrderAmountText = s.tradeHalfYear.ordAmt;
    }

    const srr = s.supplierRatingReviews;
    if (srr) {
        const sub = {};
        let total = 0;
        (srr.latitudeScoreDTOList || []).forEach(lat => {
            const breakdown = {};
            let latTotal = 0;
            (lat.scoreScatterDTOList || []).forEach(sc => {
                breakdown[String(sc.scatterValue)] = sc.scatterCount;
                latTotal += (sc.scatterCount || 0);
            });
            if (latTotal > total) total = latTotal;
            sub[lat.latitudeType] = {
                score:          lat.latitudeAverageScore,
                star_breakdown: breakdown,
            };
        });
        out.store_review_summary = {
            averageStar:   srr.averageStar || 'N/A',
            total_reviews: total,
            sub_ratings:   sub,
        };
    }
    // Pages without the supplier rating block still show the store rating.
    const strv = (g.review || {}).storeReview;
    if (!out.store_review_summary && strv && (strv.averageStar !== undefined || strv.totalReviewCount !== undefined)) {
        out.store_review_summary = {
            averageStar:   strv.averageStar !== undefined ? strv.averageStar : 'N/A',
            total_reviews: strv.totalReviewCount !== undefined ? strv.totalReviewCount : null,
            sub_ratings:   {},
        };
    }

    const prod  = g.product || {};
    const items = prod.mediaItems || [];
    const imgs = [], vids = [];
    items.forEach(it => {
        if (!it || typeof it !== 'object') return;
        if (it.type === 'video' || it.group === 'video') {
            const vu = it.videoUrl || {};
            let best = null;
            ['hd','sd','ld'].forEach(q => { if (!best && vu[q] && vu[q].videoUrl) best = vu[q]; });
            if (!best) for (const k in vu) { if (vu[k] && vu[k].videoUrl) { best = vu[k]; break; } }
            if (best && best.videoUrl) vids.push({
                videoId:   it.videoId || null,
                url:       best.videoUrl,
                cover_url: it.videoCoverUrl || '',
                duration:  it.duration || null,
                width:     it.width || null,
                height:    it.height || null,
                file_size: it.fileSizeFormatStr || '',
            });
        } else {
            const iu = it.imageUrl || {};
            const u = iu.big || iu.normal || iu.small || '';
            if (u) imgs.push(u);
        }
    });
    if (imgs.length || vids.length) out.product_media = { images: imgs, videos: vids };

    const pr = g.product || {};
    // Industry-specific attributes ("Key attributes" on the page) come after the
    // basic / other lists so they never override a value those lists give.
    const specsList = [].concat(pr.productBasicProperties || [], pr.productOtherProperties || [],
                                pr.productKeyIndustryProperties || []);
    const specs = {};
    specsList.forEach(a => { if (a && a.attrName && !(a.attrName in specs)) specs[a.attrName] = a.attrValue; });
    // Same rows grouped by the page's own attribute lists (Amazon-style sections).
    const specSections = {};
    const seenAttr = new Set();
    [['Basic Properties', pr.productBasicProperties], ['Other Properties', pr.productOtherProperties],
     ['Industry-specific Attributes', pr.productKeyIndustryProperties]]
        .forEach(([label, list]) => (list || []).forEach(a => {
            if (!a || !a.attrName || seenAttr.has(a.attrName)) return;
            seenAttr.add(a.attrName);
            (specSections[label] = specSections[label] || {})[a.attrName] = a.attrValue;
        }));
    const pick = (...names) => {
        for (const n of names) for (const k in specs)
            if (k.toLowerCase() === n) return specs[k];
        return null;
    };
    const pickLike = (...subs) => {
        for (const sub of subs) for (const k in specs)
            if (k.toLowerCase().includes(sub)) return specs[k];
        return null;
    };

    const sku = pr.sku || {};
    const variants = (sku.skuAttrs || []).map(a => ({
        name: a.name, type: a.type, id: (a.id !== undefined ? a.id : null),
        values: (a.values || []).map(v => v.name).filter(Boolean),
        // Per option: the page's attribute-value id (keys skuInfoMap) and swatch image.
        options: (a.values || []).filter(v => v && v.name).map(v => ({
            name: v.name,
            value_id: (v.id !== undefined ? v.id : null),
            image: v.originImage || v.largeImage || null,
        })),
    }));
    // "attrId:valueId;attrId:valueId;" -> SKU id, one entry per purchasable SKU.
    const skuMap = Object.entries(sku.skuInfoMap || {})
        .map(([combo, info]) => ({ combo: combo, sku_id: (info && info.id !== undefined) ? info.id : null }))
        .filter(e => e.sku_id !== null);
    const variantModel = (() => {
        for (const v of variants) {
            const nm = String(v.name || '').toLowerCase();
            if ((nm === 'model number' || nm === 'model' || nm === 'model no.' ||
                 nm === 'model no') && (v.values || []).length) return v.values[0];
        }
        return null;
    })();
    const colors = (sku.skuAttrs || [])
        .filter(a => String(a.type).toUpperCase() === 'COLOR' || /colou?r/i.test(a.name || ''))
        .reduce((acc, a) => acc.concat((a.values || []).map(v => v.name)), []).filter(Boolean);
    const sizes = (sku.skuAttrs || [])
        .filter(a => /\\bsize\\b/i.test(a.name || ''))
        .reduce((acc, a) => acc.concat((a.values || []).map(v => v.name)), []).filter(Boolean);

    const price = pr.price || {};
    const tiers = (price.productLadderPrices || []).map(t => ({
        min_quantity: t.min, max_quantity: (t.max === -1 ? null : t.max),
        price: t.price, formatted_price: t.formatPrice, usd_price: t.dollarPrice,
    }));
    // Products sold at a price range (no quantity ladder) carry it, with the
    // promotional range when a discount runs, in productRangePrices.
    const num = (v) => { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : null; };
    const rp = price.productRangePrices || null;
    const range = rp ? {
        min:           num(rp.priceRangeLow),
        max:           num(rp.priceRangeHigh),
        promo_min:     num(rp.priceRangePromotionLow),
        promo_max:     num(rp.priceRangePromotionHigh),
        usd_min:       num(rp.dollarPriceRangeLow),
        usd_max:       num(rp.dollarPriceRangeHigh),
        usd_promo_min: num(rp.dollarPriceRangePromotionLow),
        usd_promo_max: num(rp.dollarPriceRangePromotionHigh),
        text:          rp.priceRangeText || null,
        promo_text:    rp.pricePromotionRangeText || null,
    } : null;

    const usdNums = tiers.map(t => Number(t.usd_price))
        .filter(x => Number.isFinite(x) && x > 0);
    const flatUsd = [price.dollarPrice, price.minDollarPrice, price.maxDollarPrice]
        .map(Number).filter(x => Number.isFinite(x) && x > 0);
    const rangeUsd = !range ? [] : (range.usd_promo_min !== null
        ? [range.usd_promo_min, range.usd_promo_max] : [range.usd_min, range.usd_max])
        .filter(x => x !== null);
    const usdPool = usdNums.length ? usdNums : (flatUsd.length ? flatUsd : rangeUsd);
    const priceUsdMin = usdPool.length ? Math.min(...usdPool) : null;
    const priceUsdMax = usdPool.length ? Math.max(...usdPool) : null;

    const promo = g.promotion || {};
    const di = promo.discountInfo || {};
    const promotion = (di.discountRate || di.text) ? {
        discount_rate: di.discountRate !== undefined ? di.discountRate : null,
        text:          di.text || null,
        type:          di.type || null,
        source:        di.discountSource || null,
        start_time:    di.startTime || null,
        end_time:      di.endTime || null,
        status:        (promo.availableActivity || {}).status || null,
    } : null;

    // Stock per SKU and dispatch place, as the page's inventory data gives it.
    const inv = g.inventory || {};
    const stockCounts = [];
    Object.values(inv.repoSkuInventory || {}).forEach(place =>
        Object.values((place || {}).skuMap || {}).forEach(s => {
            const n = Number((s || {}).inventoryCount);
            if (Number.isFinite(n)) stockCounts.push(n);
        }));
    if (!stockCounts.length) Object.values(inv.skuInventory || {}).forEach(s =>
        ((s || {}).warehouseInventoryList || []).forEach(w => {
            const n = Number((w || {}).inventoryCount);
            if (Number.isFinite(n)) stockCounts.push(n);
        }));
    const inventory = (Object.keys(inv).length || pr.tradable !== undefined) ? {
        sku_stock:       stockCounts,
        dispatch_places: (inv.placeOfDispatches || []).map(p => p && p.name).filter(Boolean),
        ships_to:        inv.shippingToCountry || null,
        tradable:        pr.tradable === undefined ? null : !!pr.tradable,
    } : null;

    const bc = ((g.seo || {}).breadCrumb || {}).pathList || [];
    const categoryPath = bc.map(x => (x.hrefObject || {}).name).filter(Boolean);

    const trade = g.trade || {};
    const toLead = (l) => ({ min_quantity: l.minQuantity, max_quantity: l.maxQuantity, days: l.processPeriod });
    let lead = (((trade.leadTimeInfo || {}).ladderPeriodList) || []).map(toLead);
    if (!lead.length) {
        // Same ladder, keyed by warehouse, on newer pages.
        const wl = trade.warehouseLeadTimeInfo || {};
        const wlList = wl.DEFAULT || Object.values(wl)[0];
        lead = (Array.isArray(wlList) ? wlList : []).filter(Boolean).map(toLead);
    }
    const logi = trade.logisticInfo || {};

    const certRaw = pick('certification', 'certifications', 'certificate',
                         'certificates', 'certification(s)', 'product certification') ||
                    pickLike('certificat');
    const certifications = certRaw
        ? [...new Set(String(certRaw).split(/[,;|、]+/).map(x => x.trim()).filter(Boolean))]
        : [];

    const afterSales = pick('after-sales service', 'after-sale service',
                            'after sales service') || pickLike('after-sale', 'after sales');
    const returnsInfo = pick('return policy', 'returns', 'return & refund',
                             'return and refund', 'refund policy') ||
                        pickLike('return policy', 'return & refund', 'return and refund') ||
                        ((afterSales && /return|refund|replace/i.test(afterSales)) ? afterSales : null);

    out.product_ex = {
        product_id:      pr.productId || null,
        category_id:     pr.productCategoryId || pr.productLeafId || null,
        category_path:   categoryPath,
        brand:           pick('brand name', 'brand'),
        model_number:    variantModel || pick('model number', 'model', 'model no.'),
        material:        pick('body material', 'material', 'product material'),
        weight:          pickLike('net weight', 'weight (net') || pick('weight') ||
                         pickLike('weight') || (logi.unitWeight ? logi.unitWeight + ' KG' : null),
        dimensions:      pickLike('dimension', 'product size', 'overall size') ||
                         pick('dimensions', 'size') || logi.unitSize || null,
        condition:       pick('products status', 'product status', 'condition'),
        warranty:        pick('warranty(year)', 'warranty', 'warranty period', 'warranty time') ||
                         pickLike('warranty', 'guarantee'),
        after_sales_service: afterSales,
        returns:         returnsInfo,
        place_of_origin: pick('place of origin'),
        colors:          colors,
        sizes:           sizes,
        variants:        variants,
        sku_map:         skuMap,
        specifications:  specs,
        specification_sections: specSections,
        price_tiers:     tiers,
        price_usd_min:   priceUsdMin,
        price_usd_max:   priceUsdMax,
        price_keys:      Object.keys(price),
        price_unit:      price.unit || price.unitEven || null,
        price_currency_pattern: (price.currencyRule || {}).currencyPattern || null,
        price_range_detail: range,
        promotion:       promotion,
        inventory:       inventory,
        batch_number:    price.batchNum || null,
        moq_value:       (typeof pr.moq === 'number' ? pr.moq : null),
        box_moq:         pr.boxMoq || null,
        shipping_origin: pr.deliverPlace || null,
        lead_time:       lead,
        unit_size:       logi.unitSize || null,
        unit_weight:     logi.unitWeight || null,
        customization: {
            light: !!pr.supportLightCustomization,
            fast:  !!pr.supportFastCustomization,
            items: pr.productLightCustomizationList || [],
        },
        description:    null,
        certifications: certifications,
    };

    const prv = (g.review || {}).productReview || {};
    out.product_review_summary = {
        average_star:       (prv.averageStar !== undefined ? prv.averageStar : null),
        total_review_count: (prv.totalReviewCount !== undefined ? prv.totalReviewCount : null),
        // diagnostic only: what the page's own review data contains
        review_keys:        Object.keys((g.review || {})),
        product_review_keys: Object.keys(prv),
    };

    return out;
}
"""

# Supplier's product description (Amazon: #productDescription + feature
# bullets). Alibaba renders it in one of two layouts:
#   * structured: a "Highlights at a glance" bullet list (+ optional text
#     blocks) inside [data-testid="module-structure-description"]; the
#     "About this supplier" block sits outside that element and is not read;
#   * iframe: a supplier-authored HTML document (descIframe.html), read by the
#     engine from the frame itself.
DESCRIPTION_JS = r"""
() => {
    const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
    const out = { layout: null, highlights: [], paragraphs: [], iframe_url: null };
    const root = document.querySelector('[data-testid="module-structure-description"]') ||
                 document.querySelector('[data-module-name*="productDescription" i]');
    if (root) {
        out.layout = 'structured';
        const head = [...root.querySelectorAll('div,h2,h3,h4,span')].find(
            e => e.children.length === 0 && /^highlights at a glance$/i.test(clean(e.innerText)));
        const box = head ? head.parentElement : null;
        root.querySelectorAll('[data-testid="product-detail-text-sort"]').forEach(e => {
            const t = clean(e.innerText);
            if (!t) return;
            (box && box.contains(e) ? out.highlights : out.paragraphs).push(t);
        });
    }
    const fr = document.querySelector('[data-module-name*="description" i] iframe[src*="descIframe" i]') ||
               document.querySelector('iframe[src*="descIframe" i]');
    if (fr) {
        out.iframe_url = fr.src;
        out.layout = out.layout || 'iframe';
    }
    return out;
}
"""

FEEDBACK_JS = r"""
() => {
    const cards = [...document.querySelectorAll('.review-item')]
        .filter(e => e.querySelector('.anonymous-name') || e.querySelector('.content-text'));
    const q = (c, s) => { const el = c.querySelector(s); return el ? (el.innerText || el.textContent || '').trim() : ''; };
    return cards.map(c => {
        const scores = [...c.querySelectorAll('.review-latitude-score-unit')]
            .map(e => { const m = (e.innerText || '').match(/(\d)/); return m ? parseInt(m[1]) : null; })
            .filter(n => n !== null);
        const overall = scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null;
        const imgs = [...c.querySelectorAll('img')].map(i => i.src || i.getAttribute('data-src') || '')
            .filter(s => s.startsWith('http') && !/avatar|flag|\.svg|tps-\d{1,2}-/i.test(s));
        const vids = [...c.querySelectorAll('video[src],video source')]
            .map(v => v.src || v.getAttribute('src') || '').filter(s => s.startsWith('http'));
        return {
            reviewer:          q(c, '.anonymous-name') || 'N/A',
            country:           q(c, '.country-name') || 'N/A',
            date:              q(c, '.review-item-action') || 'N/A',
            stars:             overall === null ? 'N/A' : String(overall),
            text:              q(c, '.content-text') || 'N/A',
            model_number:      'N/A',
            product_reviewed:  q(c, '.product-name-link') || '',
            latitude_scores:   scores,
            verified_purchase: /verified/i.test(c.innerText || ''),
            repeat_buyer:      /repeat\s+buyer/i.test(c.innerText || ''),
            review_media:      { images: imgs, videos: vids },
        };
    });
}
"""

EXTRACT_JS = """
() => {
    const seen = new Set();
    const out  = [];

    const primarySels = [
        '.m-gallery-product-item-v2',
        '.search-card-e-slider',
        '[class*="search-card-e-offer"]',
        '[class*="J-offer-wrapper"]',
        '[data-content="product-item"]',
        'div[class*="offer-list-item"]',
        '[class*="organic-list-offer"]',
        '[class*="sese-offer"]',
        '[class*="AlbCard"]',
    ];

    let cards = [], matchedSel = "none";
    for (const sel of primarySels) {
        const found = document.querySelectorAll(sel);
        if (found.length > 0) { cards = Array.from(found); matchedSel = sel; break; }
    }

    if (cards.length < 3) {
        matchedSel = "anchor-fallback";
        const productLinkSel = [
            'a[href*="/product-detail/"]',
            'a[href*="product_"]',
            'a[href*=".html"][href*="alibaba.com"]',
            'a[href*="/offer/"]'
        ].join(',');
        document.querySelectorAll(productLinkSel).forEach(a => {
            const href = a.href || '';
            if (!href || href.includes('/search') || href.includes('/catalog/') || href.endsWith('.com/')) return;
            let el = a.parentElement;
            for (let d = 0; d < 6; d++) {
                if (el && (el.className.includes('item') || el.className.includes('card') || el.className.includes('offer'))) {
                    if (!cards.includes(el)) cards.push(el);
                    break;
                }
                el = el ? el.parentElement : null;
            }
        });
    }

    const resolveImg = img => {
        const s = img.src || img.getAttribute('data-src') || img.getAttribute('lazy-src') || img.dataset?.lazySrc || '';
        if (!s || s.startsWith('data:') || s.length < 12) return '';
        return s.startsWith('//') ? 'https:' + s : s;
    };

    const cleanTitle = el => {
        if (!el) return '';
        return (el.innerText || el.textContent || '').split('\\n')
            .map(s => s.trim()).filter(s => s.length > 3)[0] || '';
    };

    const tx = el => el ? el.textContent.trim() : '';
    const q  = (card, s) => card.querySelector(s);
    // Text with a space between text nodes. textContent glues siblings together
    // ("₹196.39-230.55" + "₹218.22-256.16" + "10% off" became
    // "₹196.39-230.55₹218.22-256.1610% off"), so the current price, the
    // struck-through price and the discount could not be told apart.
    const spacedText = el => {
        if (!el) return '';
        const parts = [];
        const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
        let n;
        while ((n = tw.nextNode())) {
            const t = (n.textContent || '').trim();
            if (t) parts.push(t);
        }
        return parts.join(' ');
    };

    for (const card of cards) {
        const isSponsored = !!card.querySelector('[class*="sponsored"],[class*="premium-tag"],[class*="ad-label"],[data-tracking*="sponsored"],[data-spm*="sponsored"]');

        const linkEl = q(card, 'a[href*="product-detail"]') || q(card, 'a[href*="alibaba.com"]') || q(card, 'a[href]');
        const href   = (linkEl ? linkEl.href : '').trim();
        if (!href || href === '#' || href.startsWith('javascript:')) continue;
        if (seen.has(href)) continue;
        seen.add(href);

        const titleEl = q(card, '.search-card-e-title')
            || q(card, '[class*="offer-title"],[class*="product-title"],[class*="subject"],h2,h3');
        const title = cleanTitle(titleEl) || cleanTitle(linkEl);
        if (title.length < 3) continue;

        const priceEl = q(card, '.search-card-e-price-main')
            || q(card, '[class*="price-value"],[class*="price-num"],[class*="price-range"],[class*="priceValue"],[class*="price-current"],[class*="price"]');
        const moqEl   = q(card, '.search-card-e-min-order')
            || q(card, '[class*="moq"],[class*="min-order"],[class*="minimum"],[class*="minOrder"]');
        const dateEl  = q(card, '[class*="post-time"],[class*="update-time"],[class*="post-date"],[class*="publish-date"],[class*="date-range"]');

        const images = Array.from(card.querySelectorAll('img')).map(resolveImg).filter(Boolean);
        const videos = Array.from(card.querySelectorAll('video[src],video source,[class*="video"] source'))
            .map(v => v.src || v.getAttribute('src') || '').filter(s => s.length > 5);

        const supplierEl  = q(card, '.search-card-e-company')
            || q(card, '[class*="supplier-name"],[class*="company-name"],[class*="vendor"],[class*="seller"],[class*="store-name"]');
        const countryEl   = q(card, '[class*="country-name"],[class*="country"],[class*="origin"],[class*="flag"]');
        const yearsEl     = q(card, '[class*="year"],[class*="member-year"],[class*="years-member"]');
        const goldEl      = q(card, '[class*="gold-supplier"],[class*="goldSupplier"],[class*="gold_supplier"]');
        const verifiedEl  = q(card, '[class*="verified-supplier"],[class*="verifiedSupplier"],[class*="verified"]');

        const responseEl  = q(card, '[class*="response-rate"],[class*="responseRate"]');
        const taEl        = q(card, '[class*="trade-assurance"],[class*="tradeAssurance"],[class*="ta-amount"],[class*="ta-cap"]');
        const trustEl     = q(card, '[class*="trust-score"],[class*="trustScore"],[class*="company-score"],[class*="store-score"]');

        const ratingEl    = q(card, '.search-card-e-review')
            || q(card, '[class*="rate-score"],[class*="rating-score"],[class*="star"],[aria-label*="star"]');
        const reviewCtEl  = q(card, '[class*="review-count"],[class*="review-num"],[class*="buyer-review"],[class*="buyer-feedback"]')
            || q(card, '[class*="transaction"],[class*="feedback"],[class*="sold"],[class*="order-count"],[class*="orders"]');

        const ratingTx = ratingEl
            ? (ratingEl.getAttribute('aria-label') || ratingEl.textContent).trim()
            : 'N/A';

        const rawPrice  = spacedText(priceEl) || 'N/A';
        const cleanPrice = rawPrice !== 'N/A'
            ? (rawPrice.replace(/\\s*Min\\.?\\s*order[\\s\\S]*/i, '').trim() || rawPrice)
            : 'N/A';

        const rawMoq  = tx(moqEl) || 'N/A';
        const cleanMoq = rawMoq !== 'N/A'
            ? (rawMoq.replace(/^[₹$€£¥][\\d,\\.\\-\\s]+/, '').trim() || rawMoq)
            : 'N/A';

        out.push({
            title,
            href,
            is_sponsored:  isSponsored,
            price:         cleanPrice,
            moq:           cleanMoq,
            date_posted:   tx(dateEl)    || 'N/A',
            images,
            videos,
            supplier:      tx(supplierEl) || 'N/A',
            country:       tx(countryEl)  || 'N/A',
            years_active:  tx(yearsEl)   || 'N/A',
            gold:          !!goldEl,
            verified:      !!verifiedEl,
            response_rate: tx(responseEl) || 'N/A',
            trade_assurance: tx(taEl)    || 'N/A',
            trust_score:   tx(trustEl)   || 'N/A',
            rating:        ratingTx,
            review_count:  tx(reviewCtEl) || 'N/A',
        });
    }
    return { selector: matchedSel, count: cards.length, items: out };
}
"""

HAS_REVIEW_ITEM_JS = (
    "() => [...document.querySelectorAll('.review-item')]"
    ".some(e => /[A-Za-z]\\*{2,}/.test(e.innerText || ''))"
)