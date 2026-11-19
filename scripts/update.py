#!/usr/bin/env python3
"""Gather energy-transition coverage from many feeds into data/articles.json, and
the world electricity mix (Ember, via Our World in Data) into data/mix.json.

Each source is an RSS/Atom feed, optionally filtered (by author, category or
keyword). New stories get the publisher page's photo, description and opening
paragraphs when the site allows it. Standard library only.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "articles.json"
MIX = ROOT / "data" / "mix.json"
MAX_ITEMS = 500
MAX_AGE_DAYS = 45
PER_SOURCE_PER_RUN = 8
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")

ENERGY_WORDS = re.compile(
    r"energy|electric|power|grid|solar|wind|batter|storage|nuclear|hydro|geotherm|renewable|clean|transmission|utilit|"
    r"\bEVs?\b|charging|vehicle|hydrogen|heat pump|coal|\bgas\b|\boil\b|LNG|emission|carbon|decarboni|net.zero|"
    r"lithium|copper|nickel|critical mineral|steel|cement|data cent|transition|climate|tariff|IRA\b|subsid", re.I)

TAGS = {
    "power": re.compile(r"\bgrid\b|utilit|transmission|interconnect|solar|wind (power|farm|turbine|energy|project)|offshore wind|"
                        r"batter(y|ies) storage|energy storage|\bBESS\b|nuclear|reactor|\bSMRs?\b|geotherm|hydropower|"
                        r"power (plant|prices|demand|market|purchase)|electricity (prices|demand|market|bills?)|\bPPAs?\b|"
                        r"data cent(er|re)s?|capacity market|load growth", re.I),
    "transport": re.compile(r"\bEVs?\b|electric (vehicle|car|truck|bus|bike|scooter)|charging|charger|Tesla|\bBYD\b|Rivian|"
                            r"plug-in|hybrid|e-bike|aviation|\bSAF\b|shipping|e-fuel|rail\b|transit|autos?\b|automaker", re.I),
    "industry": re.compile(r"\bsteel\b|cement|concrete|hydrogen|ammonia|industrial (heat|decarboni)|heat pump|"
                           r"critical mineral|lithium|copper|nickel|cobalt|graphite|rare earth|mining|manufactur|"
                           r"factory|supply chain|carbon capture|\bCCS\b|direct air capture", re.I),
    "china": re.compile(r"\bChina\b|\bChinese\b|Beijing|\bBYD\b|\bCATL\b|LONGi|Jinko", re.I),
    "policy": re.compile(r"polic|\bIRA\b|Inflation Reduction Act|tax credit|tariff|subsid|regulat|\bFERC\b|\bEPA\b|Congress|"
                         r"Senate|legislat|permitting|White House|Trump|\bEU\b|European Commission|\bCOP ?\d+|"
                         r"carbon (price|tax|market)|\bETS\b|mandate|auction|government", re.I),
}

ABBREV = re.compile(r"\b(?:U\.S|U\.K|U\.N|E\.U|M|Mme|Mgr|Me|Art|art|para|Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|Inc|Co|Corp|No|vs|etc|e\.g|i\.e|approx|Gov|Sen|Rep|Gen|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.$")


def sentences(text):
    parts, start = [], 0
    for m in re.finditer(r"[.!?][\u201d\u2019\"')]?(?=\s+[A-Z0-9\u00c0-\u00dd\u201c\u2018\u00ab\"'])", text):
        cand = text[start:m.end()].strip()
        if ABBREV.search(cand):
            continue  # "U.S." and friends don't end a sentence
        parts.append(cand)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


DATELINE = re.compile(r"^(?:[A-Z][A-Z.\u2019' -]{2,30}(?:, [A-Z][\w.\u2019' -]{1,25})?\s*(?:\([A-Za-z ]+\))?\s*[\u2014\u2013-]+\s*)")
BIO = re.compile(r"^\[?\s*[A-Z][\w.\u2019' -]{2,60}\b(is|was|serves as)\b.{0,60}\b(minister|professor|director|author|fellow|"
                 r"researcher|editor|executive|chair|founder|ceo|president|lecturer|analyst|correspondent|columnist)", re.I)


def fix_spacing(t):
    t = re.sub(r"\s+(?=[\u2019']s\b)", "", t)
    return re.sub(r"\s+(?=[.,;:!?)\u201d](?:\s|$))", "", t)


def lede(a):
    """The story's opening in at most two short sentences: the publisher's standfirst, else the first paragraph."""
    desc = a.get("description") or ""
    paras = [p for p in (a.get("paragraphs") or [])[:3] if not BIO.search(p) and not re.match(r"^\[.*\]$", p.strip())]
    truncated = desc.endswith(("\u2026", "...")) or (desc and desc[-1] not in ".!?\u201d\"')")
    pool = ([desc] if desc and not truncated else []) + paras[:1] + ([desc] if desc and truncated else [])
    for text in pool:
        text = fix_spacing(DATELINE.sub("", re.sub(r"\s+", " ", text).strip()))
        if BIO.search(text):
            continue
        ss = [x for x in sentences(text) if len(x) > 25]
        if not ss:
            continue
        out = ss[0]
        if len(out) > 230:  # one long sentence: trim at a word boundary
            out = out[:220].rsplit(" ", 1)[0].rstrip(",;:\u2014-") + "\u2026"
        elif len(ss) > 1 and len(out) + len(ss[1]) <= 240:
            out = f"{out} {ss[1]}"
        return out
    return None


# Weekly briefing: themes ranked by how many different outlets covered them
THEMES = [
    (r"solar", "solar"),
    (r"offshore wind|onshore wind|wind (power|farm|turbine|energy|project)", "wind"),
    (r"batter(y|ies) storage|energy storage|\bBESS\b|long.duration storage", "battery storage"),
    (r"nuclear|reactor|\bSMRs?\b|fusion", "nuclear"),
    (r"\bgrid\b|transmission|interconnect", "the grid"),
    (r"data cent(er|re)|\bAI\b", "AI\u2019s power demand"),
    (r"\bEVs?\b|electric (vehicle|car|truck|bus)", "EVs"),
    (r"charging|charger", "charging"),
    (r"hydrogen|ammonia", "hydrogen"),
    (r"heat pump", "heat pumps"),
    (r"\bsteel\b|cement|industrial heat", "heavy industry"),
    (r"critical mineral|lithium|copper|nickel|cobalt|graphite|rare earth", "critical minerals"),
    (r"\bChina\b|\bChinese\b|Beijing", "China"),
    (r"\bIndia\b", "India"),
    (r"\bEU\b|Europe", "Europe"),
    (r"\bIRA\b|Inflation Reduction Act|tax credit", "US tax credits"),
    (r"tariff", "tariffs"),
    (r"Trump|White House|Congress", "Washington"),
    (r"permitting", "permitting"),
    (r"coal", "coal"),
    (r"\bLNG\b|natural gas|gas (prices|plant|turbine)", "gas"),
    (r"\boil\b|OPEC|crude", "oil"),
    (r"electricity (prices|bills?)|power prices|utility bills?|affordab", "power prices"),
    (r"\bCOP ?\d+", None),
    (r"carbon capture|\bCCS\b|direct air capture", "carbon capture"),
    (r"geotherm", "geothermal"),
    (r"invest|financ|funding|\bIPO\b|venture", "clean-energy finance"),
]
THEMES = [(re.compile(p, re.I), label) for p, label in THEMES]


def first_sentence(text):
    ss = sentences(text or "")
    return ss[0] if ss else None


def briefing(articles, now):
    week = [a for a in articles if a.get("lede") and (parse_date(a["published"]) or now) >= now - timedelta(days=7)]
    if len(week) < 4:
        return None
    hits = {}  # label -> list of articles
    for a in week:
        hay = f"{a['title']} {a['lede']}"
        for rx, label in THEMES:
            m = rx.search(hay)
            if m:
                hits.setdefault(label or re.sub(r"\s+", "", m.group(0)).upper(), []).append(a)
    ranked = sorted(hits.items(), key=lambda kv: (len({a["source"] for a in kv[1]}), len(kv[1])), reverse=True)
    ranked = [kv for kv in ranked if len({a["source"] for a in kv[1]}) >= 2] or ranked
    bullets, used, used_sources = [], set(), {}
    for label, items in ranked:
        if len(bullets) >= 6:
            break
        def score(a):
            fs = first_sentence(a["lede"]) or ""
            return (a["url"] not in used, a.get("note") is None, used_sources.get(a["source"], 0) == 0,
                    60 <= len(fs) <= 220, a["published"])
        best = max(items, key=score)
        if best["url"] in used:
            continue
        text = first_sentence(best["lede"])
        if not text or len(text) < 60:
            continue
        if len(text) > 230:
            text = text[:220].rsplit(" ", 1)[0].rstrip(",;:\u2014-") + "\u2026"
        used.add(best["url"])
        used_sources[best["source"]] = used_sources.get(best["source"], 0) + 1
        bullets.append({"text": text, "source": best["source"], "url": best["url"], "theme": label})
    if not bullets:
        return None
    labels = [b["theme"] for b in bullets[:3]]
    head = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    head = head[0].upper() + head[1:]
    first = now - timedelta(days=6)
    end = now.strftime("%-d") if first.month == now.month else now.strftime("%b %-d")
    return {"headline": head, "period": f"{first.strftime('%b %-d')}\u2013{end}",
            "made": now.isoformat(timespec="seconds"), "bullets": bullets}


def tag(a):
    hay = " ".join([a.get("title", ""), a.get("description", ""), " ".join(a.get("cats", [])),
                    (a.get("paragraphs") or [""])[0]])
    return sorted({t for t, rx in TAGS.items() if rx.search(hay)} | set(a.get("tags", [])))


def gnews(query, lang="en"):
    loc = "hl=fr&gl=FR&ceid=FR:fr" if lang == "fr" else "hl=en-US&gl=US&ceid=US:en"
    return "https://news.google.com/rss/search?q=" + quote_plus(query) + "&" + loc


# desk: science | politics | reporting | opinion
# desk: news | analysis ; topic tabs come from TAGS
SOURCES = [
    # Reporting
    {"name": "Canary Media", "desk": "news", "feed": "https://www.canarymedia.com/rss.rss"},
    {"name": "Latitude Media", "desk": "news", "feed": "https://www.latitudemedia.com/feed"},
    {"name": "Utility Dive", "desk": "news", "feed": "https://www.utilitydive.com/feeds/news/"},
    {"name": "Energy Monitor", "desk": "news", "feed": "https://www.energymonitor.ai/feed/"},
    {"name": "pv magazine", "desk": "news", "feed": "https://www.pv-magazine.com/feed/"},
    {"name": "Energy-Storage.news", "desk": "news", "feed": "https://www.energy-storage.news/feed/"},
    {"name": "Electrek", "desk": "news", "feed": "https://electrek.co/feed/",
     "exclude_title": re.compile(r"\bdeal|\bdeals\b|\bsale\b|discount|\$\d+ off|lowest price|coupon|green deals", re.I)},
    {"name": "Heatmap", "desk": "news", "via_google": True, "feed": gnews("site:heatmap.news when:14d")},
    {"name": "auto", "desk": "news", "via_google": True,
     "feed": gnews('("energy transition" OR renewables OR solar OR "wind power" OR "battery storage" OR "electric vehicles") '
                   '(site:reuters.com OR site:ft.com OR site:bloomberg.com) when:7d')},
    {"name": "Carbon Brief", "desk": "news", "feed": "https://www.carbonbrief.org/feed/", "keywords": ENERGY_WORDS,
     "keywords_also": re.compile(r"energy|electric|power|solar|wind|coal|gas|oil|batter|EV|nuclear|grid|hydrogen", re.I)},
    # Data & institutions
    {"name": "EIA Today in Energy", "desk": "analysis", "feed": "https://www.eia.gov/rss/todayinenergy.xml"},
    {"name": "IEA", "desk": "analysis", "via_google": True, "feed": gnews("site:iea.org when:30d")},
    {"name": "Ember", "desk": "analysis", "via_google": True, "feed": gnews("site:ember-energy.org when:45d")},
    {"name": "IRENA", "desk": "analysis", "via_google": True, "feed": gnews("site:irena.org when:45d")},
    # Analysis & newsletters
    {"name": "Volts", "byline": "David Roberts", "desk": "analysis", "feed": "https://www.volts.wtf/feed"},
    {"name": "Sustainability by Numbers", "byline": "Hannah Ritchie", "desk": "analysis",
     "feed": "https://www.sustainabilitybynumbers.com/feed"},
    {"name": "Distilled", "desk": "analysis", "feed": "https://www.distilled.earth/feed"},
]

NS = {"dc": "http://purl.org/dc/elements/1.1/", "atom": "http://www.w3.org/2005/Atom",
      "media": "http://search.yahoo.com/mrss/", "content": "http://purl.org/rss/1.0/modules/content/"}


READER_UA = "CurrentReader/1.0 (+https://github.com/slimbeta/current; hourly RSS reader)"


def get(url, limit=1_500_000, timeout=20, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/xml,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def tidy_desc(t):
    t = re.sub(r"\s*The post .{3,300}? appeared first on .{2,80}?\.?\s*$", "", t)
    t = re.sub(r"\s*(Continue reading|Read more|Keep reading)\.*\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*\[(\u2026|\.\.\.|&hellip;)\]\s*$", "\u2026", t)
    return t.strip()


def text_of(node):
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", node or ""))).strip()
    return re.sub(r"\s+(?=[.,;:!?)\u201d](?:\s|$))", "", re.sub(r"\s+(?=[\u2019']s\b)", "", t))


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def fetch_feed(url):
    for attempt in range(3):
        try:
            # Some hosts (Substack) refuse browser-like requests from cloud servers but accept a named feed reader
            return get(url, limit=16_000_000, ua=READER_UA if attempt == 1 else UA)
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429) or attempt == 2:
                raise
            time.sleep(10 * (attempt + 1) if e.code == 429 else 1)


def parse_xml(raw):
    raw = raw.lstrip("\ufeff \t\r\n")
    try:
        return ET.fromstring(raw.encode("utf-8"))
    except ET.ParseError:
        # Repair common feed slips: attributes run together, bare ampersands, control characters.
        raw = re.sub(r'"(?=[A-Za-z_:][\w:.-]*=")', '" ', raw)
        raw = re.sub(r"&(?!#?\w+;)", "&amp;", raw)
        raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
        return ET.fromstring(raw.encode("utf-8"))


def feed_items(src):
    if src.get("via_google"):
        time.sleep(3)
    try:
        try:
            root = parse_xml(fetch_feed(src["feed"]))
        except ET.ParseError:
            # A bot-check page instead of XML: ask once more as a plainly named feed reader
            root = parse_xml(get(src["feed"], limit=16_000_000, ua=READER_UA))
    except Exception as e:
        if not src.get("fallback_feed"):
            raise
        print(f"  {src['name']}: {e}; using Google News fallback", file=sys.stderr)
        time.sleep(3)
        src["via_google"] = True
        root = parse_xml(fetch_feed(src["fallback_feed"]))
    items = root.findall(".//item") or root.findall(".//atom:entry", NS)
    for it in items:
        title = text_of(it.findtext("title") or it.findtext("atom:title", namespaces=NS))
        link = (it.findtext("link") or "").strip()
        if not link:
            el = it.find("atom:link", NS)
            link = el.get("href") if el is not None else ""
        desc = text_of(it.findtext("description") or it.findtext("atom:summary", namespaces=NS) or "")
        author = it.findtext("dc:creator", namespaces=NS) or it.findtext("author") or ""
        cats = [text_of(c.text) for c in it.findall("category")]
        when = parse_date(it.findtext("pubDate") or it.findtext("dc:date", namespaces=NS)
                          or it.findtext("atom:published", namespaces=NS) or it.findtext("atom:updated", namespaces=NS))
        image = None
        for tag in ("media:content", "media:thumbnail"):
            m = it.find(tag, NS)
            if m is not None and (m.get("url") or "").startswith("http") and m.get("medium", "image") == "image":
                image = m.get("url")
                break
        enc = it.find("enclosure")
        if not image and enc is not None and (enc.get("type") or "").startswith("image"):
            image = enc.get("url")
        yield {"title": title, "url": link, "desc": desc, "author": author.strip(), "cats": cats, "when": when, "image": image}


HOUSEKEEPING = re.compile(r"help us|donat|fundrais|open thread|sponsored|subscribe to|weekly roundup of comments|"
                          r"\bjobs?\b board|webinar|podcast:", re.I)


def keep(src, it):
    if not it["title"] or not it["url"] or HOUSEKEEPING.search(it["title"]):
        return False
    if "author" in src and not src["author"].search(it["author"]):
        return False
    if "categories" in src and not any(src["categories"].search(c) for c in it["cats"]):
        return False
    if "exclude_categories" in src and any(src["exclude_categories"].search(c) for c in it["cats"]):
        return False
    hay = f"{it['title']} {' '.join(it['cats'])}" if src.get("keyword_fields") == "title+categories" \
        else f"{it['title']} {it['desc']} {' '.join(it['cats'])}"
    if "exclude_title" in src and src["exclude_title"].search(it["title"]):
        return False
    if "keywords_also" in src and not src["keywords_also"].search(hay):
        return False
    if "keywords" in src and not src["keywords"].search(hay):
        return False
    return True


def meta(page, prop):
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        if not re.search(rf'(?:property|name)\s*=\s*["\']{re.escape(prop)}["\']', tag, re.I):
            continue
        m = re.search(r'content\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', tag, re.I)
        if m:
            return html.unescape(m.group(1) if m.group(1) is not None else m.group(2)).strip()
    return None


BOILERPLATE = re.compile(
    r"subscribe|sign up|sign in|log in|newsletter|cookie|advertisement|all rights reserved|"
    r"support our|donate|your inbox|this article|share this|read more|click here|free trial|"
    r"\bpodcast\b|listen to|photo(graph)? by|illustration by|getty images|\(c\)|©|"
    r"originally appeared|originally published|translated by|own translation|"
    r"\b\d+ (pp|pages)\b|\bpp\.|\$\d|related:|you may also like|reprint", re.I)


def opening_paragraphs(page, limit=3000, most=10):
    body = page
    m = re.search(r"<article\b.*?</article>", page, re.S | re.I)
    if m and len(m.group(0)) > 2000:
        body = m.group(0)
    body = re.sub(r"<(script|style|aside|nav|header|footer|figure)\b.*?</\1>", " ", body, flags=re.S | re.I)
    out, total = [], 0
    for raw in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I):
        t = text_of(raw)
        if len(t) < 90 or BOILERPLATE.search(t) or t.count(" ") < 12 or (out and t == out[-1]):
            continue
        out.append(t)
        total += len(t)
        if total >= limit or len(out) >= most:
            break
    return out


def clean_title(title):
    title = re.sub(r"^(Opinion|Review|Essay|Analysis)\s*[|:]\s*", "", title)
    return re.sub(r"\s+[|–—-]\s+[^|–—-]{2,40}$", "", title).strip()


def enrich(item):
    """Photo, description and opening paragraphs from the publisher page (best effort)."""
    item["ftries"] = item.get("ftries", 0) + 1
    if item.get("via_google"):
        item["fetched"] = True  # Google News links are redirects; nothing to read
        return item
    try:
        page = get(item["url"])
    except Exception as e:
        print(f"  no preview ({item['source']}): {e}", file=sys.stderr)
        return item
    item["fetched"] = True
    if item.get("undated"):
        found = meta(page, "article:published_time") or meta(page, "date") or meta(page, "dc.date")
        m = None if found else re.search(r'"datePublished"\s*:\s*"([^"]+)"', page)
        d = parse_date(found or (m.group(1) if m else None))
        if not d:
            m = re.search(r"Published:?\s+([A-Z][a-z]+ \d{1,2}, \d{4})", page)
            if m:
                try:
                    d = datetime.strptime(m.group(1), "%B %d, %Y").replace(tzinfo=timezone.utc)
                except ValueError:
                    d = None
        if d:
            item["published"] = d.isoformat(timespec="seconds")
            item.pop("undated", None)
    img = meta(page, "og:image") or meta(page, "twitter:image")
    if img and img.startswith("http") and not item.get("image"):
        item["image"] = img
    desc = meta(page, "og:description") or meta(page, "description")
    if desc and len(desc) > 40:  # the publisher's own standfirst beats a feed excerpt
        item["description"] = tidy_desc(re.sub(r"\s+", " ", desc).strip())
    paras = opening_paragraphs(page)
    d = item.get("description") or ""
    if paras and d and paras[0][:60] in d and len(paras[0]) <= len(d) + 20:
        paras = paras[1:]
    if paras:
        item["paragraphs"] = paras
    return item


def update_mix():
    url = ("https://ourworldindata.org/grapher/share-elec-by-source.csv?country=~OWID_WRL&v=1"
           "&csvType=filtered&useColumnShortNames=true")
    try:
        rows = get(url).strip().splitlines()
    except Exception as e:
        print(f"mix: {e}", file=sys.stderr)
        return
    head = rows[0].split(",")
    col = {name: head.index(name) for name in head}
    def v(cells, key):
        k = f"{key}_share_of_electricity__pct"
        try:
            return round(float(cells[col[k]]), 2)
        except (KeyError, ValueError, IndexError):
            return 0.0
    years = []
    for line in rows[1:]:
        c = line.split(",")
        if c[0] != "World" or int(c[2]) < 1985:
            continue
        other = v(c, "other_renewables_excluding_bioenergy") + v(c, "bioenergy")
        years.append({"year": int(c[2]), "coal": v(c, "coal"), "gas": v(c, "gas"), "oil": v(c, "oil"),
                      "nuclear": v(c, "nuclear"), "hydro": v(c, "hydro"), "other": round(other, 2),
                      "wind": v(c, "wind"), "solar": v(c, "solar")})
    if years:
        MIX.write_text(json.dumps({"source": "Ember and Energy Institute, via Our World in Data", "years": years}))
        last = years[-1]
        print(f"mix: {years[0]['year']}\u2013{last['year']}, solar+wind {last['solar'] + last['wind']:.1f}%")


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    existing = json.loads(OUT.read_text()) if OUT.exists() else {"articles": []}
    dropped = set(existing.get("dropped", []))  # too old when first found; don't refetch every hour
    known = {a["url"] for a in existing["articles"]} | dropped
    known_titles = {a["title"].lower() for a in existing["articles"]}
    fresh = []
    for src in SOURCES:
        try:
            items = [it for it in feed_items(src) if keep(src, it)]
        except Exception as e:
            print(f"feed failed: {src['name']} {src.get('byline', '')}: {e}", file=sys.stderr)
            continue
        added = 0
        for it in items:
            if added >= PER_SOURCE_PER_RUN:
                break
            when = it["when"] or now
            if when < cutoff or it["url"] in known or it["title"].lower() in known_titles:
                continue
            title, source = it["title"], src["name"]
            if src.get("via_google"):
                m = re.search(r"\s+-\s+([^-]+)$", title)  # "Headline - Bloomberg.com"
                if source == "auto" and not m:
                    source = "Google News"
                if m and source == "auto":
                    source = {"AP News": "Associated Press", "Financial Times": "Financial Times", "Bloomberg.com": "Bloomberg",
                              "Reuters": "Reuters"}.get(m.group(1).strip(), m.group(1).strip())
                title = re.sub(r"\s+-\s+[^-]+$", "", title)
                title = re.sub(r"\s+-\s*\|?\s*$", "", title).strip()
                title = re.sub(r"^NEWSLETTER:\s*", "", title)
            item = {"url": it["url"], "title": clean_title(title), "source": source, "desk": src["desk"],
                    "published": when.isoformat(timespec="seconds"), "seen": now.isoformat(timespec="seconds")}
            if not it["when"]:
                item["undated"] = True
            if it["cats"]:
                item["cats"] = it["cats"][:8]
            if src.get("tags"):
                item["tags"] = list(src["tags"])
            for k in ("byline", "note", "via_google", "lang"):
                if src.get(k):
                    item[k] = src[k]
            if it["desc"] and not src.get("via_google") and len(it["desc"]) > 30:
                item["description"] = tidy_desc(it["desc"])[:600]
            if it["image"]:
                item["image"] = it["image"]
            known.add(item["url"])
            known_titles.add(item["title"].lower())
            fresh.append(item)
            added += 1
        print(f"{src['name']:<26} {src.get('byline', ''):<20} {len(items):>3} matching, {added} new")

    retry = [a for a in existing["articles"][:120] if not a.get("fetched") and a.get("ftries", 0) < 3]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(enrich, fresh + retry))

    everything = fresh + existing["articles"]
    articles = [a for a in everything if (parse_date(a["published"]) or now) >= cutoff]
    dropped |= {a["url"] for a in everything if a not in articles}
    for a in articles:
        a["lede"] = lede(a)
        if not a["lede"]:
            a.pop("lede")
        a["tags"] = tag(a)
        if not a["tags"]:
            a.pop("tags")
    articles.sort(key=lambda a: a["published"], reverse=True)
    articles = articles[:MAX_ITEMS]
    # Rewrite the briefing once a day (first run after 10:00 UTC, early morning in the Americas)
    brief = existing.get("briefing")
    due = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if now < due:
        due -= timedelta(days=1)
    if not brief or (parse_date(brief.get("made")) or due) < due or os.environ.get("REBUILD_BRIEFING"):
        brief = briefing(articles, now) or brief
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated": now.isoformat(timespec="seconds"), "briefing": brief, "articles": articles,
                               "dropped": sorted(dropped)[-3000:]},
                              ensure_ascii=False, indent=1))
    print(f"wrote {len(articles)} articles ({len(fresh)} new)")
    update_mix()


if __name__ == "__main__":
    main()
