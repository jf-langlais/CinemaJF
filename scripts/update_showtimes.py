#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
META_PATH = ROOT / "metadata.json"
TZ = ZoneInfo("America/Toronto")
BASE = "https://www.cinemaclock.com"
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
    html = get(cinema["clock"]).text
    soup = BeautifulSoup(html, "html.parser")
    variants = []
    for h3 in soup.find_all("h3"):
        link = h3.find("a", href=True)
        if not link:
            continue
        raw_title = h3.get_text(" ", strip=True)
        if not raw_title or raw_title.lower().startswith(("dimanche le", "lundi le", "mardi le", "mercredi le", "jeudi le", "vendredi le", "samedi le")):
            continue
        detail_url = urljoin(BASE, link["href"])
        before_h4 = []
        h4_blocks = []
        node = h3.next_sibling
        current_h4 = None
        current_nodes = []
        while node:
            if isinstance(node, Tag) and node.name == "h3":
                break
            if isinstance(node, Tag) and node.name == "h4":
                if current_h4 is not None:
                    h4_blocks.append((current_h4, current_nodes))
                current_h4 = node
                current_nodes = []
            elif isinstance(node, Tag):
                if current_h4 is None:
                    before_h4.append(node)
                else:
                    current_nodes.append(node)
            node = node.next_sibling
        if current_h4 is not None:
            h4_blocks.append((current_h4, current_nodes))
        meta_text = text_of(before_h4)
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
        for h4, nodes in h4_blocks:
            venue = h4.get_text(" ", strip=True)
            # Ignore blocks for a different venue if CinemaClock ever injects related locations.
            if cinema["name"].split()[0].lower() not in venue.lower() and "clap" not in venue.lower() and "cartier" not in venue.lower() and "cineplex" not in venue.lower():
                continue
            block = text_of(nodes)
            fmt = classify_format(block)
            dates = parse_showtimes(block, now)
            if not dates:
                continue
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


def imdb_rating(url: str) -> float | None:
    try:
        r = get(url, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                obj = json.loads(tag.string or "{}")
            except Exception:
                continue
            objs = obj if isinstance(obj, list) else [obj]
            for item in objs:
                if isinstance(item, dict):
                    rating = (item.get("aggregateRating") or {}).get("ratingValue")
                    if rating is not None:
                        return round(float(rating), 1)
    except Exception:
        pass
    return None


def enrich_metadata(groups: dict, meta: dict, now: datetime) -> dict:
    today = now.date().isoformat()
    for key, g in groups.items():
        rec = meta.get(key, {})
        if (not rec.get("poster") or not rec.get("imdbUrl")) and g.get("detail_url"):
            rec.update({k: v for k, v in detail_metadata(g["detail_url"]).items() if v})
        if rec.get("imdbUrl") and rec.get("ratingChecked") != today:
            rating = imdb_rating(rec["imdbUrl"])
            if rating is not None:
                rec["imdbRating"] = rating
            rec["ratingChecked"] = today
        rec.setdefault("poster", "")
        rec.setdefault("imdbUrl", "")
        rec.setdefault("imdbRating", None)
        meta[key] = rec
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
                movies[key] = {
                    "title": v["translation"] or v["title"],
                    "original": v["original"] or v["title"],
                    "translation": v["translation"] or "",
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
        g = groups.setdefault(v["key"], {"detail_url": v.get("detail_url")})
        if not g.get("detail_url"):
            g["detail_url"] = v.get("detail_url")
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