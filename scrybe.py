#!/usr/bin/env python3
"""
Adromedary's Scrybe - Magic: The Gathering card image downloader
Version 1.0.0 - By Adromedary

Paste or open decklists, and Scrybe finds the cleanest English printing of every
card on Scryfall and saves the images to a folder. Standard library only
(tkinter, urllib, json, gzip, csv, xml) - nothing to pip install.

Run:    python scrybe.py
Build:  run build.bat (Windows) - see README.md

What it does
------------
* Reads plain text, MTG Arena / Moxfield / Archidekt exports ("1 Sol Ring (C21) 263 *F*"),
  Forge .dck ("1 Forest|ZNR") and MTGO .dek (XML) files, with section headers
  (Commander, Deck, Sideboard, Maybeboard...). A pinned set + collector number is
  honoured exactly; a pinned set alone limits the choice to that set.
* Imports public Archidekt decks from a URL.
* Card data: downloads Scryfall's "default cards" bulk file (~80 MB) once, caches a
  compact index, and refreshes it when Scryfall publishes a new one. Cards newer
  than the cached data, and everything in "Live API" mode, go through the API.
* Picks the printing with the lowest "weirdness score" (see score_card): English
  first, then no promos / showcase / borderless / extended art / full art /
  textless / Secret Lair / Un-sets / digital-only / missing scans, then the art
  preference (newest normal frame, original printing, or classic old border).
* Basic lands with 2+ copies get random distinct arts, one set each where possible,
  named "Forest (Zendikar Rising).jpg".
* Optional back faces of double-faced cards, tokens and emblems the deck makes,
  per-copy files for proxy printing, card-type subfolders, CSV manifest.
* Typos are fixed with Scryfall's fuzzy search and listed in the Issues tab.
* Network calls that time out (or hit a server error) go to the back of the queue
  and are retried, up to 3 attempts. Images download 4 at a time.

Card data and images are provided by Scryfall (scryfall.com). Scrybe is unofficial
Fan Content; not approved or endorsed by Wizards of the Coast or Scryfall.
"""

import csv
import gzip
import hashlib
import json
import os
import queue
import random
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

try:  # the engine works without tkinter (used by the tests); the GUI needs it
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
except ImportError:  # pragma: no cover
    tk = None

APP_NAME = "Adromedary's Scrybe"
__version__ = "1.0.0"
__author__ = "Adromedary"

USER_AGENT = f"AdromedarysScrybe/{__version__}"
API = "https://api.scryfall.com"
API_MIN_INTERVAL = 0.11      # Scryfall asks for roughly 10 requests per second at most
HTTP_TIMEOUT = 30
MAX_ATTEMPTS = 3             # a timed-out card is retried at the back of the queue, 3 tries total
DOWNLOAD_WORKERS = 4         # images come from Scryfall's CDN, which isn't rate limited
COLLECTION_BATCH = 75
BULK_CHECK_HOURS = 24        # how often to ask Scryfall whether newer card data exists
INDEX_FORMAT = 6             # bump when the cached index layout changes
PROGRESS_EVERY = 5000

# ===========================================================================
# Networking
# ===========================================================================


class NotFound(Exception):
    pass


class NetError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


_api_lock = threading.Lock()
_api_last = [0.0]


def _describe(e):
    reason = getattr(e, "reason", None)
    text = str(reason if reason is not None else e)
    if "timed out" in text.lower() or isinstance(e, TimeoutError) or isinstance(reason, TimeoutError):
        return "timed out"
    return text


def http_bytes(url, data=None, method=None, timeout=HTTP_TIMEOUT):
    """GET/POST returning raw bytes. Raises NotFound on 404, NetError otherwise.
    429 responses wait for Retry-After and try again without counting as a failure."""
    is_api = url.startswith(API)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json;q=0.9,*/*;q=0.8"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for _ in range(8):
        if is_api:
            with _api_lock:
                wait = API_MIN_INTERVAL - (time.monotonic() - _api_last[0])
                if wait > 0:
                    time.sleep(wait)
                _api_last[0] = time.monotonic()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url)
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = float(ra)
                except (TypeError, ValueError):
                    delay = 2.0
                time.sleep(min(max(delay, 0.5), 60))
                continue
            raise NetError(f"HTTP {e.code}", retryable=e.code >= 500)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise NetError(_describe(e), retryable=True)
    raise NetError("rate limited by Scryfall", retryable=True)


def get_json(url):
    return json.loads(http_bytes(url).decode("utf-8"))


def post_json(url, payload):
    return json.loads(http_bytes(url, json.dumps(payload).encode("utf-8"), "POST").decode("utf-8"))


def download_file(url, dest, total_hint=None, on_progress=None, cancel=None):
    """Stream a large file to disk (used for the bulk card data)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0) or (total_hint or 0)
            got = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise NetError("cancelled")
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
        os.replace(tmp, dest)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, NetError):
            raise
        raise NetError(_describe(e), retryable=True)


# ===========================================================================
# Text helpers
# ===========================================================================


def norm(name):
    """Normalise a card name for matching: accents, curly quotes, case, spacing, '//'."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = (s.replace("’", "'").replace("‘", "'").replace("“", '"')
         .replace("”", '"').replace("–", "-").replace("—", "-"))
    s = re.sub(r"\s*/{1,2}\s*", " // ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.replace("æ", "ae")


def safe_filename(name):
    s = name.replace(" // ", " - ").replace("//", "-")
    s = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", s).strip().rstrip(".")
    return s or "card"


def image_url(url, size):
    """Turn a Scryfall image URL into another size: normal / large / png."""
    if not url:
        return None
    out = re.sub(r"/(small|normal|large|png|art_crop|border_crop)/", f"/{size}/", url, count=1)
    if size == "png":
        out = re.sub(r"\.jpg(\?|$)", r".png\1", out)
    else:
        out = re.sub(r"\.png(\?|$)", r".jpg\1", out)
    return out


# ===========================================================================
# Decklist parsing
# ===========================================================================

SECTION_WORDS = {
    "commander": "commander", "commanders": "commander", "command zone": "commander",
    "companion": "commander", "companions": "commander",
    "deck": "main", "main": "main", "main deck": "main", "maindeck": "main",
    "mainboard": "main", "library": "main", "cards": "main",
    "sideboard": "sideboard", "side": "sideboard", "sb": "sideboard",
    "outside the game": "sideboard", "attractions": "sideboard", "stickers": "sideboard",
    "maybeboard": "maybe", "maybe": "maybe", "considering": "maybe", "maybe board": "maybe",
    "tokens": "tokens",
    "about": "about", "metadata": "about",
}

QTY_RE = re.compile(r"^(\d+)\s*[xX]?\s+(.+)$")
SET_NUM_RE = re.compile(r"\s+[\(\[]([A-Za-z0-9]{2,6})[\)\]](?:\s+([^\s\[\(]+))?\s*$")


class Entry:
    __slots__ = ("name", "qty", "set", "number", "section", "line")

    def __init__(self, name, qty=1, set_code=None, number=None, section="main", line=0):
        self.name = name
        self.qty = qty
        self.set = (set_code or "").lower() or None
        self.number = (number or "").lower() or None
        self.section = section
        self.line = line

    def __repr__(self):
        return f"Entry({self.qty} {self.name!r} set={self.set} cn={self.number} [{self.section}])"


def _header_section(line):
    t = re.sub(r"^(//+|#+)\s*", "", line.strip())
    t = re.sub(r"^\[(.*)\]$", r"\1", t)
    t = re.sub(r"\s*\(\d+\)\s*$", "", t).rstrip(":").strip().lower()
    return SECTION_WORDS.get(t)


def looks_like_decklist(text):
    if "<Cards" in text:
        return True
    return any(QTY_RE.match(l.strip()) for l in text.splitlines())


def parse_decklist(text):
    """Return a list of Entry objects from any common decklist format."""
    text = text.lstrip("﻿")
    if text.lstrip().startswith("<") and "<Cards" in text:
        return _parse_dek(text)

    entries = []
    section = "main"
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        sec = _header_section(line)
        if sec:
            section = sec
            continue
        if line.startswith(("//", "#")) or re.fullmatch(r"\[[^\]]*\]", line):
            continue  # comment or a category header we don't need
        if section == "about" or re.match(r"^(name|author|description)\s*[=:]", line, re.I):
            continue

        m = QTY_RE.match(line)
        qty, rest = (int(m.group(1)), m.group(2)) if m else (1, line)
        if qty <= 0:
            continue
        line_section = section
        set_code = number = None

        rest = re.sub(r"\s*\^[^^]*\^", "", rest)                     # Archidekt ^tags^
        for cat in re.findall(r"\[([^\]]*)\]", rest):               # Archidekt [Category] / [SET]
            low = cat.lower()
            if re.fullmatch(r"[A-Z0-9]{3,5}", cat) and not set_code:
                set_code = cat
            elif "maybe" in low or "{nodeck}" in low:
                line_section = "maybe"
            elif low.startswith("commander"):
                line_section = "commander"
            elif low.startswith("sideboard"):
                line_section = "sideboard"
        rest = re.sub(r"\s*\[[^\]]*\]", "", rest)
        rest = re.sub(r"\s*\*[A-Za-z]{1,3}\*", "", rest)              # *F* *E* foil markers
        if "|" in rest:                                               # Forge: Name|SET|art
            parts = rest.split("|")
            rest = parts[0]
            if len(parts) > 1 and parts[1].strip():
                set_code = parts[1].strip()
        sm = SET_NUM_RE.search(rest)
        if sm:
            set_code, number = sm.group(1), sm.group(2)
            rest = rest[:sm.start()]
        name = rest.strip()
        if not re.search(r"[A-Za-z]", name):
            continue
        entries.append(Entry(name, qty, set_code, number, line_section, i))
    return merge_entries(entries)


def _parse_dek(text):
    entries = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return entries
    for c in root.iter("Cards"):
        name = c.get("Name")
        if not name:
            continue
        try:
            qty = int(c.get("Quantity", "1"))
        except ValueError:
            qty = 1
        side = (c.get("Sideboard", "false").lower() == "true")
        entries.append(Entry(name, qty, section="sideboard" if side else "main"))
    return merge_entries(entries)


def merge_entries(entries):
    merged = OrderedDict()
    for e in entries:
        key = (norm(e.name), e.set, e.number, e.section)
        if key in merged:
            merged[key].qty += e.qty
        else:
            merged[key] = e
    return list(merged.values())


def fetch_archidekt(url):
    """Return (deck_name, text_with_printings, text_names_only) for a public Archidekt deck."""
    m = re.search(r"archidekt\.com/(?:api/)?decks/(\d+)", url)
    if not m:
        raise ValueError("That doesn't look like an Archidekt deck link.")
    data = get_json(f"https://archidekt.com/api/decks/{m.group(1)}/")
    included = {c.get("name"): c.get("includedInDeck", True) for c in data.get("categories") or []}
    groups = OrderedDict((k, ([], [])) for k in ("Commander", "Deck", "Sideboard", "Maybeboard"))
    for c in data.get("cards") or []:
        card = c.get("card") or {}
        name = (card.get("oracleCard") or {}).get("name")
        if not name:
            continue
        cats = c.get("categories") or []
        if any(x.lower() == "commander" for x in cats):
            g = "Commander"
        elif cats and not included.get(cats[0], True):
            g = "Maybeboard"
        elif any(x.lower() == "sideboard" for x in cats):
            g = "Sideboard"
        else:
            g = "Deck"
        qty = c.get("quantity") or 1
        ed = ((card.get("edition") or {}).get("editioncode") or "").upper()
        cn = card.get("collectorNumber") or ""
        pinned = f"{qty} {name} ({ed}) {cn}".rstrip() if ed else f"{qty} {name}"
        groups[g][0].append(pinned)
        groups[g][1].append(f"{qty} {name}")

    def render(idx):
        out = []
        for g, lists in groups.items():
            if lists[idx]:
                out.append(g)
                out.extend(lists[idx])
                out.append("")
        return "\n".join(out).strip() + "\n"

    return data.get("name") or f"Archidekt {m.group(1)}", render(0), render(1)


# ===========================================================================
# Printings and the "weirdness" score
# ===========================================================================

EXCLUDED_LAYOUTS = {
    "token", "double_faced_token", "emblem", "art_series", "scheme", "vanguard",
    "planar", "augment", "host", "reversible_card",
}
WEIRD_FRAME_EFFECTS = {
    "showcase", "extendedart", "borderless", "etched", "companion", "shatteredglass",
    "inverted", "fullart", "textless", "colorshifted", "placeholderimage",
}
WEIRD_SET_TYPES = {
    "promo", "box", "memorabilia", "funny", "alchemy", "arsenal", "token", "minigame",
    "augment", "vanguard", "masterpiece", "treasure_chest", "substitute",
}
# promo_types that don't change how a card looks
HARMLESS_PROMO_TYPES = {
    "universesbeyond", "startercollection", "starterdeck", "beginnerbox", "intropack",
    "planeswalkerdeck", "themepack",
}
HARMLESS_PROMO_RE = re.compile(r"^ff[a-z0-9]*$")   # Final Fantasy sub-game tags (ffvii, ffx...)
OLD_FRAMES = ("1993", "1997")
TYPE_ORDER = ("Creature", "Planeswalker", "Battle", "Land", "Instant", "Sorcery",
              "Artifact", "Enchantment")
TYPE_FOLDERS = {"Creature": "Creatures", "Planeswalker": "Planeswalkers", "Battle": "Battles",
                "Land": "Lands", "Instant": "Instants", "Sorcery": "Sorceries",
                "Artifact": "Artifacts", "Enchantment": "Enchantments", "Other": "Other"}


def score_card(card, is_token=False):
    """Return (full_score, base_score, reasons). 0 = a completely normal printing.
    base_score holds only the hard problems (language, missing scan, digital,
    oversized, non-game layouts) and is what's used when the filter is off."""
    full = base = 0
    why = []

    def add(points, reason, hard=False):
        nonlocal full, base
        full += points
        if hard:
            base += points
        why.append(reason)

    lang = card.get("lang") or "en"
    if lang != "en":
        add(8, f"non-English ({lang})", True)
    status = card.get("image_status")
    if status in ("placeholder", "missing"):
        add(20, "no image scan", True)
    elif status == "lowres":
        add(1, "low-res scan", True)
    if card.get("digital"):
        add(5, "digital-only", True)
    if card.get("oversized"):
        add(5, "oversized", True)
    if not is_token and card.get("layout") in EXCLUDED_LAYOUTS:
        add(10, f"layout: {card.get('layout')}", True)

    if card.get("content_warning"):
        add(5, "retired art")
    if card.get("textless"):
        add(3, "textless")
    set_type = card.get("set_type")
    if set_type in WEIRD_SET_TYPES and not (is_token and set_type == "token"):
        add(3, "Secret Lair" if set_type == "box" else f"{set_type} set")
    border = card.get("border_color")
    if border not in ("black", "white"):
        add(3, f"{border} border")
    if card.get("full_art"):
        add(2, "full art")
    if card.get("promo"):
        add(2, "promo")
    if card.get("variation"):
        add(2, "alternate-art variation")
    if card.get("set") in ("plst", "plist", "ulst", "slx"):
        add(1, "The List reprint")
    type_line = (card.get("type_line") or "").lower()
    if "basic" in type_line and "land" in type_line and "universesbeyond" in (card.get("promo_types") or []):
        add(1, "Universes Beyond basic")
    frame = card.get("frame")
    if frame == "future":
        add(2, "timeshifted frame")
    elif frame in OLD_FRAMES and (card.get("released_at") or "") >= "2003-07-29" and not is_token:
        add(2, "retro frame")
    fx = set(card.get("frame_effects") or []) & WEIRD_FRAME_EFFECTS
    if fx:
        add(2 * len(fx), ", ".join(sorted(fx)))
    promo_types = [t for t in (card.get("promo_types") or [])
                   if t not in HARMLESS_PROMO_TYPES and not HARMLESS_PROMO_RE.match(t)]
    if promo_types:
        add(len(promo_types), ", ".join(promo_types))
    return full, base, why


class Print:
    """A compact record of one printing - small enough to cache ~110k of them."""
    FIELDS = ("id", "name", "front", "back_name", "set", "set_name", "cn", "released",
              "lang", "sf", "sb", "why", "img", "back_img", "frame", "illus", "ptype",
              "basic", "tokens", "oracle", "artist", "is_token", "extra")
    __slots__ = FIELDS

    def to_list(self):
        return [getattr(self, f) for f in self.FIELDS]

    @classmethod
    def from_list(cls, values):
        p = cls.__new__(cls)
        for f, v in zip(cls.FIELDS, values):
            setattr(p, f, v)
        return p

    def __repr__(self):
        return f"<{self.name} {self.set.upper()} #{self.cn} {self.lang} sf={self.sf}>"


def _ptype(type_line):
    left = (type_line or "").split("//")[0].split("—")[0]
    for t in TYPE_ORDER:
        if t in left:
            return t
    return "Other"


def make_print(card):
    faces = card.get("card_faces") or []
    iu = card.get("image_uris") or (faces[0].get("image_uris") if faces else None) or {}
    img = iu.get("normal") or iu.get("large") or iu.get("png")
    if not img:
        return None
    p = Print.__new__(Print)
    p.id = card.get("id")
    p.name = card.get("name") or ""
    p.front = faces[0].get("name") if faces else p.name
    p.back_name = p.back_img = None
    if len(faces) > 1 and not card.get("image_uris") and faces[1].get("image_uris"):
        biu = faces[1]["image_uris"]
        p.back_img = biu.get("normal") or biu.get("large") or biu.get("png")
        p.back_name = faces[1].get("name")
    layout = card.get("layout")
    p.is_token = layout in ("token", "double_faced_token", "emblem")
    p.set = (card.get("set") or "").lower()
    p.set_name = card.get("set_name") or p.set.upper()
    p.cn = card.get("collector_number") or ""
    try:
        p.released = int((card.get("released_at") or "0").replace("-", ""))
    except ValueError:
        p.released = 0
    p.lang = card.get("lang") or "en"
    p.sf, p.sb, p.why = score_card(card, p.is_token)
    p.img = img
    p.frame = card.get("frame") or ""
    type_line = card.get("type_line") or (faces[0].get("type_line") if faces else "") or ""
    p.ptype = _ptype(type_line)
    tl = type_line.split("//")[0].lower()
    p.basic = (not p.is_token) and "basic" in tl and "land" in tl
    p.illus = (card.get("illustration_id") or (faces[0].get("illustration_id") if faces else None)) \
        if (p.basic or p.is_token) else None
    p.tokens = []
    if not p.is_token:
        for part in card.get("all_parts") or []:
            if part.get("id") == p.id:
                continue
            if part.get("component") == "token" or "Emblem" in (part.get("type_line") or ""):
                p.tokens.append(part["id"])
    p.oracle = (card.get("oracle_id") or (faces[0].get("oracle_id") if faces else None)) \
        if p.is_token else None
    p.artist = card.get("artist") or ""
    p.extra = None
    if p.is_token:
        src = faces[0] if faces and "power" not in card else card
        pt = f"{src.get('power')}-{src.get('toughness')}" if src.get("power") is not None else ""
        colors = "".join(card.get("colors") or (faces[0].get("colors") if faces else []) or []) or "C"
        p.extra = f"{pt} {colors}".strip()
    return p


# ---------- choosing printings ----------

ART_PREFS = ("newest", "oldest", "classic")


def rank_key(p, use_filter, pref):
    score = p.sf if use_filter else p.sb
    nonen = p.lang != "en"
    old = p.frame in OLD_FRAMES
    if pref == "oldest":
        return (nonen, score, 0, p.released)
    if pref == "classic":
        return (nonen, score, 0 if old else 1, -p.released)
    modern = 0 if p.frame == "2015" else (1 if not old else 2)
    return (nonen, score, modern, -p.released)  # newest normal frame first


def choose_best(prints, use_filter, pref):
    return min(prints, key=lambda p: rank_key(p, use_filter, pref))


def choose_variety(prints, qty, use_filter, pref, rng):
    """Pick `qty` printings with different art, a different set each where possible,
    chosen at random from the cleanest tier. With the filter on it never drops to a
    weirder tier just for variety (no full-art basics sneaking in)."""
    if not prints:
        return [], 0
    ranked = sorted(prints, key=lambda p: (rank_key(p, use_filter, pref), rank_key(p, True, pref)))
    per_art = OrderedDict()                 # one printing per illustration: its cleanest one
    for p in ranked:
        per_art.setdefault(p.illus or p.id, p)
    ranked = list(per_art.values())
    best = rank_key(ranked[0], use_filter, pref)[:2]
    tiers = OrderedDict()
    for p in ranked:
        k = rank_key(p, use_filter, pref)
        if use_filter and k[:2] != best:
            continue
        tiers.setdefault(k[:3], []).append(p)

    chosen, arts, sets = [], set(), set()
    for tier in tiers.values():
        pool = tier[:]
        rng.shuffle(pool)
        for p in pool:                       # pass 1: new set and new art
            if len(chosen) >= qty:
                break
            art = p.illus or p.id
            if p.set not in sets and art not in arts:
                chosen.append(p)
                sets.add(p.set)
                arts.add(art)
        for p in pool:                       # pass 2: new art, set may repeat
            if len(chosen) >= qty:
                break
            art = p.illus or p.id
            if art not in arts:
                chosen.append(p)
                arts.add(art)
        if len(chosen) >= qty:
            break
    distinct = len(chosen)
    i = 0
    while len(chosen) < qty:
        chosen.append(chosen[i % distinct])
        i += 1
    return chosen, distinct


# ===========================================================================
# Card data: bulk index (cached on disk) + live API
# ===========================================================================


def app_data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    path = os.path.join(base, "AdromedarysScrybe") if base else \
        os.path.join(os.path.expanduser("~"), ".adromedarys_scrybe")
    os.makedirs(path, exist_ok=True)
    return path


class CardIndex:
    def __init__(self, prints, label=""):
        self.label = label
        self.count = len(prints)
        self.by_name = {}
        self.by_setnum = {}
        self.tokens_by_id = {}
        self.tokens_by_oracle = {}
        aliases = {}
        for p in prints:
            if p.is_token:
                self.tokens_by_id[p.id] = p
                if p.oracle:
                    self.tokens_by_oracle.setdefault(p.oracle, []).append(p)
                continue
            key = norm(p.name)
            self.by_name.setdefault(key, []).append(p)
            fkey = norm(p.front)
            if fkey != key:
                aliases.setdefault(fkey, set()).add(key)
            sk = (p.set, p.cn.lower())
            old = self.by_setnum.get(sk)
            if old is None or (old.lang != "en" and p.lang == "en"):
                self.by_setnum[sk] = p
        for fkey, keys in aliases.items():
            if fkey not in self.by_name and len(keys) == 1:
                self.by_name[fkey] = self.by_name[next(iter(keys))]


def _iter_bulk_cards(path, cancel=None):
    opener = gzip.open if path.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if cancel is not None and cancel.is_set():
                return
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def build_index_from_bulk(path, emit=None, cancel=None):
    prints = []
    for n, card in enumerate(_iter_bulk_cards(path, cancel), 1):
        p = make_print(card)
        if p:
            prints.append(p)
        if emit and n % PROGRESS_EVERY == 0:
            emit("status", f"Indexing card data... {n:,} printings read")
    return prints


class BulkManager:
    """Downloads / caches Scryfall bulk data and the compact index built from it."""

    def __init__(self, data_dir, emit, cancel):
        self.dir = data_dir
        self.emit = emit
        self.cancel = cancel
        self.meta_path = os.path.join(data_dir, "bulk_meta.json")

    def _meta(self):
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_meta(self, meta):
        try:
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except OSError:
            pass

    def status_text(self):
        meta = self._meta()
        f = meta.get("file")
        if f and os.path.exists(os.path.join(self.dir, f)):
            return f"Cached card data: {meta.get('updated_at', '?')[:10]} ({f})"
        return "No card data cached yet - it downloads (~80 MB) on the first run."

    def ensure_auto(self, force_check=False):
        meta = self._meta()
        local = os.path.join(self.dir, meta["file"]) if meta.get("file") else None
        if local and not os.path.exists(local):
            local = None
        fresh = time.time() - meta.get("checked_at", 0) < BULK_CHECK_HOURS * 3600
        if local and fresh and not force_check:
            return local
        self.emit("status", "Checking Scryfall for new card data...")
        try:
            info = get_json(API + "/bulk-data/default_cards")
        except (NetError, NotFound, ValueError) as e:
            if local:
                self.emit("issue", "-", "Card data", "Note",
                          f"Couldn't check for newer card data ({e}); using the cached copy.")
                return local
            raise NetError(f"couldn't reach Scryfall to get card data ({e})")
        uri = info.get("jsonl_download_uri") or info.get("download_uri")
        fname = uri.rsplit("/", 1)[-1].split("?")[0]
        dest = os.path.join(self.dir, fname)
        if not os.path.exists(dest):
            size = info.get("compressed_size") or info.get("size") or 0

            def prog(got, total):
                self.emit("progress", got, total or got or 1,
                          f"Downloading card data... {got / 1e6:.0f} / {(total or 0) / 1e6:.0f} MB")

            self.emit("mode", "determinate")
            download_file(uri, dest, size, prog, self.cancel)
            for old in os.listdir(self.dir):            # drop superseded data + indexes
                if old != fname and (old.startswith("default-cards") or old.startswith("idx-")):
                    try:
                        os.remove(os.path.join(self.dir, old))
                    except OSError:
                        pass
        self._save_meta({"file": fname, "updated_at": info.get("updated_at", ""),
                         "checked_at": time.time()})
        return dest

    def load_index(self, raw_path):
        st = os.stat(raw_path)
        sig = f"{os.path.abspath(raw_path)}|{st.st_size}|{int(st.st_mtime)}|{INDEX_FORMAT}"
        cache = os.path.join(self.dir, "idx-" + hashlib.sha1(sig.encode()).hexdigest()[:16] + ".json.gz")
        if os.path.exists(cache):
            self.emit("status", "Loading cached card index...")
            try:
                with gzip.open(cache, "rt", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("format") == INDEX_FORMAT:
                    return CardIndex([Print.from_list(x) for x in data["prints"]], raw_path)
            except (OSError, ValueError, KeyError, EOFError):
                pass
        self.emit("mode", "indeterminate")
        self.emit("status", "Indexing card data (first time only)...")
        prints = build_index_from_bulk(raw_path, self.emit, self.cancel)
        if self.cancel.is_set():
            raise NetError("cancelled")
        try:
            with gzip.open(cache + ".part", "wt", encoding="utf-8", compresslevel=3) as f:
                json.dump({"format": INDEX_FORMAT, "prints": [p.to_list() for p in prints]}, f,
                          separators=(",", ":"))
            os.replace(cache + ".part", cache)
        except OSError:
            pass
        return CardIndex(prints, raw_path)


class ApiSource:
    def __init__(self):
        self.names = {}
        self.setnums = {}
        self.ids = {}
        self.fuzzy_cache = {}

    def _search(self, query):
        url = f"{API}/cards/search?" + urllib.parse.urlencode(
            {"q": query, "unique": "prints", "order": "released", "dir": "desc"})
        cards = []
        for _ in range(20):
            try:
                data = get_json(url)
            except NotFound:
                break
            cards.extend(data.get("data") or [])
            if not data.get("has_more"):
                break
            url = data.get("next_page")
        return cards

    def prints_for_name(self, name):
        key = norm(name)
        if key in self.names:
            return self.names[key]
        q = name.replace('"', "")
        cards = self._search(f'!"{q}" lang:en') or self._search(f'!"{q}" lang:any')
        prints = [p for p in (make_print(c) for c in cards) if p and not p.is_token] or None
        self.names[key] = prints
        return prints

    def by_setnum(self, set_code, number):
        key = (set_code, number)
        if key not in self.setnums:
            try:
                card = get_json(f"{API}/cards/{urllib.parse.quote(set_code)}/{urllib.parse.quote(number)}")
                self.setnums[key] = make_print(card)
            except NotFound:
                self.setnums[key] = None
        return self.setnums[key]

    def by_ids(self, ids):
        todo = [i for i in ids if i not in self.ids]
        for i in range(0, len(todo), COLLECTION_BATCH):
            chunk = todo[i:i + COLLECTION_BATCH]
            data = post_json(f"{API}/cards/collection", {"identifiers": [{"id": x} for x in chunk]})
            for card in data.get("data") or []:
                self.ids[card.get("id")] = make_print(card)
            for x in chunk:
                self.ids.setdefault(x, None)
        return {i: self.ids.get(i) for i in ids}

    def fuzzy(self, name):
        key = norm(name)
        if key not in self.fuzzy_cache:
            try:
                card = get_json(f"{API}/cards/named?" + urllib.parse.urlencode({"fuzzy": name}))
                self.fuzzy_cache[key] = card.get("name")
            except NotFound:
                self.fuzzy_cache[key] = None
        return self.fuzzy_cache[key]


class Source:
    """Bulk index first, live API for anything the index doesn't have."""

    def __init__(self, index, api):
        self.index = index
        self.api = api

    def in_index(self, name):
        return self.index is not None and norm(name) in self.index.by_name

    def setnum_in_index(self, s, n):
        return self.index is not None and (s, n) in self.index.by_setnum

    def prints_for_name(self, name):
        if self.in_index(name):
            return self.index.by_name[norm(name)]
        return self.api.prints_for_name(name)

    def by_setnum(self, s, n):
        if self.setnum_in_index(s, n):
            return self.index.by_setnum[(s, n)]
        return self.api.by_setnum(s, n)


# ===========================================================================
# The job engine
# ===========================================================================


class Options:
    def __init__(self, **kw):
        self.source = "auto"            # auto | api | local
        self.local_bulk = ""
        self.quality = "normal"         # normal | large | png
        self.art_pref = "newest"
        self.name_style = "name"        # name | name_set | set_num
        self.use_filter = True
        self.vary_basics = True
        self.back_faces = True
        self.tokens = True
        self.sort_types = False
        self.copies = False
        self.skip_maybe = True
        self.overwrite = False
        self.__dict__.update(kw)


class Deck:
    def __init__(self, name, entries, out_dir):
        self.name = name
        self.entries = entries
        self.out_dir = out_dir


class DownloadItem:
    __slots__ = ("deck", "label", "url", "path", "print", "face", "attempts", "result")

    def __init__(self, deck, label, url, path, pr, face):
        self.deck, self.label, self.url, self.path, self.print, self.face = deck, label, url, path, pr, face
        self.attempts = 0
        self.result = None


SOFT_KINDS = {"Corrected", "Fallback", "Variety", "Note", "Pinned"}


class Engine:
    def __init__(self, opts, emit, cancel, index_cache, data_dir=None):
        self.o = opts
        self._emit = emit
        self.cancel = cancel
        self.index_cache = index_cache      # dict kept by the app between runs
        self.data_dir = data_dir or app_data_dir()
        self.api = ApiSource()
        self.rng = random.Random()
        self.hard = self.soft = 0

    def emit(self, *ev):
        if ev[0] == "issue":
            if ev[3] in SOFT_KINDS:
                self.soft += 1
            else:
                self.hard += 1
        self._emit(*ev)

    # ---------- generic queue with requeue-on-timeout ----------

    def run_queue(self, tasks, label):
        """tasks: list of (description, fn). Retryable failures go to the back of the
        queue, up to MAX_ATTEMPTS tries. Returns {description: error or None}."""
        q = deque((d, fn, 0) for d, fn in tasks)
        total = len(q)
        done = 0
        errors = {}
        while q and not self.cancel.is_set():
            d, fn, tries = q.popleft()
            self.emit("progress", done, max(total, 1), f"{label} {done + 1}/{total}: {d}")
            try:
                fn()
                done += 1
            except NetError as e:
                tries += 1
                if e.retryable and tries < MAX_ATTEMPTS:
                    q.append((d, fn, tries))
                else:
                    errors[d] = f"{e} (after {tries} attempt{'s' if tries > 1 else ''})"
                    done += 1
        return errors

    # ---------- data source ----------

    def prepare_source(self):
        index = None
        if self.o.source in ("auto", "local"):
            bm = BulkManager(self.data_dir, self.emit, self.cancel)
            try:
                if self.o.source == "local":
                    raw = self.o.local_bulk
                    if not raw or not os.path.exists(raw):
                        raise NetError("the chosen bulk data file doesn't exist")
                else:
                    raw = bm.ensure_auto()
                st = os.stat(raw)
                key = (os.path.abspath(raw), st.st_size, int(st.st_mtime))
                index = self.index_cache.get(key)
                if index is None:
                    index = bm.load_index(raw)
                    self.index_cache.clear()
                    self.index_cache[key] = index
            except (NetError, OSError) as e:
                if self.cancel.is_set():
                    raise
                self.emit("issue", "-", "Card data", "Note",
                          f"Bulk card data unavailable ({e}) - using the live Scryfall API instead.")
        return Source(index, self.api)

    # ---------- resolving one deck ----------

    def resolve_deck(self, deck, src):
        o = self.o
        entries = [e for e in deck.entries if not (o.skip_maybe and e.section == "maybe")]
        entries = [e for e in entries if e.section != "tokens"]

        # Pre-fetch everything the bulk index can't answer, with requeue on timeouts.
        tasks, seen = [], set()
        for e in entries:
            if e.set and e.number and not src.setnum_in_index(e.set, e.number):
                k = ("sn", e.set, e.number)
                if k not in seen:
                    seen.add(k)
                    tasks.append((f"{e.name} ({e.set.upper()} {e.number})",
                                  lambda e=e: self.api.by_setnum(e.set, e.number)))
            if not src.in_index(e.name) and norm(e.name) not in seen:
                seen.add(norm(e.name))
                tasks.append((e.name, lambda e=e: self.api.prints_for_name(e.name)))
        failed = self.run_queue(tasks, f"[{deck.name}] Looking up") if tasks else {}

        selections = []   # (entry, [Print...], canonical_name)
        for e in entries:
            if self.cancel.is_set():
                break
            if e.name in failed:
                self.emit("issue", deck.name, e.name, "Lookup failed", failed[e.name])
                continue
            # exact printing requested
            if e.set and e.number:
                p = None
                try:
                    p = src.by_setnum(e.set, e.number)
                except (NetError, NotFound):
                    p = None
                if p and norm(p.name) in (norm(e.name), norm(p.front)) or (p and norm(e.name) in norm(p.name)):
                    picks = [p] * (e.qty if o.copies else 1)
                    selections.append((e, picks, p.name))
                    continue
                self.emit("issue", deck.name, e.name, "Pinned",
                          f"{e.set.upper()} #{e.number} not found for this card - picked the best printing instead.")

            prints, canon = self._prints_with_correction(deck, e.name, src)
            if not prints:
                continue
            set_pinned = False
            if e.set:
                in_set = [p for p in prints if p.set == e.set]
                if in_set:
                    prints, set_pinned = in_set, True
                elif not e.number:
                    self.emit("issue", deck.name, canon, "Pinned",
                              f"No printing in set {e.set.upper()} - picked the best printing instead.")

            if prints[0].basic and e.qty > 1 and o.vary_basics and not e.number:
                # A set the user chose on purpose may use all of its arts (e.g. ZNR full-art lands).
                picks, distinct = choose_variety(prints, e.qty, o.use_filter and not set_pinned,
                                                 o.art_pref, self.rng)
                if distinct < e.qty:
                    self.emit("issue", deck.name, canon, "Variety",
                              f"Only {distinct} distinct art(s) available for {e.qty} copies - some repeat.")
                if not set_pinned:
                    for p in OrderedDict((x.id, x) for x in picks).values():
                        self._note_fallback(deck, canon, p)
                selections.append((e, picks, canon))
            else:
                best = choose_best(prints, o.use_filter, o.art_pref)
                self._note_fallback(deck, canon, best)
                selections.append((e, [best] * (e.qty if o.copies else 1), canon))
        return selections

    def _prints_with_correction(self, deck, name, src):
        try:
            prints = src.prints_for_name(name)
        except NetError as e:
            self.emit("issue", deck.name, name, "Lookup failed", str(e))
            return None, name
        if prints:
            return prints, prints[0].name
        fixed = None
        for _ in range(MAX_ATTEMPTS):
            try:
                fixed = self.api.fuzzy(name)
                break
            except NetError as e:
                if not e.retryable:
                    break
        if fixed:
            try:
                prints = src.prints_for_name(fixed)
            except NetError:
                prints = None
            if prints:
                self.emit("issue", deck.name, name, "Corrected", f"Read as \"{fixed}\".")
                return prints, fixed
        self.emit("issue", deck.name, name, "Not found", "No card with this name on Scryfall - check the spelling.")
        return None, name

    def _note_fallback(self, deck, name, p):
        label = f"{name} ({p.set_name})"
        if p.lang != "en":
            self.emit("issue", deck.name, label, "Fallback", f"No English printing exists - used {p.lang}.")
        elif p.sb >= 20 or "no image scan" in p.why:
            self.emit("issue", deck.name, label, "Fallback", "Every printing lacks a real scan - used the best available.")
        elif self.o.use_filter and p.sf > 0:
            self.emit("issue", deck.name, label, "Fallback",
                      "No fully normal printing - used the closest: " + "; ".join(p.why))

    # ---------- tokens ----------

    def resolve_tokens(self, deck, selections, src):
        ids = []
        for _, picks, _ in selections:
            for p in picks:
                for t in p.tokens or []:
                    if t not in ids:
                        ids.append(t)
        if not ids:
            return []
        found = {}
        idx = src.index
        missing = []
        for t in ids:
            if idx is not None and t in idx.tokens_by_id:
                found[t] = idx.tokens_by_id[t]
            else:
                missing.append(t)
        if missing:
            holder = {}
            err = self.run_queue([("tokens", lambda: holder.update(self.api.by_ids(missing)))],
                                 f"[{deck.name}] Fetching tokens")
            if err:
                self.emit("issue", deck.name, "Tokens", "Lookup failed", err.get("tokens", ""))
            found.update({k: v for k, v in holder.items() if v})
        chosen = OrderedDict()
        for t in ids:
            p = found.get(t)
            if not p:
                continue
            key = p.oracle or p.id
            if key in chosen:
                continue
            if idx is not None and p.oracle in idx.tokens_by_oracle:
                best = choose_best(idx.tokens_by_oracle[p.oracle], self.o.use_filter, "newest")
                # keep the token printed for this card's own set unless it's actually worse
                if rank_key(best, self.o.use_filter, "newest")[:2] < rank_key(p, self.o.use_filter, "newest")[:2]:
                    p = best
            chosen[key] = p
        return list(chosen.values())

    # ---------- files ----------

    def plan_files(self, deck, selections, tokens):
        o = self.o
        items, used = [], set()
        size = {"normal": "normal", "large": "large", "png": "png"}[o.quality]
        ext = ".png" if size == "png" else ".jpg"

        def unique(folder, base):
            path = os.path.join(folder, safe_filename(base) + ext)
            n = 2
            while path.lower() in used:
                path = os.path.join(folder, f"{safe_filename(base)} ({n}){ext}")
                n += 1
            used.add(path.lower())
            return path

        def styled(p, display):
            if o.name_style == "name_set":
                return f"{display} ({p.set_name})"
            if o.name_style == "set_num":
                return f"{p.set.upper()} {p.cn} - {display}"
            return display

        for e, picks, canon in selections:
            p0 = picks[0]
            folder = deck.out_dir
            if o.sort_types:
                folder = os.path.join(folder, "Commander" if e.section == "commander"
                                      else TYPE_FOLDERS.get(p0.ptype, "Other"))
            varied = len({p.id for p in picks}) > 1
            set_names = [p.set_name for p in picks]
            for i, p in enumerate(picks, 1):
                display = p.front if p.back_img else p.name
                if varied:
                    base = f"{display} ({p.set_name}" + (f" {p.cn})" if set_names.count(p.set_name) > 1 else ")")
                elif len(picks) > 1:
                    base = f"{styled(p, display)} ({i})"
                else:
                    base = styled(p, display)
                items.append(DownloadItem(deck, canon, image_url(p.img, size), unique(folder, base), p, "front"))
                if o.back_faces and p.back_img:
                    back = f"{base} [back - {p.back_name}]"
                    items.append(DownloadItem(deck, f"{canon} (back)", image_url(p.back_img, size),
                                              unique(folder, back), p, "back"))
        tok_folder = os.path.join(deck.out_dir, "Tokens")
        names = [t.name for t in tokens]
        for t in tokens:
            label = t.name if "Emblem" in t.name else f"{t.name} Token"
            if names.count(t.name) > 1 and t.extra:
                label += f" ({t.extra})"
            items.append(DownloadItem(deck, label, image_url(t.img, size), unique(tok_folder, label), t, "front"))
            if o.back_faces and t.back_img:
                items.append(DownloadItem(deck, label + " (back)", image_url(t.back_img, size),
                                          unique(tok_folder, f"{label} [back - {t.back_name}]"), t, "back"))
        return items

    def _fetch(self, it):
        if self.cancel.is_set():
            return "cancelled"
        if os.path.exists(it.path) and not self.o.overwrite:
            return "skipped"
        try:
            data = http_bytes(it.url)
            os.makedirs(os.path.dirname(it.path), exist_ok=True)
            tmp = it.path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, it.path)
            return "ok"
        except NotFound:
            return NetError("image not found on Scryfall (404)")
        except NetError as e:
            return e
        except OSError as e:
            return NetError(f"couldn't save file: {e}")

    def download(self, items):
        pending = deque(items)
        total, done = len(items), 0
        stats = {"ok": 0, "skipped": 0, "failed": 0}
        self.emit("mode", "determinate")
        while pending and not self.cancel.is_set():
            batch = list(pending)
            pending.clear()
            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
                futures = {ex.submit(self._fetch, it): it for it in batch}
                for fut in as_completed(futures):
                    it = futures[fut]
                    res = fut.result()
                    if res == "cancelled":
                        continue
                    if isinstance(res, NetError):
                        it.attempts += 1
                        if res.retryable and it.attempts < MAX_ATTEMPTS:
                            pending.append(it)   # back of the queue
                            continue
                        it.result = "failed"
                        stats["failed"] += 1
                        tries = f" (after {it.attempts} attempts)" if it.attempts > 1 else ""
                        self.emit("issue", it.deck.name, it.label, "Download failed", f"{res}{tries}")
                    else:
                        it.result = res
                        stats[res] += 1
                    done += 1
                    self.emit("progress", done, max(total, 1),
                              f"Downloading images {done}/{total}: {it.label}")
        return stats

    def write_manifest(self, deck, items):
        rows = [it for it in items if it.result in ("ok", "skipped")]
        if not rows:
            return
        path = os.path.join(deck.out_dir, "scrybe_manifest.csv")
        try:
            os.makedirs(deck.out_dir, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["File", "Card", "Face", "Set", "Set name", "Collector #", "Language",
                            "Artist", "Released", "Notes", "Scryfall ID"])
                for it in rows:
                    p = it.print
                    rel = os.path.relpath(it.path, deck.out_dir)
                    w.writerow([rel, it.label, it.face, p.set.upper(), p.set_name, p.cn, p.lang,
                                p.artist, str(p.released), "; ".join(p.why), p.id])
        except OSError as e:
            self.emit("issue", deck.name, "Manifest", "Note", f"Couldn't write manifest: {e}")

    # ---------- whole run ----------

    def run(self, decks):
        t0 = time.time()
        try:
            self.emit("mode", "indeterminate")
            src = self.prepare_source()
            if self.cancel.is_set():
                return self.emit("done", self._summary({}, 0, t0, cancelled=True))
            self.emit("mode", "determinate")
            plan = []
            for deck in decks:
                if self.cancel.is_set():
                    break
                selections = self.resolve_deck(deck, src)
                tokens = self.resolve_tokens(deck, selections, src) if self.o.tokens else []
                plan.append((deck, self.plan_files(deck, selections, tokens)))
            all_items = [it for _, items in plan for it in items]
            stats = self.download(all_items) if not self.cancel.is_set() else {}
            for deck, items in plan:
                self.write_manifest(deck, items)
            self.emit("done", self._summary(stats, len(all_items), t0, self.cancel.is_set()))
        except Exception as e:  # never leave the GUI hanging
            self.emit("issue", "-", "Scrybe", "Error", f"{type(e).__name__}: {e}")
            self.emit("done", self._summary({}, 0, t0, cancelled=self.cancel.is_set()))

    def _summary(self, stats, total, t0, cancelled=False):
        return {"ok": stats.get("ok", 0), "skipped": stats.get("skipped", 0),
                "failed": stats.get("failed", 0), "total": total, "hard": self.hard,
                "soft": self.soft, "cancelled": cancelled, "seconds": time.time() - t0}


# ===========================================================================
# Settings
# ===========================================================================

DEFAULT_SETTINGS = {
    "out_dir": os.path.join(os.path.expanduser("~"), "Pictures", "Scrybe"),
    "source": "auto", "local_bulk": "", "quality": "normal", "art_pref": "newest",
    "name_style": "name", "use_filter": True, "vary_basics": True, "back_faces": True,
    "tokens": True, "sort_types": False, "copies": False, "skip_maybe": True,
    "overwrite": False,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(os.path.join(app_data_dir(), "settings.json"), "r", encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        with open(os.path.join(app_data_dir(), "settings.json"), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


def read_text_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ===========================================================================
# GUI
# ===========================================================================

ICON_PNG_B64 = "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"  # 64px window icon

QUALITY_LABELS = OrderedDict([("normal", "Normal - JPG, 488x680"),
                              ("large", "Large - JPG, 672x936"),
                              ("png", "Highest - PNG, 745x1040")])
ART_LABELS = OrderedDict([("newest", "Newest normal printing"),
                          ("oldest", "Original printing"),
                          ("classic", "Classic old-border look")])
NAME_LABELS = OrderedDict([("name", "Card Name"),
                           ("name_set", "Card Name (Set Name)"),
                           ("set_num", "SET 123 - Card Name")])
DECK_FILETYPES = [("Decklists", "*.txt *.dek *.dck *.mwdeck"), ("All files", "*.*")]


class ScrybeApp:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.index_cache = {}
        self.batch_files = []
        self.archidekt = None
        self.last_out = None
        self.counts = [0, 0]

        root.title(f"{APP_NAME} {__version__} — By {__author__}")
        root.geometry("900x760")
        root.minsize(760, 640)
        self._set_icon()
        self._style()

        v = self.s
        self.v_out = tk.StringVar(value=v["out_dir"])
        self.v_deckname = tk.StringVar()
        self.v_source = tk.StringVar(value=v["source"])
        self.v_local = tk.StringVar(value=v["local_bulk"])
        self.v_quality = tk.StringVar(value=QUALITY_LABELS.get(v["quality"], QUALITY_LABELS["normal"]))
        self.v_art = tk.StringVar(value=ART_LABELS.get(v["art_pref"], ART_LABELS["newest"]))
        self.v_names = tk.StringVar(value=NAME_LABELS.get(v["name_style"], NAME_LABELS["name"]))
        self.bools = {k: tk.BooleanVar(value=bool(v[k])) for k in
                      ("use_filter", "vary_basics", "back_faces", "tokens", "sort_types",
                       "copies", "skip_maybe", "overwrite")}
        self.v_status = tk.StringVar(value="Ready. Paste a decklist or open deck files.")
        self.v_source.trace_add("write", lambda *a: self.lbl_data.config(text=self._data_label())
                                if hasattr(self, "lbl_data") else None)

        self._build()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

    # ---------- look ----------

    def _set_icon(self):
        if ICON_PNG_B64:
            try:
                self._icon = tk.PhotoImage(data=ICON_PNG_B64)
                self.root.iconphoto(True, self._icon)
            except tk.TclError:
                pass

    def _style(self):
        st = ttk.Style(self.root)
        if sys.platform.startswith("win") and "vista" in st.theme_names():
            st.theme_use("vista")
        elif "clam" in st.theme_names():
            st.theme_use("clam")
        st.configure("Go.TButton", font=("Segoe UI", 10, "bold"), padding=(18, 6))
        st.configure("Hint.TLabel", foreground="#666666", font=("Segoe UI", 8))
        st.configure("Credit.TLabel", foreground="#7a6a9a", font=("Segoe UI", 9, "italic"))
        st.configure("Banner.TLabel", foreground="#1d5fa8", font=("Segoe UI", 9, "bold"))

    # ---------- layout ----------

    def _build(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.nb = nb
        self.tab_deck = ttk.Frame(nb)
        self.tab_opts = ttk.Frame(nb)
        self.tab_issues = ttk.Frame(nb)
        nb.add(self.tab_deck, text="  Deck  ")
        nb.add(self.tab_opts, text="  Options  ")
        nb.add(self.tab_issues, text="  Issues  ")
        self._build_deck_tab()
        self._build_options_tab()
        self._build_issues_tab()

    def _build_deck_tab(self):
        t = self.tab_deck
        bar = ttk.Frame(t)
        bar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Button(bar, text="Open deck file(s)…", command=self.open_files).pack(side="left")
        ttk.Button(bar, text="Batch folder…", command=self.open_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="Import Archidekt URL…", command=self.import_url).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear_input).pack(side="right")

        row = ttk.Frame(t)
        row.pack(fill="x", padx=10, pady=4)
        ttk.Label(row, text="Deck name:").pack(side="left")
        ttk.Entry(row, textvariable=self.v_deckname, width=40).pack(side="left", padx=6)
        ttk.Label(row, text="(optional - images go in a subfolder with this name)",
                  style="Hint.TLabel").pack(side="left")

        self.banner = ttk.Label(t, text="", style="Banner.TLabel")
        self.banner.pack(fill="x", padx=10)
        self.text = scrolledtext.ScrolledText(t, height=16, wrap="none", undo=True,
                                              font=("Consolas", 10))
        self.text.pack(fill="both", expand=True, padx=10, pady=(2, 6))

        out = ttk.Frame(t)
        out.pack(fill="x", padx=10, pady=4)
        ttk.Label(out, text="Save to:").pack(side="left")
        ttk.Entry(out, textvariable=self.v_out).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="Browse…", command=self._browse_out).pack(side="left")

        act = ttk.Frame(t)
        act.pack(fill="x", padx=10, pady=(6, 2))
        self.btn_go = ttk.Button(act, text="Download", style="Go.TButton", command=self.start)
        self.btn_go.pack(side="left")
        self.btn_cancel = ttk.Button(act, text="Cancel", command=self.stop, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_open = ttk.Button(act, text="Open output folder", command=self.open_output)
        self.btn_open.pack(side="left")
        ttk.Label(act, text=f"{APP_NAME} {__version__} — By {__author__}",
                  style="Credit.TLabel").pack(side="right")

        self.bar = ttk.Progressbar(t, mode="determinate")
        self.bar.pack(fill="x", padx=10, pady=(6, 2))
        ttk.Label(t, textvariable=self.v_status, anchor="w").pack(fill="x", padx=10, pady=(0, 10))

    def _build_options_tab(self):
        t = self.tab_opts
        t.columnconfigure(0, weight=1)
        t.columnconfigure(1, weight=1)

        data = ttk.LabelFrame(t, text=" Card data ")
        data.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 6))
        for val, text in (("auto", "Automatic - download Scryfall's card data once, keep it up to date (recommended)"),
                          ("api", "Live API only - no big download, slower for large decks"),
                          ("local", "My own bulk data file (.jsonl.gz):")):
            ttk.Radiobutton(data, text=text, value=val, variable=self.v_source).pack(anchor="w", padx=8, pady=1)
        lf = ttk.Frame(data)
        lf.pack(fill="x", padx=28, pady=(0, 4))
        ttk.Entry(lf, textvariable=self.v_local).pack(side="left", fill="x", expand=True)
        ttk.Button(lf, text="Browse…", command=self._browse_bulk).pack(side="left", padx=6)
        df = ttk.Frame(data)
        df.pack(fill="x", padx=8, pady=(0, 8))
        self.lbl_data = ttk.Label(df, text=self._data_label(), style="Hint.TLabel")
        self.lbl_data.pack(side="left")
        ttk.Button(df, text="Check for new card data now", command=self.refresh_data).pack(side="right")

        img = ttk.LabelFrame(t, text=" Images ")
        img.grid(row=1, column=0, sticky="nsew", padx=(10, 5), pady=6)
        ttk.Label(img, text="Quality:").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(img, textvariable=self.v_quality, values=list(QUALITY_LABELS.values()),
                     state="readonly", width=28).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(img, text="Art preference:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(img, textvariable=self.v_art, values=list(ART_LABELS.values()),
                     state="readonly", width=28).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(img, text="File names:").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(img, textvariable=self.v_names, values=list(NAME_LABELS.values()),
                     state="readonly", width=28).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Checkbutton(img, text="Back faces of double-faced cards",
                        variable=self.bools["back_faces"]).grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 1))
        ttk.Checkbutton(img, text="Tokens and emblems the deck makes (Tokens folder)",
                        variable=self.bools["tokens"]).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(1, 8))

        org = ttk.LabelFrame(t, text=" Folders & files ")
        org.grid(row=1, column=1, sticky="nsew", padx=(5, 10), pady=6)
        for key, text in (("sort_types", "Sort into card-type subfolders"),
                          ("copies", "One file per copy (for printing proxies)"),
                          ("skip_maybe", "Skip maybeboard / considering"),
                          ("overwrite", "Replace images that already exist")):
            ttk.Checkbutton(org, text=text, variable=self.bools[key]).pack(anchor="w", padx=8, pady=2)
        ttk.Label(org, text="A scrybe_manifest.csv listing every printing is\nsaved with each deck.",
                  style="Hint.TLabel").pack(anchor="w", padx=8, pady=(4, 8))

        flt = ttk.LabelFrame(t, text=" Printing filter ")
        flt.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=6)
        left = ttk.Frame(flt)
        left.pack(side="left", fill="both", expand=True)
        ttk.Checkbutton(left, text="Normal-card filter", variable=self.bools["use_filter"]).pack(anchor="w", padx=8, pady=(6, 0))
        ttk.Label(left, style="Hint.TLabel", justify="left",
                  text="Skips promos, showcase / borderless / extended art, full art, textless, retro frames,\n"
                       "Secret Lair, Un-sets, digital-only cards and missing scans. English printings always win.\n"
                       "If a card has no clean printing, the closest one is used and listed under Issues.").pack(anchor="w", padx=26)
        ttk.Checkbutton(left, text="Vary basic land art", variable=self.bools["vary_basics"]).pack(anchor="w", padx=8, pady=(8, 0))
        ttk.Label(left, style="Hint.TLabel", justify="left",
                  text="\"16 Forest\" gets 16 random different arts, one set each where possible, saved as\n"
                       "\"Forest (Set Name).jpg\". Write \"16 Forest (ZNR)\" to vary inside one set.").pack(anchor="w", padx=26, pady=(0, 8))
        ttk.Separator(flt, orient="vertical").pack(side="left", fill="y", pady=8)
        ttk.Label(flt, text=f"By {__author__}", style="Credit.TLabel").pack(side="left", padx=16)

        about = ttk.Label(t, style="Hint.TLabel", justify="left",
                          text=f"{APP_NAME} {__version__} — By {__author__}.   Card data and images courtesy of Scryfall.\n"
                               "Unofficial Fan Content - not approved or endorsed by Wizards of the Coast or Scryfall.")
        about.grid(row=3, column=0, columnspan=2, sticky="w", padx=12, pady=(8, 10))

    def _build_issues_tab(self):
        t = self.tab_issues
        top = ttk.Frame(t)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.lbl_issues = ttk.Label(top, text="Nothing yet.")
        self.lbl_issues.pack(side="left")
        ttk.Button(top, text="Save as CSV…", command=self.save_issues).pack(side="right")
        ttk.Button(top, text="Copy", command=self.copy_issues).pack(side="right", padx=6)
        frame = ttk.Frame(t)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ("deck", "card", "kind", "detail")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, w, txt in (("deck", 150, "Deck"), ("card", 210, "Card"), ("kind", 110, "Type"), ("detail", 420, "Detail")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w", stretch=(c == "detail"))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("hard", foreground="#b3261e")
        self.tree.tag_configure("soft", foreground="#555555")

    # ---------- input ----------

    def _set_batch(self, files):
        self.batch_files = files
        self.archidekt = None
        if files:
            self.banner.config(text=f"Batch mode: {len(files)} deck file(s) queued - the text box is ignored. "
                                    "Each deck gets its own subfolder. Click Clear to paste instead.")
            self.text.config(state="disabled", background="#eeeeee")
        else:
            self.banner.config(text="")
            self.text.config(state="normal", background="white")

    def open_files(self):
        files = filedialog.askopenfilenames(title="Open decklist(s)", filetypes=DECK_FILETYPES)
        if not files:
            return
        if len(files) == 1:
            self._set_batch([])
            self.text.delete("1.0", "end")
            self.text.insert("1.0", read_text_file(files[0]))
            self.v_deckname.set(os.path.splitext(os.path.basename(files[0]))[0])
        else:
            self._set_batch(list(files))

    def open_folder(self):
        d = filedialog.askdirectory(title="Folder of decklists")
        if not d:
            return
        files = sorted(os.path.join(d, f) for f in os.listdir(d)
                       if f.lower().endswith((".txt", ".dek", ".dck", ".mwdeck")))
        if not files:
            messagebox.showinfo(APP_NAME, "No .txt, .dek or .dck files in that folder.")
            return
        self._set_batch(files)

    def import_url(self):
        url = simpledialog.askstring(APP_NAME, "Paste a public Archidekt deck link:", parent=self.root)
        if not url:
            return
        if "moxfield" in url.lower():
            messagebox.showinfo(APP_NAME, "Moxfield blocks automated downloads.\n\nOn Moxfield use "
                                          "More → Export → Copy for MTGA (or Plain Text), then paste it here.")
            return
        self.v_status.set("Fetching deck from Archidekt...")

        def work():
            try:
                self.events.put(("imported",) + fetch_archidekt(url))
            except Exception as e:
                self.events.put(("import_failed", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def clear_input(self):
        self._set_batch([])
        self.text.delete("1.0", "end")
        self.v_deckname.set("")

    def _data_label(self):
        if self.v_source.get() == "local":
            return "Using your own bulk data file (its index is cached after the first run)."
        if self.v_source.get() == "api":
            return "Live API mode - no card data is stored on this computer."
        return BulkManager(app_data_dir(), lambda *a: None, self.cancel).status_text()

    def _browse_out(self):
        d = filedialog.askdirectory(title="Save images to")
        if d:
            self.v_out.set(d)

    def _browse_bulk(self):
        f = filedialog.askopenfilename(title="Scryfall bulk data file",
                                       filetypes=[("Scryfall bulk data", "*.jsonl.gz *.json.gz *.jsonl *.json *.gz"),
                                                  ("All files", "*.*")])
        if f:
            self.v_local.set(f)
            self.v_source.set("local")

    # ---------- run ----------

    def _collect_settings(self):
        inv = lambda d, label: next((k for k, v in d.items() if v == label), next(iter(d)))
        s = {"out_dir": self.v_out.get().strip(), "source": self.v_source.get(),
             "local_bulk": self.v_local.get().strip(),
             "quality": inv(QUALITY_LABELS, self.v_quality.get()),
             "art_pref": inv(ART_LABELS, self.v_art.get()),
             "name_style": inv(NAME_LABELS, self.v_names.get())}
        s.update({k: v.get() for k, v in self.bools.items()})
        return s

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        s = self._collect_settings()
        out = s["out_dir"]
        if not out:
            messagebox.showwarning(APP_NAME, "Choose a folder to save images to.")
            return
        decks = []
        self._clear_issues()
        if self.batch_files:
            for path in self.batch_files:
                text = read_text_file(path)
                name = os.path.splitext(os.path.basename(path))[0]
                if not looks_like_decklist(text):
                    self._add_issue("-", name, "Note", "Doesn't look like a decklist - skipped.")
                    continue
                entries = parse_decklist(text)
                if entries:
                    decks.append(Deck(name, entries, os.path.join(out, safe_filename(name))))
        else:
            text = self.text.get("1.0", "end")
            entries = parse_decklist(text)
            if entries:
                name = self.v_deckname.get().strip()
                decks.append(Deck(name or "Pasted list", entries,
                                  os.path.join(out, safe_filename(name)) if name else out))
        if not decks:
            messagebox.showwarning(APP_NAME, "No cards found. Paste a decklist or open deck files.")
            return
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Can't use that folder:\n{e}")
            return
        self.s = s
        save_settings(s)
        self.last_out = decks[0].out_dir if len(decks) == 1 else out
        self.cancel.clear()
        self.btn_go.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.bar.config(mode="determinate", value=0, maximum=1)
        opts = Options(**{k: s[k] for k in s if k != "out_dir"})
        engine = Engine(opts, lambda *ev: self.events.put(ev), self.cancel, self.index_cache)
        n = sum(len(d.entries) for d in decks)
        self.v_status.set(f"Starting - {len(decks)} deck(s), {n} entries...")
        self.worker = threading.Thread(target=engine.run, args=(decks,), daemon=True)
        self.worker.start()

    def stop(self):
        self.cancel.set()
        self.v_status.set("Cancelling - finishing the images already in flight...")

    def refresh_data(self):
        if self.worker and self.worker.is_alive():
            return
        self.btn_go.config(state="disabled")
        self.cancel.clear()

        def work():
            emit = lambda *ev: self.events.put(ev)
            bm = BulkManager(app_data_dir(), emit, self.cancel)
            try:
                emit("mode", "indeterminate")
                raw = bm.ensure_auto(force_check=True)
                bm.load_index(raw)
                self.index_cache.clear()
                emit("data_ready", bm.status_text())
            except Exception as e:
                emit("data_ready", f"Card data update failed: {e}")
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def open_output(self):
        path = self.last_out or self.v_out.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showinfo(APP_NAME, "The output folder doesn't exist yet.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
        except OSError as e:
            messagebox.showerror(APP_NAME, str(e))

    # ---------- issues ----------

    def _clear_issues(self):
        self.tree.delete(*self.tree.get_children())
        self.counts = [0, 0]
        self._update_issue_label()

    def _add_issue(self, deck, card, kind, detail):
        soft = kind in SOFT_KINDS
        self.counts[1 if soft else 0] += 1
        self.tree.insert("", "end", values=(deck, card, kind, detail), tags=("soft" if soft else "hard",))
        self._update_issue_label()

    def _update_issue_label(self):
        hard, soft = self.counts
        self.nb.tab(self.tab_issues, text=f"  Issues ({hard}) · Notes ({soft})  " if hard or soft else "  Issues  ")
        self.lbl_issues.config(text=(f"{hard} problem(s) in red, {soft} note(s) in grey "
                                     "(fallback printings, corrected names, repeats)." if hard or soft
                                     else "No issues."))

    def _issue_rows(self):
        return [self.tree.item(i, "values") for i in self.tree.get_children()]

    def copy_issues(self):
        rows = self._issue_rows()
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join("\t".join(r) for r in rows))

    def save_issues(self):
        rows = self._issue_rows()
        if not rows:
            return
        f = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                         initialfile="scrybe_issues.csv")
        if f:
            with open(f, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["Deck", "Card", "Type", "Detail"])
                w.writerows(rows)

    # ---------- event pump ----------

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self.v_status.set(ev[1])
                elif kind == "mode":
                    if ev[1] == "indeterminate":
                        self.bar.config(mode="indeterminate")
                        self.bar.start(12)
                    else:
                        self.bar.stop()
                        self.bar.config(mode="determinate")
                elif kind == "progress":
                    if str(self.bar.cget("mode")) != "determinate":
                        self.bar.stop()
                        self.bar.config(mode="determinate")
                    self.bar.config(maximum=ev[2], value=ev[1])
                    self.v_status.set(ev[3])
                elif kind == "issue":
                    self._add_issue(*ev[1:5])
                elif kind == "done":
                    self._finish(ev[1])
                elif kind == "data_ready":
                    self.bar.stop()
                    self.bar.config(mode="determinate", value=0)
                    self.lbl_data.config(text=ev[1])
                    self.v_status.set(ev[1])
                    self.btn_go.config(state="normal")
                elif kind == "imported":
                    self._imported(*ev[1:])
                elif kind == "import_failed":
                    self.v_status.set("Import failed.")
                    messagebox.showerror(APP_NAME, f"Couldn't import that deck:\n{ev[1]}")
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _imported(self, name, pinned, plain):
        keep = messagebox.askyesno(APP_NAME, f"Imported \"{name}\".\n\nKeep the exact printings chosen on "
                                              "Archidekt?\n\nNo = let Scrybe pick the best normal printing.")
        self._set_batch([])
        self.text.delete("1.0", "end")
        self.text.insert("1.0", pinned if keep else plain)
        self.v_deckname.set(name)
        self.v_status.set(f"Imported {name}. Press Download when ready.")

    def _finish(self, r):
        self.bar.stop()
        self.bar.config(mode="determinate", maximum=max(r["total"], 1), value=r["ok"] + r["skipped"] + r["failed"])
        self.btn_go.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.lbl_data.config(text=self._data_label())
        head = "Cancelled" if r["cancelled"] else "Done"
        msg = f"{head} in {r['seconds']:.0f}s - {r['ok']} saved"
        if r["skipped"]:
            msg += f", {r['skipped']} already existed"
        if r["failed"]:
            msg += f", {r['failed']} failed"
        hard, soft = self.counts
        if hard or soft:
            msg += f". {hard} problem(s), {soft} note(s) - see Issues."
        self.v_status.set(msg)
        if hard:
            self.nb.select(self.tab_issues)

    def _on_close(self):
        save_settings(self._collect_settings())
        self.cancel.set()
        self.root.destroy()


def main():
    if tk is None:
        print("Tkinter isn't available in this Python installation.")
        sys.exit(1)
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Adromedary.Scrybe")
        except Exception:
            pass
    root = tk.Tk()
    ScrybeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
