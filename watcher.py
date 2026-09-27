"""Stock watcher (Hlídač skladu) – checks every item in items.json and sends a WhatsApp
message when a wanted size comes back in stock.

Usage:
  python watcher.py                 # normal run (used by GitHub Actions)
  python watcher.py --dry-run       # print messages instead of sending, don't save state
  python watcher.py --test-message  # just send a test WhatsApp message
  python watcher.py --inspect URL   # show what the checker sees on a product page
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as creq


# ======================================================================
# Data model
# ======================================================================
class FetchError(Exception):
    """The page could not be downloaded (network error, blocked, 404...)."""


class ParseError(Exception):
    """The page was downloaded but stock data could not be found in it."""


@dataclass
class ProductStatus:
    name: str | None = None
    # size label -> available?
    sizes: dict[str, bool] = field(default_factory=dict)
    # used when the shop exposes no per-size data
    product_available: bool | None = None

    def available_sizes(self) -> list[str]:
        return [s for s, ok in self.sizes.items() if ok]


def norm_size(s: str) -> str:
    return " ".join(str(s).strip().upper().split())


# ======================================================================
# Downloading pages
# ======================================================================
BLOCK_MARKERS = (
    "captcha-delivery.com",  # DataDome
    "cf-chl-",               # Cloudflare challenge
    "challenge-platform",
    "px-captcha",            # PerimeterX
)

HEADERS = {
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def get_html(url: str, attempts: int = 3) -> str:
    last = ""
    for i in range(attempts):
        if i:
            time.sleep(3 * i + random.random() * 2)
        try:
            # impersonate a real Chrome (TLS + HTTP/2 fingerprint) – helps with anti-bot walls
            r = creq.get(url, impersonate="chrome", headers=HEADERS, timeout=30, allow_redirects=True)
        except Exception as e:  # network problems
            last = f"síťová chyba: {e}"
            continue
        text = r.text or ""
        if r.status_code == 404:
            raise FetchError("stránka neexistuje (404) – produkt byl možná stažen z prodeje")
        challenged = r.status_code != 200 and any(m in text[:20000] for m in BLOCK_MARKERS)
        if r.status_code in (403, 429, 503) or challenged:
            last = f"e-shop zablokoval přístup (HTTP {r.status_code})"
            continue
        if r.status_code >= 400:
            last = f"HTTP {r.status_code}"
            continue
        return text
    raise FetchError(last or "nepodařilo se stáhnout stránku")


# ======================================================================
# Shop: Reserved & other LPP shops
# ======================================================================
# LPP group shops: Reserved, Mohito, Sinsay, House, Cropp.
#
# The product page embeds two JS functions with JSON payloads:
#   window['getProductData'] = function() { return {... "sizes": [{sizeId, sizeName, stockQuantity, isInStock}] ...}; };
#   window['getStockData']   = function() { return {"<sizeId>": <qty>, ...}; };


LPP_DOMAINS = ("reserved.com", "mohito.com", "sinsay.com", "housebrand.com", "cropp.com")


def _js_return(html: str, fname: str):
    m = re.search(r"window\[['\"]%s['\"]\]\s*=\s*function\s*\(\)\s*\{\s*return\s*" % re.escape(fname), html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
        return obj
    except ValueError:
        return None


def lpp_parse(html: str, url: str) -> ProductStatus:
    product = _js_return(html, "getProductData")
    if not isinstance(product, dict) or "sizes" not in product:
        raise ParseError("na stránce Reserved/LPP jsem nenašla data o velikostech")
    stock = _js_return(html, "getStockData") or {}

    sizes: dict[str, bool] = {}
    for s in product.get("sizes") or []:
        label = str(s.get("sizeName") or s.get("sizeId"))
        sid = str(s.get("sizeId"))
        if sid in stock:
            ok = (stock.get(sid) or 0) > 0
        else:
            ok = bool(s.get("isInStock") or s.get("stock") or (s.get("stockQuantity") or 0) > 0)
        sizes[label] = sizes.get(label, False) or ok

    return ProductStatus(name=product.get("name") or product.get("extendedName"), sizes=sizes)


# ======================================================================
# Shop: Decathlon
# ======================================================================
# Decathlon (Next.js app router).
#
# Product data is streamed in `self.__next_f.push([1,"..."])` chunks. After decoding
# and joining them, every SKU (= one size) looks like
#   {"skuId":"…", … "sizeLabel":"36", … "itemGroupId":"334419","modelId":"8914038", … "isAvailable":true, …}
# The page also contains recommended products, so SKUs are filtered by the model id
# (`?mc=` in the URL) or by the item group id (`R-p-XXXX`).
# Sizes that are sold out completely are often missing from the page – that simply
# means "not available".


DECATHLON_DOMAINS = ("decathlon.",)

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)
_SKU = re.compile(r'\{"skuId":"')


def _flight_text(html: str) -> str:
    out = []
    for m in _PUSH.finditer(html):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            pass
    return "".join(out)


def _field(seg: str, key: str):
    m = re.search(r'"%s":(?:"([^"]*)"|(true|false))' % key, seg)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2) == "true"


def decathlon_parse(html: str, url: str) -> ProductStatus:
    q = parse_qs(urlparse(url).query)
    model = (q.get("mc") or [None])[0]
    g = re.search(r"R-p-(\d+)", url)
    group = g.group(1) if g else None

    text = _flight_text(html)
    if not text:
        raise ParseError("na stránce Decathlonu jsem nenašla produktová data")

    starts = [m.start() for m in _SKU.finditer(text)]
    sizes: dict[str, bool] = {}
    for i, s in enumerate(starts):
        seg = text[s: starts[i + 1] if i + 1 < len(starts) else s + 30000]
        size = _field(seg, "sizeLabel")
        if not size:
            continue
        if model and _field(seg, "modelId") != model:
            continue
        if not model and group and _field(seg, "itemGroupId") != group:
            continue
        ok = _field(seg, "isAvailable") is True
        sizes[size] = sizes.get(size, False) or ok

    name = None
    t = re.search(r"<title>([^<]+)</title>", html)
    if t:
        name = htmllib.unescape(t.group(1)).split("|")[0].strip()

    if not sizes:
        # no SKU of this model on the page at all -> everything sold out, or page changed
        if '"sizeLabel"' not in text:
            raise ParseError("na stránce Decathlonu jsem nenašla velikosti")
    return ProductStatus(name=name, sizes=sizes, product_available=any(sizes.values()) if sizes else False)


# ======================================================================
# Shop: any other (schema.org JSON-LD)
# ======================================================================
# Fallback for any other shop: schema.org JSON-LD (used by most e-shops for Google).
#
# Reads Product / ProductGroup(hasVariant) offers. If variants carry a size, reports
# per-size availability; otherwise just whether the product is in stock.


_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
IN_STOCK = ("instock", "limitedavailability", "onlineonly", "instoreonly", "presale", "preorder")


def _walk(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "hasVariant", "isVariantOf"):
            if k in node:
                yield from _walk(node[k])


def _types(n) -> set[str]:
    t = n.get("@type")
    return set(t if isinstance(t, list) else [t]) if t else set()


def _offer_available(offers) -> bool | None:
    res = None
    for o in offers if isinstance(offers, list) else [offers]:
        if not isinstance(o, dict):
            continue
        a = str(o.get("availability", "")).lower().rsplit("/", 1)[-1]
        if a:
            res = bool(res) or a in IN_STOCK
    return res


def _size(n) -> str | None:
    s = n.get("size")
    if isinstance(s, dict):
        s = s.get("name")
    if s:
        return str(s)
    for p in n.get("additionalProperty") or []:
        if isinstance(p, dict) and str(p.get("name", "")).lower() in ("size", "velikost", "velikosť", "rozmiar"):
            return str(p.get("value"))
    return None


def generic_parse(html: str, url: str) -> ProductStatus:
    nodes = []
    for m in _LD.finditer(html):
        try:
            nodes.extend(_walk(json.loads(htmllib.unescape(m.group(1).strip()))))
        except ValueError:
            continue
    products = [n for n in nodes if _types(n) & {"Product", "ProductGroup"}]
    if not products:
        meta = re.search(r'property=["\']product:availability["\'][^>]*content=["\']([^"\']+)', html, re.I)
        if meta:
            return ProductStatus(product_available=meta.group(1).lower().replace(" ", "") in IN_STOCK)
        raise ParseError("tento e-shop zatím neumím přečíst (nenašla jsem strukturovaná data o produktu)")

    name = next((p.get("name") for p in products if p.get("name")), None)
    sizes: dict[str, bool] = {}
    overall = None
    for p in products:
        if "offers" not in p:
            continue
        a = _offer_available(p["offers"])
        if a is None:
            continue
        overall = bool(overall) or a
        s = _size(p)
        if s:
            sizes[s] = sizes.get(s, False) or a
    return ProductStatus(name=name, sizes=sizes, product_available=overall)

def pick_shop(url: str):
    host = urlparse(url).netloc.lower()
    if any(d in host for d in LPP_DOMAINS):
        return "Reserved/LPP", lpp_parse
    if any(d in host for d in DECATHLON_DOMAINS):
        return "Decathlon", decathlon_parse
    return host.removeprefix("www."), generic_parse


# ======================================================================
# Notifications (WhatsApp via CallMeBot, optional Telegram)
# ======================================================================


def _whatsapp(text: str) -> str | None:
    phone, key = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if not (phone and key):
        return None
    phone = phone.replace(" ", "")
    for attempt in range(3):
        try:
            r = creq.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": phone, "text": text, "apikey": key},
                timeout=40,
            )
            body = (r.text or "").lower()
            if r.status_code == 200 and "error" not in body[:300]:
                return "whatsapp: ok"
            err = f"whatsapp: HTTP {r.status_code} {r.text[:200]!r}"
        except Exception as e:
            err = f"whatsapp: {e}"
        time.sleep(5 * (attempt + 1))
    return err


def _telegram(text: str) -> str | None:
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return None
    try:
        r = creq.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": False}, timeout=30)
        return "telegram: ok" if r.status_code == 200 else f"telegram: HTTP {r.status_code}"
    except Exception as e:
        return f"telegram: {e}"


def send_message(text: str, dry_run: bool = False) -> list[str]:
    if dry_run:
        print("---- [dry-run] zpráva ----\n" + text + "\n-------------------------")
        return ["dry-run"]
    results = [r for r in (_whatsapp(text), _telegram(text)) if r]
    if not results:
        results = ["žádný kanál není nastaven (chybí CALLMEBOT_PHONE / CALLMEBOT_APIKEY)"]
    for r in results:
        print("  notify ->", r)
    return results


# ======================================================================
# Main loop
# ======================================================================


ROOT = Path(__file__).resolve().parent
ITEMS = ROOT / "items.json"
STATE = ROOT / "state.json"
ERROR_ALERT_AFTER = 6  # consecutive failed checks (~3 h at 30 min interval)
ANY = "(skladem)"      # pseudo-size used when the shop has no size data


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default


def check_url(url: str) -> tuple[str, ProductStatus]:
    label, parser = pick_shop(url)
    return label, parser(get_html(url), url)


def matching(item: dict, st: ProductStatus) -> tuple[list[str], str | None]:
    """Return (sizes that satisfy the item's rule, optional warning)."""
    watch = {norm_size(s) for s in item.get("watch_sizes") or [] if str(s).strip()}
    ignore = {norm_size(s) for s in item.get("ignore_sizes") or [] if str(s).strip()}
    warn = None
    if st.sizes:
        avail = st.available_sizes()
        if watch:
            known = {norm_size(s) for s in st.sizes}
            missing = sorted(watch - known)
            if missing:
                warn = f"velikost {', '.join(missing)} na stránce zatím není (zná: {', '.join(st.sizes)})"
            return [s for s in avail if norm_size(s) in watch], warn
        return [s for s in avail if norm_size(s) not in ignore], warn
    if watch or ignore:
        warn = "e-shop neukazuje velikosti – hlídám jen, jestli je produkt skladem"
    return ([ANY] if st.product_available else []), warn


def rule_text(item: dict) -> str:
    if item.get("watch_sizes"):
        return "velikost " + ", ".join(item["watch_sizes"])
    if item.get("ignore_sizes"):
        return "jakákoliv velikost kromě " + ", ".join(item["ignore_sizes"])
    return "cokoliv skladem"


def run(dry_run: bool = False, only: str | None = None) -> int:
    cfg = load(ITEMS, {"items": []})
    state = load(STATE, {})
    old_state_json = json.dumps(state, sort_keys=True, ensure_ascii=False)
    items_state: dict = state.setdefault("items", {})

    items = [i for i in cfg.get("items", []) if i.get("enabled", True) and i.get("url")]
    if only:
        items = [i for i in items if i.get("id") == only]
    print(f"Kontroluji {len(items)} položek…")

    for n, item in enumerate(items):
        iid = item.get("id") or item["url"]
        prev = items_state.get(iid, {})
        entry = dict(prev)
        title = item.get("name") or prev.get("name") or item["url"]
        if n:
            time.sleep(2 + random.random() * 3)  # be polite
        try:
            shop, st = check_url(item["url"])
        except (FetchError, ParseError) as e:
            entry["errors"] = prev.get("errors", 0) + 1
            entry["last_error"] = str(e)
            entry["status"] = "error"
            print(f"  ✗ {title}: {e} (chyba č. {entry['errors']})")
            if entry["errors"] == ERROR_ALERT_AFTER:
                send_message(f"⚠️ Hlídač skladu: {title}\nUž {ERROR_ALERT_AFTER}× po sobě se nepodařilo zkontrolovat "
                            f"({e}).\n{item['url']}", dry_run)
            items_state[iid] = entry
            continue
        except Exception as e:  # unexpected bug – keep going with other items
            entry["errors"] = prev.get("errors", 0) + 1
            entry["last_error"] = f"neočekávaná chyba: {e!r}"
            entry["status"] = "error"
            print(f"  ✗ {title}: {e!r}")
            items_state[iid] = entry
            continue

        match, warn = matching(item, st)
        prev_match = set(prev.get("matched", []))
        new = [s for s in match if s not in prev_match]
        gone = [s for s in prev_match if s not in match]

        entry.update({
            "name": st.name or prev.get("name"),
            "shop": shop,
            "sizes": st.sizes,
            "product_available": st.product_available,
            "matched": sorted(match),
            "errors": 0,
            "last_error": None,
            "warning": warn,
            "status": "available" if match else "waiting",
        })
        if prev.get("errors", 0) >= ERROR_ALERT_AFTER:
            send_message(f"✅ Hlídač skladu: {title} – kontrola zase funguje.", dry_run)

        avail = ", ".join(st.available_sizes()) or ("skladem" if st.product_available else "nic")
        print(f"  ✓ {title} [{shop}] dostupné: {avail} | hlídám: {rule_text(item)} | shoda: {match or '—'}")

        if new:
            sizes_txt = "" if new == [ANY] else f"\nVelikost: {', '.join(new)}"
            send_message(f"🛍️ Naskladněno! {title}{sizes_txt}\n{item['url']}", dry_run)
            entry["last_notified"] = now()
        if gone and item.get("notify_sold_out"):
            sizes_txt = "" if gone == [ANY] else f" (velikost {', '.join(sorted(gone))})"
            send_message(f"😕 Vyprodáno: {title}{sizes_txt}\n{item['url']}", dry_run)

        # only bump timestamp when something meaningful changed (keeps git history quiet)
        cmp_keys = ("sizes", "matched", "status", "warning", "last_error", "name", "product_available")
        if any(prev.get(k) != entry.get(k) for k in cmp_keys):
            entry["changed_at"] = now()
        items_state[iid] = entry

    # forget items that were deleted from items.json
    if not only:
        live = {i.get("id") or i["url"] for i in cfg.get("items", [])}
        for k in list(items_state):
            if k not in live:
                del items_state[k]

    # daily heartbeat keeps the repo "active" so GitHub doesn't pause the schedule
    state["heartbeat"] = datetime.now(timezone.utc).date().isoformat()

    if dry_run:
        print("[dry-run] state.json neukládám")
    elif json.dumps(state, sort_keys=True, ensure_ascii=False) != old_state_json:
        STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
        print("state.json aktualizován")
    return 0


def inspect(url: str) -> int:
    try:
        shop, st = check_url(url)
    except (FetchError, ParseError) as e:
        print("Chyba:", e)
        return 1
    print(f"Obchod: {shop}\nNázev: {st.name}")
    if st.sizes:
        for s, ok in st.sizes.items():
            print(f"  {'✅' if ok else '❌'} {s}")
    else:
        print("Velikosti: e-shop je neukazuje; produkt skladem:", st.product_available)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-message", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--inspect")
    a = ap.parse_args(argv)
    if a.test_message:
        res = send_message("👋 Testovací zpráva z Hlídače skladu – notifikace fungují!")
        return 0 if any("ok" in r for r in res) else 1
    if a.inspect:
        return inspect(a.inspect)
    return run(dry_run=a.dry_run, only=a.only)


if __name__ == "__main__":
    sys.exit(main())
