from __future__ import annotations

JS_SEARCH_RESULTS = r"""
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/p/itm"]').forEach(a => {
    const href = a.href;
    const m = href.match(/\/p\/(itm[0-9a-z]+)/i);
    if (!m) return;
    const pidm = href.match(/[?&]pid=([^&]+)/);
    const key = pidm ? pidm[1] : m[1];
    if (seen.has(key)) return;
    seen.add(key);
    // The card anchor's own first text line is UI chrome ("Bestseller" / "Add
    // to Compare" checkbox label), not the product title -- the title lives on
    // the product image's alt text. Fall back to the first innerText line that
    // isn't one of those known badge/action labels if the image has none.
    const imgAlt = (a.querySelector('img[alt]') || {}).getAttribute
      ? a.querySelector('img[alt]').getAttribute('alt') : null;
    const badgeRe = /^(bestseller|add to compare|sponsored|ad|assured)$/i;
    let title = (imgAlt || '').trim();
    if (!title) {
      const lines = (a.getAttribute('title') || a.innerText || '').split('\n').map(t => t.trim()).filter(Boolean);
      title = lines.find(t => !badgeRe.test(t)) || lines[0] || '';
    }
    const sponsored = /\bsponsored\b|\bad\b/i.test(
      (a.closest('div') ? a.closest('div').innerText : '').slice(0, 200));
    out.push({url: href, product_id: pidm ? pidm[1] : null, item_id: m[1], title, is_sponsored: sponsored});
  });
  return out;
}
"""

JS_JSONLD = """
() => [...document.querySelectorAll('script[type="application/ld+json"]')]
        .map(s => { try { return JSON.parse(s.textContent) } catch(e) { return null } })
        .filter(Boolean)
"""

JS_PRICES = r"""
() => {
  const out = {selling: null, mrp: null, discount: null};
  const top = e => e.getBoundingClientRect().top + window.scrollY;
  const struck = e => {
    for (let n = e, i = 0; n && i < 4; n = n.parentElement, i++) {
      const cs = getComputedStyle(n);
      if ((cs.textDecorationLine || cs.textDecoration || '').includes('line-through')) return true;
      if (['DEL', 'S', 'STRIKE'].includes(n.tagName)) return true;
    }
    return false;
  };
  const all = [...document.querySelectorAll('div,span,del,s,p')].filter(e => top(e) < 1800);
  // The buy-box's own price text is reliably rendered in the largest font on
  // the page; a variant-swatch chip or an unrelated "Similar Products"/
  // recommendation carousel card showing some OTHER product's (or some OTHER
  // configuration's) price is always set in a visibly smaller font. Picking
  // the max-font-size candidate - for both the live price and the
  // struck-through original price - keeps the scan from latching onto
  // whichever matching element merely happens to come first in document
  // order, which previously let a smaller-font, unrelated element contaminate
  // the result (seen live: an unrelated Samsung flagship's struck price, or a
  // different storage variant's own struck price, both elsewhere on the same
  // page and both in a smaller font than the real MRP).
  let bestSelling = 0, bestMrp = 0;
  for (const e of all.filter(e => e.children.length === 0)) {
    const t = e.textContent.trim();
    const isStruck = struck(e);
    let bare = null;
    if (/^₹\s?[\d,]{3,}$/.test(t)) {
      bare = t.replace(/^₹\s?/, '');
    } else if (isStruck && /^[\d,]{3,}$/.test(t)) {
      // The struck-through original-price text can be rendered as a bare
      // number with the ₹ glyph in its own separate sibling node (seen on
      // Flipkart's current buy-box markup), so it never matches the
      // ₹-prefixed pattern above. Only accepted when genuinely struck -
      // never for a bare number alone.
      bare = t;
    }
    if (bare === null) continue;
    const fs = parseFloat(getComputedStyle(e).fontSize);
    if (isStruck) {
      if (fs > bestMrp) { bestMrp = fs; out.mrp = "₹" + bare; }
    } else if (fs > bestSelling) { bestSelling = fs; out.selling = "₹" + bare; }
  }
  for (const e of all) {
    const t = (e.innerText || '').trim();
    if (t.length > 14) continue;
    const d = t.match(/^[^\d]{0,3}\s?(\d{1,2})\s?%\s*(off)?$/i);
    if (d) { out.discount = d[1] + '%'; break; }
  }
  return out;
}
"""

JS_IMAGES_VIDEOS = r"""
() => {
  const W = window.innerWidth;
  const top = e => e.getBoundingClientRect().top + window.scrollY;
  const stop = /^(from the manufacturer|product description|description|ratings & reviews|specifications|questions and answers|similar products|all details)$/i;
  let cutoff = 1500;
  [...document.querySelectorAll('div,span,h1,h2,h3')]
    .filter(e => e.children.length === 0 && stop.test(e.textContent.trim()))
    .forEach(e => { const y = top(e); if (y > 450 && y < cutoff) cutoff = y; });
  const inGallery = e => {
    const r = e.getBoundingClientRect();
    return r.width >= 40 && r.height >= 40 && (r.left + r.width / 2) < W * 0.5 && (r.top + window.scrollY) < cutoff;
  };
  const imgs = new Set();
  const add = u => {
    if (!u || !/rukminim/.test(u)) return;
    const m = u.match(/\/image\/(\d+)\/(\d+)\//);
    if (!m || parseInt(m[1]) < 100) return;
    imgs.add(u.replace(/\/image\/\d+\/\d+\//, '/image/832/832/').split('?')[0]);
  };
  document.querySelectorAll('img').forEach(i => {
    if (!inGallery(i)) return;
    add(i.currentSrc || i.src); add(i.getAttribute('data-src'));
  });
  document.querySelectorAll('div,span,a').forEach(e => {
    if (!inGallery(e)) return;
    const bg = getComputedStyle(e).backgroundImage;
    const m = bg && bg.match(/url\("?([^")]+)"?\)/);
    if (m) add(m[1]);
  });
  const vids = new Set();
  const addV = u => { if (u && /^https?:/.test(u)) vids.add(u); };
  document.querySelectorAll('video').forEach(v => {
    if (!inGallery(v)) return;
    addV(v.currentSrc || v.src);
    v.querySelectorAll('source').forEach(s => addV(s.src));
  });
  document.querySelectorAll('iframe').forEach(f => {
    if (inGallery(f) && /youtube|vimeo|video/.test(f.src)) addV(f.src);
  });
  document.querySelectorAll('a[href*="youtube.com"], a[href*="youtu.be"]').forEach(a => {
    if (inGallery(a)) addV(a.href);
  });
  return {images: [...imgs], videos: [...vids]};
}
"""

JS_GALLERY_THUMBS = r"""
() => {
  const W = window.innerWidth, H = window.innerHeight;
  const out = [];
  document.querySelectorAll('img').forEach(i => {
    const r = i.getBoundingClientRect();
    const u = i.currentSrc || i.src || '';
    if (/rukminim/.test(u) && r.width >= 30 && r.width <= 130 && r.height >= 30 && r.height <= 130
        && r.left < W * 0.18 && r.top > 80 && r.top < H - 20) {
      out.push({x: r.left + r.width / 2, y: r.top + r.height / 2});
    }
  });
  return out;
}
"""

JS_HIGHLIGHTS = """
() => {
  const heads = [...document.querySelectorAll('div,span,h2,h3')]
      .filter(e => e.children.length === 0 && /^Product highlights$/i.test(e.textContent.trim()));
  for (const h of heads) {
    let node = h;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const lis = node.querySelectorAll('li');
      if (lis.length) return [...lis].map(l => l.innerText.trim()).filter(Boolean);
    }
  }
  return [];
}
"""

JS_SPECS_TABLES = r"""
() => {
  const specs = {};

  // 1. Check for standard table elements
  document.querySelectorAll('table').forEach(t => {
    const rows = [...t.querySelectorAll('tr')]
        .map(tr => [...tr.querySelectorAll('td,th')].map(td => td.innerText.trim()))
        .filter(r => r.length >= 2 && r[0]);
    if (!rows.length) return;
    let title = 'General';
    const p = t.previousElementSibling || (t.parentElement && t.parentElement.previousElementSibling);
    if (p && p.innerText) {
      const line = p.innerText.trim().split('\n')[0].trim();
      if (line && line.length < 50) title = line;
    }
    for (const r of rows) {
      const k = r[0];
      const v = r.slice(1).join(' ').trim();
      if (k.toLowerCase() === 'description' && v.toLowerCase().includes('manufacturer info')) continue;
      if (k && v && k.length < 80) {
        if (!specs[title]) specs[title] = {};
        specs[title][k] = v;
      }
    }
  });

  const totalRows = () => Object.values(specs).reduce((sum, sec) => sum + Object.keys(sec).length, 0);
  if (totalRows() >= 2) return specs;

  // 2. Check for modern Flipkart div-based specification rows (guessed,
  // obfuscated class-name fragments from an older Flipkart template).
  const heads = [...document.querySelectorAll('div,span,h2,h3,p')]
      .filter(e => /^(Specifications|Product Details|Specifications & Features|Product Information)$/i.test(e.innerText.trim()));

  for (const h of heads) {
    let container = h.parentElement;
    for (let i = 0; i < 7 && container; i++, container = container.parentElement) {
      const rowCandidates = [...container.querySelectorAll('div.row, div[class*="_1s_Smc"], div[class*="_3k-BhJ"] tr, div[class*="_3k-BhJ"] > div, tr')];
      if (rowCandidates.length >= 2) {
        let currentSection = 'General';
        for (const row of rowCandidates) {
          const cells = [...row.children].filter(c => c.innerText && c.innerText.trim());
          if (cells.length === 1 && cells[0].innerText.trim().length < 40) {
            currentSection = cells[0].innerText.trim();
          } else if (cells.length >= 2) {
            const k = cells[0].innerText.trim();
            const v = cells.slice(1).map(c => c.innerText.trim()).join(' ');
            if (k.toLowerCase() === 'description' && v.toLowerCase().includes('manufacturer info')) continue;
            if (k && v && k.length < 80 && !k.toLowerCase().includes('specifications')) {
              if (!specs[currentSection]) specs[currentSection] = {};
              specs[currentSection][k] = v;
            }
          }
        }
        if (totalRows() >= 2) return specs;
      }
    }
  }

  if (totalRows() >= 2) return specs;

  // 3. Class-name-independent structural detection. Flipkart's obfuscated
  // CSS classes churn across deployments/A-B templates (confirmed live, Nov
  // 2026: paths 1 and 2 above both find nothing on a real, currently-served
  // product page that nonetheless has full specs on-screen), so this path
  // never matches on a class name. It instead recognizes a section purely
  // by (a) a known, extensible section-header vocabulary and (b) the
  // structural shape of a label/value row: a container whose immediate
  // children are each either a row directly, or a single-child wrapper
  // around one - where a "row" is an element with 2 (or 3, the 3rd being an
  // empty spacer) children, the first two of which are leaf elements (no
  // children of their own) carrying short label text and value text
  // respectively. Confirmed live on Flipkart's current template: a
  // top-level container holds one such title+content pair per section
  // ("General", "Display Features", "Camera Features", "Battery & Power
  // Features", "Connectivity Features", "Dimensions", "Memory & Storage
  // Features", "Multimedia Features", "OS & Processor Features", "Call
  // Features", ...), and each content div's children are single-row
  // wrappers. A legal/manufacturer-info block that Flipkart instead renders
  // in a separate modal dialog (opened by engine._expand_sections clicking
  // "Manufacturer Info") is NOT reachable this way - it has no adjacent
  // content sibling to walk into - so it is deliberately left to the
  // existing text-based extraction (JS_SPECS_TEXT), which the engine merges
  // in alongside this result.
  const KNOWN_HEADERS = new Set([
    'general', 'dimensions', 'warranty', 'connectivity', 'display', 'camera', 'battery',
    'processor', 'multimedia', 'sound', 'other', 'performance', 'design', 'platform',
    'storage', 'ports', 'software', 'sensors', 'audio', 'video', 'product details',
    'key features', 'additional features', 'manufacturer info', 'manufacturer details',
    'network & connectivity', 'memory & storage', 'os & processor',
  ]);
  const HEADER_SUFFIX_RE = new RegExp(
    '^(general|display|camera|processor|os\\s*&\\s*processor|memory\\s*&\\s*storage|' +
    'connectivity|call|multimedia|battery\\s*&\\s*power|audio|video|dimensions|warranty|' +
    'additional|network|operating\\s*system)\\s+(features|details|specifications)$', 'i');

  function isHeaderText(t) {
    const s = (t || '').trim().toLowerCase().replace(/\s+/g, ' ');
    if (!s || s.length > 60) return false;
    if (KNOWN_HEADERS.has(s)) return true;
    return HEADER_SUFFIX_RE.test(s);
  }

  function isLeaf(el) {
    return !!el && el.nodeType === 1 && el.children.length === 0;
  }

  function findRow(el) {
    if (!el || el.nodeType !== 1) return null;
    const kids = [...el.children];
    if (kids.length < 2 || kids.length > 3) return null;
    const c0 = kids[0], c1 = kids[1];
    if (!isLeaf(c0) || !isLeaf(c1)) return null;
    const label = (c0.innerText || '').trim();
    const value = (c1.innerText || '').trim();
    if (!label || !value) return null;
    if (label.length < 2 || label.length > 80) return null;
    if (value.length > 400) return null;
    if (label.toLowerCase() === value.toLowerCase()) return null;
    if (kids.length === 3) {
      const c2 = kids[2];
      if (!isLeaf(c2)) return null;
      if ((c2.innerText || '').trim()) return null;
    }
    return { label, value };
  }

  function rowFrom(node) {
    if (!node || node.nodeType !== 1) return null;
    let r = findRow(node);
    if (r) return r;
    if (node.children.length === 1) {
      r = findRow(node.children[0]);
      if (r) return r;
    }
    return null;
  }

  const headerLeaves = [...document.querySelectorAll('div,span,h2,h3,h4,li')]
      .filter(e => e.children.length === 0 && isHeaderText(e.innerText));

  for (const h of headerLeaves) {
    const title = h.innerText.trim().replace(/\s+/g, ' ');
    const parent = h.parentElement;
    if (!parent) continue;
    const siblings = [...parent.children];
    const hIdx = siblings.indexOf(h);
    if (hIdx === -1) continue;
    // The content container is the (first) sibling right after the header
    // within the same parent - true for every section confirmed live.
    let content = null;
    for (let i = hIdx + 1; i < siblings.length; i++) {
      if (siblings[i].children.length > 0) { content = siblings[i]; break; }
    }
    if (!content) continue;
    const rows = [];
    for (const child of content.children) {
      const r = rowFrom(child);
      if (r) rows.push(r);
    }
    if (!rows.length) continue;
    for (const { label, value } of rows) {
      if (!specs[title]) specs[title] = {};
      if (!(label in specs[title])) specs[title][label] = value;
    }
  }

  return specs;
}
"""

JS_SPECS_TEXT = """
() => {
  const heads = [...document.querySelectorAll('div,span,h2,h3,p')]
      .filter(e => /^(Specifications|Product Details)$/i.test(e.innerText.trim()));
  for (const h of heads) {
    let node = h;
    for (let i = 0; i < 7 && node; i++, node = node.parentElement) {
      let txt = node.innerText || '';
      if (txt.length > 200) {
        const specIdx = txt.search(/^(Specifications|Product Details)$/im);
        if (specIdx !== -1) {
          txt = txt.slice(specIdx);
        }
        const stopIdx = txt.search(/\\n\\s*(Similar Products|Trending|More about|Ratings & Reviews|Customer Ratings|Questions & Answers|Safe and Secure|ABOUT US|Flipkart Internet)/i);
        if (stopIdx !== -1) {
          txt = txt.slice(0, stopIdx);
        }
        if (txt.length > 100) return txt;
      }
    }
  }
  return '';
}
"""

JS_BREADCRUMBS = """
() => {
  const selectors = [
    'div._75nlfW a',
    'div._2WhKao a',
    'div.r2CdBx a',
    'a._3GIHBu',
    'a._2whkao',
    'nav a'
  ];
  for (const sel of selectors) {
    const els = [...document.querySelectorAll(sel)];
    if (els.length >= 2) {
      const texts = els.map(e => e.innerText.trim()).filter(t => t && t.toLowerCase() !== 'home' && !t.includes('>'));
      if (texts.length) return texts;
    }
  }
  return [];
}
"""

JS_VARIANTS = """
() => {
  const labels = ['Color','Colour','Storage','RAM','Size','Variant','Pack of','Capacity','Model'];
  const out = {};
  const leaves = [...document.querySelectorAll('div,span,h2,h3')].filter(e => e.children.length === 0);
  for (const label of labels) {
    const el = leaves.find(e => new RegExp('^' + label + '\\\\s*:?$', 'i').test(e.textContent.trim()));
    if (!el) continue;
    let node = el.parentElement;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const items = [...node.querySelectorAll('li, a[href*="/p/itm"], div[class*="swatch"], div[class*="Swatch"]')];
      if (items.length) {
        const seenOpts = new Set();
        const opts = [];
        items.forEach(item => {
          const a = item.tagName === 'A' ? item : item.querySelector('a');
          const optText = (item.innerText || item.getAttribute('title') || item.querySelector('img')?.alt || '').trim().replace(/\\n+/g, ' ');
          if (optText && !seenOpts.has(optText.toLowerCase()) && optText.length < 50) {
            seenOpts.add(optText.toLowerCase());
            opts.push({
              option: optText,
              url: a ? a.href : null,
              product_id: a ? ((a.href.match(/[?&]pid=([^&]+)/) || [])[1] || null) : null
            });
          }
        });
        if (opts.length > 0) {
          out[label] = opts;
          break;
        }
      }
    }
  }
  return out;
}
"""

JS_CLICK_MORE = """
() => {
  let n = 0;
  document.querySelectorAll('span,a,div,button').forEach(e => {
    const t = (e.innerText || '').trim();
    if (/^(more|read more|view all|view all specifications|all details|show more|view details)$/i.test(t)) {
      const target = e.closest('button, a') || e;
      const href = target.getAttribute && target.getAttribute('href');
      if (href && !href.startsWith('#') && !href.startsWith('javascript')) return;
      try { target.click(); n++; } catch(err) {}
    }
  });
  return n;
}
"""

JS_REVIEW_MEDIA = r"""
() => {
  const IMR = /blobio|\/imr\//i;
  const imgsFrom = (el) => {
    const s = new Set();
    el.querySelectorAll('img').forEach(i => {
      [i.currentSrc, i.src, i.getAttribute('data-src'), i.srcset].forEach(v => {
        if (!v) return;
        v.split(',').forEach(part => {
          const u = (part || '').trim().split(' ')[0];
          if (u && IMR.test(u)) s.add(u);
        });
      });
    });
    el.querySelectorAll('*').forEach(e => {
      const bg = getComputedStyle(e).backgroundImage || '';
      const m = bg.match(/url\("?([^")]+)"?\)/);
      if (m && IMR.test(m[1])) s.add(m[1]);
    });
    return [...s];
  };
  const vidsFrom = (el) => {
    const s = new Set();
    el.querySelectorAll('video').forEach(v => {
      [v.currentSrc, v.src, v.getAttribute('src'), v.getAttribute('data-src')].forEach(u => {
        if (u && /^https?:/.test(u)) s.add(u);
      });
      v.querySelectorAll('source').forEach(so => {
        const u = so.src || so.getAttribute('src');
        if (u && /^https?:/.test(u)) s.add(u);
      });
    });
    el.querySelectorAll('a[href]').forEach(a => {
      const u = a.href || '';
      if (/\.(mp4|m3u8|webm)(\?|$)/i.test(u) || /\/video\//i.test(u)) s.add(u);
    });
    return [...s];
  };
  // A review card is the smallest ancestor of a review photo/video whose text
  // carries the per-review scaffolding (the Helpful action plus a rating/variant
  // or verified-purchase marker). The top aggregate "customer photos" strip has
  // no Helpful action, so it is naturally excluded. textContent (not innerText)
  // is used so detection does not depend on CSS layout being applied.
  const isCard = (node) => {
    if (!node || node.nodeType !== 1) return false;
    const t = node.textContent || '';
    if (t.length < 20 || t.length > 2500) return false;
    return /Helpful/.test(t) && (/Review for/.test(t) || /Verified Purchase/.test(t) || /•/.test(t));
  };
  // Per-review photos are <img> (src + srcset); seed from every review photo /
  // video element and from each card's own text markers (the Helpful action /
  // verified-purchase line), then walk up to the card and harvest all media.
  const seeds = [];
  document.querySelectorAll('img').forEach(i => {
    const u = i.currentSrc || i.src || i.getAttribute('data-src') || i.srcset || '';
    if (IMR.test(u)) seeds.push(i);
  });
  document.querySelectorAll('video').forEach(v => seeds.push(v));
  [...document.querySelectorAll('*')].forEach(e => {
    const t = (e.textContent || '').trim();
    if ((t === 'Helpful' || t === 'Verified Purchase') && e.querySelectorAll('*').length <= 1) seeds.push(e);
  });
  // A photo <img> can sit ~16 wrappers below its card, so walk up generously.
  const cards = new Set();
  for (const seed of seeds) {
    let node = seed;
    for (let i = 0; i < 24 && node; i++, node = node.parentElement) {
      if (isCard(node)) { cards.add(node); break; }
    }
  }
  const out = [];
  for (const card of cards) {
    const images = imgsFrom(card);
    const videos = vidsFrom(card);
    if (!images.length && !videos.length) continue;
    out.push({
      text: (card.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500),
      images: images,
      videos: videos,
    });
  }
  return out;
}
"""

JS_ASPECT_CHIPS = """
() => {
  const leaf = [...document.querySelectorAll('div,span,button,a')]
      .find(e => e.children.length === 0 && /^Overall$/i.test(e.textContent.trim()));
  if (!leaf) return [];
  let node = leaf;
  for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
    const p = node.parentElement;
    if (p && p.children.length >= 3) {
      return [...p.children].map(c => c.innerText.trim()).filter(Boolean);
    }
  }
  return [];
}
"""
