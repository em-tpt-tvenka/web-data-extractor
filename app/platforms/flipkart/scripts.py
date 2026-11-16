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
    const title = (a.getAttribute('title') || a.innerText || '').trim().split('\n')[0];
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
  let best = 0;
  for (const e of all.filter(e => e.children.length === 0)) {
    const t = e.textContent.trim();
    if (/^₹\s?[\d,]{3,}$/.test(t)) {
      if (struck(e)) { if (!out.mrp) out.mrp = t; }
      else {
        const fs = parseFloat(getComputedStyle(e).fontSize);
        if (fs > best) { best = fs; out.selling = t; }
      }
    }
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

JS_SPECS_TABLES = """
() => {
  const specs = {};
  document.querySelectorAll('table').forEach(t => {
    const rows = [...t.querySelectorAll('tr')]
        .map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim()))
        .filter(r => r.length >= 2 && r[0]);
    if (!rows.length) return;
    let title = 'General';
    const p = t.previousElementSibling || (t.parentElement && t.parentElement.previousElementSibling);
    if (p && p.innerText) title = p.innerText.trim().split('\\n')[0] || title;
    specs[title] = Object.assign(specs[title] || {}, Object.fromEntries(rows.map(r => [r[0], r.slice(1).join(' ')])));
  });
  return specs;
}
"""

JS_SPECS_TEXT = """
() => {
  const heads = [...document.querySelectorAll('div,span,h2,h3')]
      .filter(e => e.children.length === 0 && /^Specifications$/i.test(e.textContent.trim()));
  for (const h of heads) {
    let node = h;
    for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
      if (node.innerText && node.innerText.length > 400) return node.innerText;
    }
  }
  return '';
}
"""

JS_VARIANTS = """
() => {
  const labels = ['Color','Colour','Storage','RAM','Size','Variant','Pack of','Capacity','Model'];
  const out = {};
  const leaves = [...document.querySelectorAll('div,span')].filter(e => e.children.length === 0);
  for (const label of labels) {
    const el = leaves.find(e => new RegExp('^' + label + '\\\\s*:?$', 'i').test(e.textContent.trim()));
    if (!el) continue;
    let node = el.parentElement;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      const links = [...node.querySelectorAll('a[href*="/p/itm"]')];
      if (links.length) {
        out[label] = links.map(a => ({
          option: (a.innerText || a.getAttribute('title') || a.querySelector('img')?.alt || '').trim().replace(/\\n+/g, ' '),
          url: a.href,
          product_id: (a.href.match(/[?&]pid=([^&]+)/) || [])[1] || null
        }));
        break;
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
    if (e.children.length === 0 && /^(more|read more)$/i.test(e.textContent.trim())) {
      const a = e.closest('a');
      const href = a && a.getAttribute('href');
      if (href && !href.startsWith('#') && !href.startsWith('javascript')) return;
      e.click(); n++;
    }
  });
  return n;
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
