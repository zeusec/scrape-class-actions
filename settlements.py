#!/usr/bin/env python3
import re
import json
import html
import logging
import requests
from datetime import date, datetime
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

CAORG_URL = "https://www.classaction.org/settlements"
SPARROW_URL = "https://usesparrow.com/class-actions"
SPARROW_DETAIL_WORKERS = 4
HTTP_TIMEOUT = 30
DEADLINE_FORMATS = ("%m/%d/%y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.3"

CAORG_CARD = re.compile(
    r'<div id="([^"]+)" data-name="([^"]*)"[^>]*class="[^"]*settlement-card'
    r'(.*?)(?=<div id="[^"]+" data-name=|<footer)', re.S)
CAORG_LINK = re.compile(r'<a href="([^"]+)"[^>]*class="js-settlement-link')
CAORG_PAYOUT = re.compile(r'Settlement<br>\s*Payout</span>\s*<span[^>]*>(.*?)</span>', re.S)
CAORG_DEADLINE = re.compile(r'<br>Deadline</span>\s*<span[^>]*>(.*?)</span>', re.S)
CAORG_PROOF = re.compile(r'Proof\s*<br>Required\?</span>\s*<span[^>]*>(.*?)</span>', re.S)
CAORG_DESC = re.compile(r'<p class="f6 lh-copy[^"]*">(.*?)</p>', re.S)

SPARROW_CARD = re.compile(
    r'<a href="(/class-actions/[^"]*?-(\d{4}-\d{2}-\d{2}))"[^>]*>.*?'
    r'<p class="font-semibold[^"]*">(.*?)</p>', re.S)
SPARROW_FIELD = re.compile(
    r'<span class="text-white/60">([^<]+)</span>\s*(?:<span[^>]*>|<a[^>]*>)(.*?)</(?:span|a)>')
SPARROW_OFFICIAL = re.compile(r'file directly with the settlement administrator\.\s*<a href="([^"]+)"')
SPARROW_LDJSON = re.compile(r'type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S)

log = logging.getLogger("class-action-settlements")

def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()

def parse_deadline(value):
    text = (value or "").strip()
    for fmt in DEADLINE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

def normalise_deadline(value):
    parsed = parse_deadline(value)
    if not parsed:
        return (value or "").strip() or None
    return f"{parsed:%B} {parsed.day}, {parsed.year}"

def admin_key(url):
    if not url:
        return None
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or None

def fetch(url):
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.text

def scrape_caorg():
    out = {}
    for cid, name, body in CAORG_CARD.findall(fetch(CAORG_URL)):
        link = CAORG_LINK.search(body)
        official = link.group(1) if link else None
        key = admin_key(official) or cid
        if key in out:
            log.warning("classaction.org: %r and %r share administrator %s; keeping the latter",
                        out[key]["name"], name, key)
        payout = CAORG_PAYOUT.search(body)
        deadline = CAORG_DEADLINE.search(body)
        proof = CAORG_PROOF.search(body)
        desc = CAORG_DESC.search(body)
        out[key] = {
            "name": html.unescape(name).strip(),
            "official": official,
            "payout": strip_tags(payout.group(1)) if payout else None,
            "deadline": normalise_deadline(strip_tags(deadline.group(1)) if deadline else None),
            "proof": strip_tags(proof.group(1)) if proof else None,
            "summary": strip_tags(desc.group(1)) if desc else None,
            "sparrow": None,
        }
    return out

def _sparrow_article_description(body):
    for block in SPARROW_LDJSON.findall(body):
        try:
            graph = json.loads(block).get("@graph", [])
        except ValueError:
            continue
        for node in graph:
            if node.get("@type") == "Article" and node.get("description"):
                return node["description"]
    return None

def _sparrow_detail(slug, name):
    try:
        body = fetch("https://usesparrow.com" + slug)
    except requests.RequestException as e:
        log.warning("sparrow detail %s: %s", slug, e)
        return None
    fields = {strip_tags(k): strip_tags(v) for k, v in SPARROW_FIELD.findall(body)}
    official = SPARROW_OFFICIAL.search(body)
    return {
        "name": name,
        "official": official.group(1) if official else None,
        "payout": fields.get("Payout up to"),
        "deadline": normalise_deadline(fields.get("Deadline")),
        "proof": fields.get("Proof required"),
        "summary": _sparrow_article_description(body),
        "sparrow": "https://usesparrow.com" + slug,
    }

def scrape_sparrow():
    today = date.today().isoformat()
    cards = [(slug, strip_tags(name)) for slug, deadline, name
             in SPARROW_CARD.findall(fetch(SPARROW_URL)) if deadline >= today]
    with ThreadPoolExecutor(max_workers=SPARROW_DETAIL_WORKERS) as ex:
        results = list(ex.map(lambda c: _sparrow_detail(*c), cards))
    out = {}
    for (slug, _), rec in zip(cards, results):
        if rec:
            out[admin_key(rec["official"]) or slug] = rec
    return out

def _better(caorg, sparrow):
    merged = dict(caorg)
    merged["sparrow"] = sparrow["sparrow"]
    for field in ("payout", "deadline", "proof", "summary"):
        if (not merged.get(field) or merged[field] in ("Varies", "N/A")) and sparrow.get(field):
            merged[field] = sparrow[field]
    return merged

def collect():
    scraped = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {"classaction.org": ex.submit(scrape_caorg), "sparrow": ex.submit(scrape_sparrow)}
        for source, future in futures.items():
            try:
                scraped[source] = future.result()
            except Exception as e:
                log.warning("%s scrape failed: %s", source, e)
                scraped[source] = {}
            else:
                if not scraped[source]:
                    log.warning("%s returned 0 settlements — its markup has probably "
                                "changed and the scraper needs updating", source)
    merged = dict(scraped["classaction.org"])
    for key, rec in scraped["sparrow"].items():
        merged[key] = _better(merged[key], rec) if key in merged else rec
    log.info("settlements: %d classaction.org + %d sparrow -> %d merged",
             len(scraped["classaction.org"]), len(scraped["sparrow"]), len(merged))
    return merged, all(scraped.values())
