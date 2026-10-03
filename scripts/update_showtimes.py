#!/usr/bin/env python3
from __future__ import annotations

import gzip
import io
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup, Tag, NavigableString
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
META_PATH = ROOT / "metadata.json"
POSTER_DIR = ROOT / "posters"
TZ = ZoneInfo("America/Toronto")
BASE = "https://www.cinemaclock.com"
IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
IMDB_RATING_SOURCE = "official-dataset-v1"
IMDB_SUGGEST = "https://v3.sg.media-imdb.com/suggestion/x/{query}.json"
TITLE_TRANSLATION_OVERRIDES = {
    "spider man brand new day": "Spider-Man : Un jour nouveau",
}

POSTER_OVERRIDES = {
    "a pied d oeuvre": "https://cinehorizons.net/sites/default/files/affiches/1823285106-pied-doeuvre.jpg",
    "les aventuriers voyageurs reve d afrique": "https://img2.cdn.bizzmedia.ca/media/3Lfoq1cMctXNSXNWkobeQz8PG93iLiMmd4fsr5Vs.jpg/400/584",
    "toy story 5": "https://cdn.teater.co/imgs/toy-story-5-2026_600_880.webp",
    "spider man brand new day": "https://www.newdvdreleasedates.com/images/posters/large/spider-man-brand-new-day-2026.jpg",
    "the stunt driver": "https://www.impawards.com/intl/canada/2026/posters/stunt_driver_xlg.jpg",
    "la bataille de gaulle liberte": "https://www.impawards.com/intl/france/2026/posters/la_bataille_de_gaulle_jecris_ton_nom.jpg",
    "la bataille de gaulle resistance": "https://img1.cdn.bizzmedia.ca/media/7s44vlWhabjxyx9FyoVV4KYlIYpyB7jQ9fpl9o2l.jpg/400/584",
}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/153 Safari/537.36 CinemaJF/1.0"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.6"})

CINEMAS = [
    {
        "id": "cineplex-sainte-foy",
        "name": "Cineplex Ste-Foy",
        "short": "Cineplex Ste-Foy",
        "clock": f"{BASE}/cinemas/cineplex-odeon-sainte-foy",
        "source": "https://www.cineplex.com/theatre/cinema-cineplex-odeon-saintefoy?openTM=true",
    },
    {
        "id": "cineplex-beauport",
        "name": "Cineplex Beauport",
        "short": "Cineplex Beauport",
        "clock": f"{BASE}/cinemas/cineplex-odeon-beauport",
        "source": "https://www.cineplex.com/fr/theatre/Cinema-Cineplex-Odeon-Beauport",
    },
    {
        "id": "cineplex-imax",
        "name": "Cineplex IMAX",
        "short": "Cineplex IMAX",
        "clock": f"{BASE}/cinemas/cineplex-imax-aux-galeries-de-la-capitale",
        "source": "https://www.cineplex.com/fr/theatre/cinema-cineplex-imax-aux-galeries-de-la-capitale",
    },
    {
        "id": "clap-ste-foy",
        "name": "Le Clap Place Sainte-Foy",
        "short": "Clap Ste-Foy",
        "clock": f"{BASE}/cinemas/le-clap-place-ste-foy",
        "source": "https://clap.ca/",
    },
    {
        "id": "clap-loretteville",
        "name": "Le Clap Loretteville",
        "short": "Clap Loretteville",
        "clock": f"{BASE}/cinemas/le-clap-loretteville",
        "source": "https://clap.ca/",
    },
    {
        "id": "cinema-cartier",
        "name": "Cinéma Cartier",
        "short": "Cinéma Cartier",
        "clock": f"{BASE}/cinemas/cinema-cartier",
        "source": "https://www.cinemacartier.com/",
    },
]

MONTHS = {
    "jan": 1, "janv": 1, "fev": 2, "fevr": 2, "fév": 2, "févr": 2,
    "mar": 3, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7,
    "aou": 8, "aout": 8, "aoû": 8, "août": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12, "déc": 12,
}
DATE_START = re.compile(r"(?i)(Aujourd'hui|Lun|Mar|Mer|Jeu|Ven|Sam|Dim)\s+(\d{1,2})\s+([A-Za-zÀ-ÿ.]+)")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})(am|pm)?\b", re.I)
WEEK_RE = re.compile(r"\b1(?:e|er|re)\s*sem\.?\b", re.I)
RUNTIME_RE = re.compile(r"\b(\d)h(\d{2})m\b")
QUOTE_RE = re.compile(r"«\s*(.*?)\s*»")
IMDB_RE = re.compile(r"https?://(?:www\.)?imdb\.com/title/(tt\d+)", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def clean_title(title: str) -> str:
    title = re.sub(r"\s+v\.f\.?$", "", title, flags=re.I).strip()
    return title


def get(url: str, timeout: int = 25) -> requests.Response:
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def get_rendered_html(url: str) -> str:
    # Fast path: many CinemaClock theatre pages are fully rendered server-side.
    html = get(url).text
    soup = BeautifulSoup(html, "html.parser")
    if len(soup.find_all("h3")) > 2:
        return html
    # Some theatres are returned as an AJAX shell. Render those in Chromium.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, locale="fr-CA")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_function("document.querySelectorAll('h3').length > 2", timeout=15000)
        except Exception:
            page.wait_for_timeout(3500)
        rendered = page.content()
        browser.close()
        return rendered


def blocks_after_h3(h3: Tag):
    meta_parts = []
    blocks = []
    current = None
    for el in h3.next_elements:
        if el is h3:
            continue
        if isinstance(el, Tag) and el.name == "h3":
            break
        if isinstance(el, Tag) and el.name == "h4":
            current = {"heading": el.get_text(" ", strip=True), "parts": []}
            blocks.append(current)
            continue
        if isinstance(el, NavigableString):
            parent = el.parent
            if not parent or parent.name in ("script", "style", "noscript", "h3", "h4"):
                continue
            t = str(el).strip()
            if not t:
                continue
            if current is None:
                meta_parts.append(t)
            else:
                current["parts"].append(t)
    return " ".join(meta_parts), [(b["heading"], " ".join(b["parts"])) for b in blocks]


def iter_between(start: Tag, stop_names=("h3",)):
    node = start.next_sibling
    while node:
        if isinstance(node, Tag) and node.name in stop_names:
            break
        if isinstance(node, Tag):
            yield node
        node = node.next_sibling


def text_of(nodes) -> str:
    parts = []
    for n in nodes:
        t = n.get_text(" ", strip=True)
        if t:
            parts.append(t)
    return " ".join(parts)


def month_number(raw: str) -> int | None:
    k = norm(raw).replace(" ", "")[:4]
    for name, n in MONTHS.items():
        if k.startswith(norm(name)[:3]):
            return n
    return None


def resolve_year(month: int, day: int, now: datetime) -> int:
    candidates = []
    for y in (now.year - 1, now.year, now.year + 1):
        try:
            dt = datetime(y, month, day, tzinfo=TZ)
            candidates.append((abs((dt - now).days), y))
        except ValueError:
            pass
    return min(candidates)[1]


def parse_showtimes(block_text: str, now: datetime) -> dict[str, list[str]]:
    matches = list(DATE_START.finditer(block_text))
    out: dict[str, list[str]] = {}
    for i, m in enumerate(matches):
        day = int(m.group(2))
        month = month_number(m.group(3))
        if not month:
            continue
        year = resolve_year(month, day, now)
        date_key = f"{year:04d}-{month:02d}-{day:02d}"
        end = matches[i + 1].start() if i + 1 < len(matches) else len(block_text)
        segment = block_text[m.end():end]
        times = []
        for tm in TIME_RE.finditer(segment):
            hh, mm = map(int, tm.group(1).split(":"))
            suffix = (tm.group(2) or "").lower()
            if suffix == "pm" and hh < 12:
                hh += 12
            if suffix == "am" and hh == 12:
                hh = 0
            times.append(f"{hh:02d}:{mm:02d}")
        if times:
            out[date_key] = sorted(set(times))
    return out


def classify_format(text: str) -> str:
    n = norm(text)
    tags = []
    checks = [
        ("UltraAVX", "ultraavx"), ("IMAX", "imax"), ("ScreenX", "screenx"),
        ("3D", "3d"), ("Dolby Atmos", "dolby atmos"), ("D-BOX", "d box"),
        ("VIP", "vip"), ("Parent-enfant", "parent enfant"),
    ]
    for label, key in checks:
        if key in n:
            tags.append(label)
    return " · ".join(tags) if tags else "Standard"


def classify_language(title: str, meta_text: str) -> str:
    n = norm(meta_text)
    raw = title.strip()
    if re.search(r"\bv\.f\.?$", raw, re.I):
        return "VF"
    if "anglais" in n or "version originale anglaise" in n:
        return "VOA"
    if "francais" in n or "version originale francaise" in n:
        return "VOF"
    quote = QUOTE_RE.search(meta_text)
    if quote and " en coreen" not in n and " en japonais" not in n and " en italien" not in n and " en espagnol" not in n:
        if norm(quote.group(1)) != norm(clean_title(title)):
            return "VF"
    # CinemaClock's French pages generally omit a language marker for francophone originals.
    if not quote:
        return "VOF"
    return "VO"


def parse_theatre(cinema: dict, now: datetime) -> list[dict]:
    html = get_rendered_html(cinema["clock"])
    soup = BeautifulSoup(html, "html.parser")
    variants = []
    for h3 in soup.find_all("h3"):
        raw_title = h3.get_text(" ", strip=True)
        if not raw_title or raw_title.lower().startswith(("dimanche le", "lundi le", "mardi le", "mercredi le", "jeudi le", "vendredi le", "samedi le")):
            continue
        link = h3.find("a", href=True)
        detail_url = urljoin(BASE, link["href"]) if link else cinema["clock"]
        meta_text, h4_blocks = blocks_after_h3(h3)
        if not h4_blocks:
            continue
        quote = QUOTE_RE.search(meta_text)
        original_hint = quote.group(1).strip() if quote else ""
        title = clean_title(raw_title)
        lang = classify_language(raw_title, meta_text)
        if lang == "VF" and original_hint:
            original = original_hint
            translation = title
        elif lang == "VOA":
            original = title
            translation = ""
        elif lang == "VOF":
            original = title
            translation = ""
        else:
            original = original_hint or title
            translation = "" if norm(original) == norm(title) else title
        runtime = None
        rm = RUNTIME_RE.search(meta_text)
        if rm:
            runtime = f"{int(rm.group(1))} h {rm.group(2)}"
        is_new = bool(WEEK_RE.search(meta_text))
        for venue, block in h4_blocks:
            dates = parse_showtimes(block, now)
            if not dates:
                continue
            fmt = classify_format(block)
            variants.append({
                "key": norm(original), "title": title, "original": original, "translation": translation,
                "lang": lang, "runtime": runtime, "genre": "", "is_new": is_new,
                "detail_url": detail_url, "cinema": cinema["id"], "format": fmt,
                "dates": dates,
            })
    if not variants:
        title = soup.title.get_text(" ", strip=True) if soup.title else "(no title)"
        sample = soup.get_text(" ", strip=True)[:2500]
        raise RuntimeError(f"No movie blocks parsed from {cinema['clock']} | title={title!r} | h3={len(soup.find_all('h3'))} h4={len(soup.find_all('h4'))} | sample={sample!r}")
    return variants


def load_metadata() -> dict:
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def detail_metadata(url: str) -> dict:
    result = {}
    try:
        soup = BeautifulSoup(get(url).text, "html.parser")
        og = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"})
        if og and og.get("content"):
            result["poster"] = urljoin(BASE, og["content"])
        imdb = soup.find("a", href=IMDB_RE)
        if imdb:
            m = IMDB_RE.search(imdb.get("href", ""))
            if m:
                result["imdbUrl"] = f"https://www.imdb.com/title/{m.group(1)}/"
    except Exception:
        pass
    return result


def imdb_poster(url: str) -> str:
    """Fallback: return IMDb's primary image URL from a title page."""
    try:
        soup = BeautifulSoup(get(url, timeout=25).text, "html.parser")
        tag = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"})
        if tag and tag.get("content"):
            return tag["content"].strip()
    except Exception as e:
        print(f"WARNING IMDb poster unavailable for {url}: {e}", file=sys.stderr)
    return ""


def bad_poster(url: str) -> bool:
    u = (url or "").lower()
    return (not u or "logo-256x256-alpha" in u or "placeholder" in u or "default-poster" in u)


def poster_filename(key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", norm(key)).strip("-")[:80] or "film"
    return f"{slug}.jpg"


def imdb_suggestion(title: str, now: datetime) -> dict:
    """Use IMDb's autocomplete service to discover a title id and poster."""
    if not title:
        return {}
    try:
        url = IMDB_SUGGEST.format(query=quote(title.lower()))
        payload = get(url, timeout=25).json()
        candidates = []
        target = norm(title)
        for item in payload.get("d", []):
            tid = str(item.get("id", ""))
            label = str(item.get("l", ""))
            if not tid.startswith("tt") or not label:
                continue
            import difflib
            score = difflib.SequenceMatcher(None, target, norm(label)).ratio()
            year = item.get("y")
            # Prefer exact/near-exact title matches; a plausible current year is a small bonus.
            if year and isinstance(year, int) and abs(year - now.year) <= 2:
                score += 0.08
            candidates.append((score, item))
        if not candidates:
            return {}
        score, item = max(candidates, key=lambda x: x[0])
        if score < 0.72:
            return {}
        image = (item.get("i") or {}).get("imageUrl", "")
        return {
            "imdbUrl": f"https://www.imdb.com/title/{item['id']}/",
            "poster": image,
            "matchScore": round(score, 3),
        }
    except Exception as e:
        print(f"WARNING IMDb suggestion failed for {title!r}: {e}", file=sys.stderr)
        return {}


def cache_poster(key: str, candidates: list[str]) -> str:
    """Validate a remote poster, normalize it to a small JPEG and keep it in this repo."""
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    dest = POSTER_DIR / poster_filename(key)
    if dest.exists() and dest.stat().st_size > 5000:
        return dest.relative_to(ROOT).as_posix()

    for url in candidates:
        if bad_poster(url):
            continue
        try:
            r = SESSION.get(url, timeout=35)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").lower()
            if "image" not in ctype and len(r.content) < 8000:
                continue
            img = Image.open(io.BytesIO(r.content))
            img.load()
            w, h = img.size
            if w < 120 or h < 160 or h / max(w, 1) < 1.1:
                continue
            img = ImageOps.exif_transpose(img).convert("RGB")
            img = ImageOps.fit(img, (240, 360), method=Image.Resampling.LANCZOS)
            img.save(dest, "JPEG", quality=84, optimize=True)
            print(f"POSTER cached {key}: {url} -> {dest.name}")
            return dest.relative_to(ROOT).as_posix()
        except Exception as e:
            print(f"WARNING poster candidate failed for {key}: {url} ({e})", file=sys.stderr)
    return ""


def imdb_id(url: str) -> str:
    m = IMDB_RE.search(url or "")
    return m.group(1) if m else ""


def alias_metadata(key: str, meta: dict) -> dict:
    """Reuse a known IMDb mapping for a very close title alias (e.g. Endgame / Endgame Encore)."""
    urls = {}
    for other, rec in meta.items():
        if other == key or not isinstance(rec, dict) or not rec.get("imdbUrl"):
            continue
        if other.startswith(key + " ") or key.startswith(other + " "):
            urls.setdefault(rec["imdbUrl"], rec)
    return next(iter(urls.values())) if len(urls) == 1 else {}


def load_imdb_ratings(ids: set[str]) -> dict[str, tuple[float, int]]:
    """Load ratings from IMDb's official non-commercial daily dataset."""
    if not ids:
        return {}
    r = SESSION.get(IMDB_RATINGS_URL, timeout=120)
    r.raise_for_status()
    found: dict[str, tuple[float, int]] = {}
    with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
        with io.TextIOWrapper(gz, encoding="utf-8") as rows:
            header = next(rows, None)
            for line in rows:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                tconst, avg, votes = parts
                if tconst in ids:
                    try:
                        found[tconst] = (round(float(avg), 1), int(votes))
                    except ValueError:
                        pass
                    if len(found) == len(ids):
                        break
    return found


def enrich_metadata(groups: dict, meta: dict, now: datetime) -> dict:
    today = now.date().isoformat()

    # Resolve metadata, discover missing IMDb ids/posters, then cache posters locally.
    for key, g in groups.items():
        rec = dict(meta.get(key, {}))
        detail = {}
        if g.get("detail_url"):
            detail = detail_metadata(g["detail_url"])
            if not rec.get("imdbUrl") and detail.get("imdbUrl"):
                rec["imdbUrl"] = detail["imdbUrl"]

        if not rec.get("imdbUrl"):
            alias = alias_metadata(key, meta)
            if alias.get("imdbUrl"):
                rec["imdbUrl"] = alias["imdbUrl"]

        suggestion = {}
        if not rec.get("imdbUrl") or not str(rec.get("poster", "")).startswith("posters/"):
            suggestion = imdb_suggestion(g.get("title") or key, now)
            if not rec.get("imdbUrl") and suggestion.get("imdbUrl"):
                rec["imdbUrl"] = suggestion["imdbUrl"]
                rec["imdbMatchScore"] = suggestion.get("matchScore")

        rec.setdefault("poster", "")
        rec.setdefault("imdbUrl", "")
        rec.setdefault("imdbRating", None)

        local = rec.get("poster", "")
        if local.startswith("posters/") and (ROOT / local).exists():
            rec["posterSource"] = "local-cache"
        else:
            candidates = []
            if key in POSTER_OVERRIDES:
                candidates.append(POSTER_OVERRIDES[key])
            if suggestion.get("poster"):
                candidates.append(suggestion["poster"])
            if rec.get("poster") and not rec["poster"].startswith("posters/"):
                candidates.append(rec["poster"])
            if detail.get("poster"):
                candidates.append(detail["poster"])
            if rec.get("imdbUrl"):
                p = imdb_poster(rec["imdbUrl"])
                if p:
                    candidates.append(p)

            cached = cache_poster(key, list(dict.fromkeys(candidates)))
            if cached:
                rec["poster"] = cached
                rec["posterSource"] = "local-cache"
                rec["posterChecked"] = today
            elif bad_poster(rec.get("poster", "")):
                rec["poster"] = ""
                rec["posterSource"] = "missing"
                rec["posterChecked"] = today

        meta[key] = rec

    # IMDb publishes ratings as a daily non-commercial dataset.
    pending: dict[str, list[str]] = defaultdict(list)
    for key in groups:
        rec = meta[key]
        tconst = imdb_id(rec.get("imdbUrl", ""))
        if tconst and (rec.get("ratingChecked") != today or rec.get("ratingSource") != IMDB_RATING_SOURCE):
            pending[tconst].append(key)

    if pending:
        try:
            ratings = load_imdb_ratings(set(pending))
            print(f"IMDb dataset: {len(ratings)}/{len(pending)} ratings matched")
            for tconst, keys in pending.items():
                entry = ratings.get(tconst)
                for key in keys:
                    rec = meta[key]
                    rec["imdbRating"] = entry[0] if entry else None
                    rec["imdbVotes"] = entry[1] if entry else None
                    rec["ratingChecked"] = today
                    rec["ratingSource"] = IMDB_RATING_SOURCE
        except Exception as e:
            print(f"WARNING IMDb ratings dataset unavailable: {e}", file=sys.stderr)

    return meta


def prior_for_failed(date_key: str, failed_ids: set[str]) -> list[dict]:
    p = DATA_DIR / f"{date_key}.json"
    if not p.exists() or not failed_ids:
        return []
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    kept = []
    for movie in old.get("movies", []):
        sh = [s for s in movie.get("showings", []) if s.get("cinema") in failed_ids]
        if sh:
            cp = {k: v for k, v in movie.items() if k != "showings"}
            cp["showings"] = sh
            kept.append(cp)
    return kept


def merge_movie(dst: dict, src: dict):
    dst["showings"].extend(src.get("showings", []))
    for field in ("poster", "imdbUrl", "imdbRating", "runtime", "genre", "badge", "translation", "original"):
        if not dst.get(field) and src.get(field):
            dst[field] = src[field]


def build_files(variants: list[dict], failed_ids: set[str], meta: dict, now: datetime):
    dates = [(now.date() + timedelta(days=i)).isoformat() for i in range(7)]
    cinema_public = [{k: c[k] for k in ("id", "name", "short", "source")} for c in CINEMAS]
    for date_key in dates:
        movies = {}
        for v in variants:
            times = v["dates"].get(date_key)
            if not times:
                continue
            key = v["key"] or norm(v["title"])
            if key not in movies:
                m = meta.get(key, {})
                translation = TITLE_TRANSLATION_OVERRIDES.get(key, v["translation"] or "")
                movies[key] = {
                    "title": translation or v["title"],
                    "original": v["original"] or v["title"],
                    "translation": translation,
                    "runtime": v.get("runtime") or "",
                    "genre": v.get("genre") or "",
                    "badge": "Nouveauté" if v.get("is_new") else "",
                    "poster": m.get("poster", ""),
                    "imdbRating": m.get("imdbRating"),
                    "imdbUrl": m.get("imdbUrl", ""),
                    "showings": [],
                }
            movies[key]["showings"].append({
                "cinema": v["cinema"], "lang": v["lang"], "format": v["format"], "times": times
            })
        # Keep last known data only for cinemas that failed this run.
        for old_movie in prior_for_failed(date_key, failed_ids):
            key = norm(old_movie.get("original") or old_movie.get("title"))
            if key in movies:
                merge_movie(movies[key], old_movie)
            else:
                movies[key] = old_movie
        # deduplicate showings
        for m in movies.values():
            merged = {}
            for sh in m["showings"]:
                k = (sh.get("cinema"), sh.get("lang"), sh.get("format") or "Standard")
                merged.setdefault(k, set()).update(sh.get("times", []))
            m["showings"] = [
                {"cinema": k[0], "lang": k[1], "format": k[2], "times": sorted(v)}
                for k, v in merged.items()
            ]
        dt = datetime.fromisoformat(date_key).replace(tzinfo=TZ)
        date_label = dt.strftime("%Y-%m-%d")
        payload = {
            "date": date_key,
            "dateLabel": date_label,
            "displayDate": date_label,
            "generatedAt": now.isoformat(timespec="minutes"),
            "updatedLabel": now.strftime("%H h %M"),
            "location": "Québec, QC",
            "cinemas": cinema_public,
            "movies": sorted(movies.values(), key=lambda x: norm(x.get("original") or x.get("title"))),
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / f"{date_key}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    now = datetime.now(TZ)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_variants = []
    errors = {}
    success = []
    for cinema in CINEMAS:
        try:
            variants = parse_theatre(cinema, now)
            all_variants.extend(variants)
            success.append(cinema["id"])
            print(f"OK {cinema['name']}: {len(variants)} variants")
        except Exception as e:
            errors[cinema["id"]] = str(e)
            print(f"ERROR {cinema['name']}: {e}", file=sys.stderr)
    if not success:
        print("All sources failed; preserving existing files.", file=sys.stderr)
        return 2
    groups = {}
    for v in all_variants:
        g = groups.setdefault(v["key"], {"detail_url": v.get("detail_url"), "title": v.get("original") or v.get("title")})
        if not g.get("detail_url"):
            g["detail_url"] = v.get("detail_url")
        if not g.get("title"):
            g["title"] = v.get("original") or v.get("title")
    meta = enrich_metadata(groups, load_metadata(), now)
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    failed = {c["id"] for c in CINEMAS if c["id"] not in success}
    build_files(all_variants, failed, meta, now)
    status = {
        "generatedAt": now.isoformat(timespec="seconds"),
        "successfulSources": success,
        "failedSources": errors,
        "source": "CinemaClock showtime listings; official cinema links used for ticket navigation",
    }
    (DATA_DIR / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())