#!/usr/bin/env python3
"""
MacBook Air M5 price monitor for UAE trusted stores — DISCOVERY mode.

For each store you provide a SEARCH URL. The script opens the search results,
discovers every product listing (link + title + price), keeps the ones whose
title matches your keyword filters, and emails you when any listing is at or
below the threshold. New listings are picked up automatically because they
appear in the store's search.

Designed to run unattended on GitHub Actions (see .github/workflows/monitor.yml).

Notifications — set EITHER of these (Telegram is simpler; both work together):

  Telegram (recommended):
    TELEGRAM_TOKEN     bot token from @BotFather
    TELEGRAM_CHAT_ID   your chat id (from @userinfobot)

  Email (optional):
    SMTP_USER          Gmail address that sends the alert
    SMTP_PASSWORD      Gmail *App Password* (NOT your normal password)
    ALERT_TO           where the alert is sent (defaults to SMTP_USER)
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
# The dashboard (GitHub Pages) reads this file. Lives under docs/ so Pages serves it.
DATA_PATH = os.path.join(os.path.dirname(__file__), "docs", "data.json")
MAX_HISTORY = 200  # cap stored price points per listing

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AE,en;q=0.9,ar;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# A price that appears next to a currency token (AED / DHS / درهم / د.إ).
# Currency context avoids grabbing unrelated numbers from the page.
CURRENCY = r"(?:AED|aed|DHS|Dhs|dhs|درهم|د\.إ)"
NUM = r"(\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d{3,6}(?:\.\d+)?)"
PRICE_CTX_RE = re.compile(rf"{CURRENCY}\s*{NUM}|{NUM}\s*{CURRENCY}")
# Bare number, used only for explicit selectors / structured data.
BARE_NUM_RE = re.compile(NUM)

MIN_PRICE, MAX_PRICE = 1000, 20000  # sanity window for a MacBook (AED)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def to_float(raw):
    """'4,199.00' / 'AED 4199' / 4200 -> 4199.0 etc., or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = BARE_NUM_RE.search(str(raw))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def price_from_text(text):
    """First currency-anchored price in a blob of text, within sanity window."""
    for m in PRICE_CTX_RE.finditer(text or ""):
        raw = m.group(1) or m.group(2)
        p = to_float(raw)
        if p and MIN_PRICE <= p <= MAX_PRICE:
            return p
    return None


def sane(price):
    return price is not None and MIN_PRICE <= price <= MAX_PRICE


def is_trusted(url, trusted_domains):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in trusted_domains)


def title_ok(title, require, exclude):
    t = (title or "").lower()
    if require and not all(k.lower() in t for k in require):
        return False
    if exclude and any(k.lower() in t for k in exclude):
        return False
    return True


# --------------------------------------------------------------------------- #
#  Discovery from a search-results page
# --------------------------------------------------------------------------- #
def _walk_json_items(obj, base_url, out):
    """Find dicts that look like a product (name + price, maybe url)."""
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("title") or obj.get("product_title")
        price = None
        for k in ("price", "lowPrice", "salePrice", "sale_price", "offerPrice"):
            if k in obj and not isinstance(obj[k], (dict, list)):
                price = to_float(obj[k])
                if price:
                    break
        if not price and isinstance(obj.get("offers"), dict):
            price = to_float(obj["offers"].get("price"))
        url = obj.get("url") or obj.get("link") or obj.get("productUrl")
        if name and sane(price):
            full = urljoin(base_url, url) if url else None
            out.append({"title": str(name).strip(), "url": full, "price": price})
        for v in obj.values():
            _walk_json_items(v, base_url, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json_items(v, base_url, out)


def from_structured(soup, base_url):
    """JSON-LD ItemList / Next.js / embedded JSON product arrays."""
    out = []
    for tag in soup.find_all("script"):
        text = tag.string or tag.get_text() or ""
        if '"price"' not in text.lower() and "ld+json" not in (tag.get("type") or ""):
            continue
        text = text.strip()
        if not (text.startswith("{") or text.startswith("[")):
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        _walk_json_items(data, base_url, out)
    return out


def from_generic_cards(soup, base_url):
    """
    Layout-agnostic fallback: for every link on the page, climb a few
    ancestors looking for a currency-anchored price in the same card.
    """
    results = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        title = (
            a.get("title")
            or a.get("aria-label")
            or a.get_text(" ", strip=True)
            or ""
        )
        if not title:
            img = a.find("img")
            if img:
                title = img.get("alt", "")
        title = title.strip()
        if len(title) < 8:  # skip tiny/navigation links
            continue

        price, node = None, a
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            price = price_from_text(node.get_text(" ", strip=True))
            if price:
                break
        if not sane(price):
            continue

        url = urljoin(base_url, href)
        key = url.split("?")[0]
        if key not in results:  # first (usually cheapest/primary) wins
            results[key] = {"title": title, "url": url, "price": price}
    return list(results.values())


def from_selectors(soup, base_url, store):
    """Explicit per-store CSS selectors from config (optional override)."""
    cards = soup.select(store["card_selector"])
    out = []
    for card in cards:
        link_sel = store.get("link_selector", "a")
        a = card.select_one(link_sel)
        url = urljoin(base_url, a["href"]) if a and a.has_attr("href") else None
        if store.get("title_selector"):
            t_el = card.select_one(store["title_selector"])
            title = t_el.get_text(" ", strip=True) if t_el else ""
        else:
            title = (a.get_text(" ", strip=True) if a else "") or card.get_text(" ", strip=True)
        if store.get("price_selector"):
            p_el = card.select_one(store["price_selector"])
            price = price_from_text(p_el.get_text(" ", strip=True)) or to_float(
                p_el.get_text(strip=True)
            ) if p_el else None
        else:
            price = price_from_text(card.get_text(" ", strip=True))
        if title and sane(price):
            out.append({"title": title.strip(), "url": url, "price": price})
    return out


def discover(html, base_url, store):
    """Return de-duplicated listings found on a search page."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    if store.get("card_selector"):
        listings += from_selectors(soup, base_url, store)
    if not listings:
        listings += from_structured(soup, base_url)
    if not listings:
        listings += from_generic_cards(soup, base_url)

    # De-dup: prefer entries that have a URL; keep lowest price per URL/title.
    best = {}
    for item in listings:
        key = (item.get("url") or "").split("?")[0] or item["title"].lower()
        if key not in best or item["price"] < best[key]["price"]:
            best[key] = item
    return list(best.values())


# --------------------------------------------------------------------------- #
#  Fetching (direct, or via a scraping API that renders JS + UAE residential IP)
# --------------------------------------------------------------------------- #
def _truthy(v):
    return str(v).lower() in ("1", "true", "yes")


def proxied(url, store):
    """
    Return (request_url, params, renders) for the fetch.

    If SCRAPER_API_KEY is set we route through a scraping API so JavaScript
    pages render and requests come from a UAE IP (country_code=ae). Otherwise
    we hit the store directly (works only for plain-HTML stores).
    Supported SCRAPER_PROVIDER values: scraperapi (default), scrapingbee.
    """
    key = os.environ.get("SCRAPER_API_KEY")
    if not key:
        return url, None, False
    provider = os.environ.get("SCRAPER_PROVIDER", "scraperapi").lower()
    country = os.environ.get("SCRAPER_COUNTRY", "ae")
    render = store.get("render", True)  # most UAE stores need JS rendering
    if provider == "scrapingbee":
        return (
            "https://app.scrapingbee.com/api/v1/",
            {
                "api_key": key,
                "url": url,
                "render_js": "true" if render else "false",
                "country_code": country,
            },
            render,
        )
    # default: ScraperAPI
    return (
        "https://api.scraperapi.com/",
        {
            "api_key": key,
            "url": url,
            "render": "true" if render else "false",
            "country_code": country,
        },
        render,
    )


def fetch(url, store=None, retries=3):
    store = store or {}
    base, params, renders = proxied(url, store)
    timeout = 75 if renders else 30  # rendered pages are slower
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(base, params=params, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200 and len(resp.text) > 800:
                return resp.text
            last_err = f"HTTP {resp.status_code} (len={len(resp.text)})"
        except requests.RequestException as exc:
            last_err = str(exc)
        time.sleep(2 * attempt)
    raise RuntimeError(last_err or "unknown fetch error")


# --------------------------------------------------------------------------- #
#  State (avoid re-alerting at the same price)
# --------------------------------------------------------------------------- #
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
#  Email
# --------------------------------------------------------------------------- #
def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=20,
        )
        if resp.status_code == 200:
            print(">> Telegram alert sent")
            return True
        print(f"!! Telegram failed: HTTP {resp.status_code} {resp.text[:200]}")
    except requests.RequestException as exc:
        print(f"!! Telegram error: {exc}")
    return False


def send_email(subject, body):
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("ALERT_TO", user)
    if not user or not password:
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())
        print(f">> Alert email sent to {to_addr}")
        return True
    except OSError as exc:
        print(f"!! Email error: {exc}")
        return False


def notify(subject, body):
    """Send via Telegram and/or email, whichever is configured."""
    text = subject + "\n\n" + body
    sent = send_telegram(text)
    sent = send_email(subject, body) or sent
    if not sent:
        print("!! No notifier configured (set TELEGRAM_* or SMTP_*).")
        print("   Would have sent:\n" + text)
    return sent


# --------------------------------------------------------------------------- #
#  Dashboard data (consumed by docs/index.html on GitHub Pages)
# --------------------------------------------------------------------------- #
def rate_deal(price, history, threshold):
    """Camel/Keepa-style label based on this listing's own price history."""
    prices = [p for _, p in history] or [price]
    low, high = min(prices), max(prices)
    if price <= threshold:
        return "🔥 Target hit"
    if price <= low:
        return "Best price ever"
    if high > low and price <= low + (high - low) * 0.15:
        return "Great deal"
    if high > low and price <= low + (high - low) * 0.40:
        return "Good deal"
    return "Tracking"


def write_dashboard(listings, threshold, now_iso):
    """Write docs/data.json for the static dashboard."""
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    items = []
    for rec in sorted(listings, key=lambda r: r["price"]):
        history = rec.get("history", [])
        prices = [p for _, p in history] or [rec["price"]]
        low, high = min(prices), max(prices)
        items.append(
            {
                "title": rec["title"],
                "store": rec["store"],
                "url": rec["url"],
                "price": rec["price"],
                "lowest": low,
                "highest": high,
                "savings": round(high - rec["price"]) if high > rec["price"] else 0,
                "deal": rate_deal(rec["price"], history, threshold),
                "below_target": rec["price"] <= threshold,
                "history": history,
                "last_seen": rec.get("last_seen", now_iso),
            }
        )
    payload = {
        "updated": now_iso,
        "currency": "AED",
        "threshold": threshold,
        "count": len(items),
        "listings": items,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f">> Dashboard data written: {len(items)} listing(s) -> {DATA_PATH}")


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    threshold = float(cfg["threshold_aed"])
    trusted = cfg.get("trusted_domains", [])
    require = cfg.get("require_keywords", [])
    exclude = cfg.get("exclude_keywords", [])
    stores = cfg.get("stores", [])
    state = load_state()

    deals = []
    seen_now = []  # every matched listing this run, for the dashboard
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"Discovery run — threshold {threshold:.0f} AED, {len(stores)} store(s)\n")

    for store in stores:
        name = store.get("name", store["search_url"])
        search_url = store["search_url"]
        if "REPLACE" in search_url:
            print(f"-- SKIP (placeholder): {name}")
            continue

        try:
            html = fetch(search_url, store)
        except RuntimeError as exc:
            print(f"!! {name}: search fetch failed / likely blocked -> {exc}")
            continue

        found = discover(html, search_url, store)
        matched = [
            it
            for it in found
            if title_ok(it["title"], require, exclude)
            and it.get("url")
            and is_trusted(it["url"], trusted)
        ]
        print(f"== {name}: {len(found)} listings found, {len(matched)} match filters")

        for it in matched:
            url, price = it["url"], it["price"]
            rec = state.get(url, {})
            history = rec.get("history", [])
            # Append a point only if price changed (keeps history compact).
            if not history or history[-1][1] != price:
                history.append([now_iso, price])
            history = history[-MAX_HISTORY:]

            rec.update(
                {
                    "title": it["title"],
                    "store": name,
                    "url": url,
                    "price": price,
                    "history": history,
                    "last_seen": now_iso,
                }
            )
            state[url] = rec
            seen_now.append(rec)

            tag = "  <-- BELOW THRESHOLD" if price <= threshold else ""
            print(f"     {price:.0f} AED  {it['title'][:60]}{tag}")

            if price <= threshold:
                prev = rec.get("last_alert_price")
                if prev is None or price < prev:
                    deals.append((name, it))
                    rec["last_alert_price"] = price
            else:
                rec.pop("last_alert_price", None)

    save_state(state)
    write_dashboard(seen_now, threshold, now_iso)

    if not seen_now:
        print(
            "\nNo matching listings found. Either the M5 isn't listed yet, the "
            "stores blocked the request, or the filters are too strict "
            "(check require_keywords in config.yaml)."
        )

    if deals:
        lines = ["A MacBook Air M5 dropped to or below your target price!\n"]
        for store_name, it in sorted(deals, key=lambda d: d[1]["price"]):
            lines.append(
                f"* {it['price']:.0f} AED — {store_name}\n  {it['title']}\n  {it['url']}\n"
            )
        body = "\n".join(lines)
        cheapest = min(d[1]["price"] for d in deals)
        subject = f"🔥 MacBook Air M5 price drop! From {cheapest:.0f} AED"
        notify(subject, body)
    else:
        print("\nNo new deals below threshold this run.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
