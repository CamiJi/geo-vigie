#!/usr/bin/env python3
"""geo-vigie — collecte les offres, filtre par mots-clés, dédup, stocke.

Usage :
    python collect.py            # relevé complet
    python collect.py --check    # ne fait que tester la joignabilité des sources
    python collect.py --dry      # n'écrit rien en base, affiche ce qui serait gardé
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

from common import (
    clean_html,
    db,
    end_run,
    fingerprint,
    keyword_match,
    load_config,
    log_run,
    now,
    slug,
)

UA = "geo-vigie/0.1 (releve d'offres d'emploi personnel; contact: voir README)"
TIMEOUT = 20


# ── Lecture des réponses ─────────────────────────────────────────────

def dig(item: dict, dotted: str):
    """Champ imbriqué ``a.b.c`` dans un dict, sinon chaîne vide."""
    if not dotted:
        return ""
    value = item
    for part in dotted.split("."):
        if isinstance(value, dict):
            value = value.get(part, "")
        elif isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if index < len(value) else ""
        else:
            return ""
    if isinstance(value, (dict, list)):
        return value
    return value or ""


def fetch_json(source: dict, config: dict) -> list[dict]:
    resp = requests.get(source["url"], headers={"User-Agent": UA},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    items = payload
    if source.get("items_path"):
        for part in source["items_path"].split("."):
            items = items.get(part, []) if isinstance(items, dict) else []

    fields = source.get("fields", {})
    out = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        out.append({key: clean_html(str(dig(raw, path)))
                    for key, path in fields.items() if path})
    return out


def fetch_next_json(source: dict, config: dict) -> list[dict]:
    """Parse les résultats Reed exposés dans ``#__NEXT_DATA__``."""
    resp = requests.get(source["url"], headers={"User-Agent": UA},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    if not script or not script.string and not script.get_text(strip=True):
        raise ValueError("script Next.js __NEXT_DATA__ introuvable")
    payload = json.loads(script.string or script.get_text())

    items = payload
    for part in source.get("items_path", "").split("."):
        if part:
            items = items.get(part, []) if isinstance(items, dict) else []

    out = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        row = {key: clean_html(str(dig(raw, path)))
               for key, path in source.get("fields", {}).items() if path}
        if row.get("url"):
            row["url"] = urljoin(source.get("url_prefix", source["url"]),
                                 row["url"])
        out.append(row)
    return out


def fetch_egu(source: dict, config: dict) -> list[dict]:
    """Parse EGU job-board cards (title, employer, location, date, summary)."""
    resp = requests.get(source["url"], headers={"User-Agent": UA},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for card in soup.select("article"):
        title_link = card.select_one("h2 a[href], h3 a[href]")
        if not title_link:
            continue

        company = location = posted_at = ""
        for item in card.select(".list-inline-item"):
            icon = item.find("i")
            classes = " ".join(icon.get("class", [])) if icon else ""
            if "fa-university" in classes:
                company_link = item.find("a", href=True)
                company = clean_html(company_link.get_text(" ", strip=True)
                                     if company_link else item.get_text(" ", strip=True))
            elif "fa-globe" in classes:
                location = clean_html(item.get_text(" ", strip=True))
            elif "fa-calendar-plus" in classes:
                time_tag = item.find("time")
                posted_at = (time_tag.get("datetime", "") if time_tag
                             else item.get_text(" ", strip=True))

        summary = card.select_one(".media-body > p:not(.more-link)")
        out.append({
            "title": clean_html(title_link.get_text(" ", strip=True)),
            "company": company,
            "location": location,
            "url": urljoin(source["url"], title_link["href"]),
            "description": clean_html(summary.get_text(" ", strip=True)
                                       if summary else card.get_text(" ", strip=True)),
            "posted_at": posted_at,
        })
    return out


def _relative_date(text: str) -> str:
    """Convert French relative posting dates into sortable ISO timestamps."""
    value = clean_html(text).lower()
    current = datetime.now(timezone.utc)
    if "moins d'une heure" in value or "moins d’une heure" in value:
        return current.isoformat(timespec="seconds")
    match = re.search(
        r"(?:il y a|publi[eé] il y a)\s+"
        r"(?:(plus de|moins de)\s+)?(\d+)\s+"
        r"(minute|heure|jour|semaine|mois)s?\b", value,
    )
    if not match:
        if "hier" in value:
            return (current - timedelta(days=1)).isoformat(timespec="seconds")
        return value
    modifier = match.group(1)
    amount = int(match.group(2))
    if modifier == "plus de":
        amount += 1
    elif modifier == "moins de":
        amount = max(0, amount - 1)
    unit = match.group(3)
    delta = {
        "minute": timedelta(minutes=amount),
        "heure": timedelta(hours=amount),
        "jour": timedelta(days=amount),
        "semaine": timedelta(weeks=amount),
        "mois": timedelta(days=30 * amount),
    }[unit]
    return (current - delta).isoformat(timespec="seconds")


def fetch_hellowork(source: dict, config: dict) -> list[dict]:
    """Parse public HelloWork search cards; configured URLs have no query string."""
    resp = requests.get(source["url"], headers={"User-Agent": UA},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for card in soup.select('li[data-id-storage-target="item"]'):
        title_link = card.select_one('a[data-cy="offerTitle"][href]')
        if not title_link:
            continue
        title_parts = title_link.select("h3 p")
        title = (title_parts[0].get_text(" ", strip=True) if title_parts
                 else title_link.get("title", "").split(" - ")[0])
        company = (title_parts[1].get_text(" ", strip=True)
                   if len(title_parts) > 1 else "")
        location_tag = card.select_one('[data-cy="localisationCard"]')
        contract_tag = card.select_one('[data-cy="contractCard"]')
        relative = ""
        for candidate in card.stripped_strings:
            if re.search(r"\bil y a\b|\bmoins d'une heure\b|\bhier\b", candidate, re.I):
                relative = candidate
                break
        details = ""
        if contract_tag:
            details = "Contrat affiché : " + contract_tag.get_text(" ", strip=True)
        if not details:
            details = "Résumé indisponible dans la carte ; ouvrir l'offre pour vérifier les missions."
        out.append({
            "title": clean_html(title),
            "company": clean_html(company),
            "location": clean_html(location_tag.get_text(" ", strip=True)
                                    if location_tag else ""),
            "url": urljoin(source["url"], title_link["href"]),
            "description": details,
            "posted_at": _relative_date(relative) if relative else "",
        })
    return out


def fetch_france_travail(source: dict, config: dict) -> list[dict]:
    """Parse France Travail public job-search results (clean slug URL)."""
    resp = requests.get(source["url"], headers={"User-Agent": UA},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for card in soup.select("li.result"):
        title_link = card.select_one('a.media[href*="/offres/recherche/detail/"]')
        title_tag = card.select_one(".media-heading-title")
        if not title_link or not title_tag:
            continue
        company_location = card.select_one("p.subtext")
        location_tag = company_location.find("span") if company_location else None
        location = (location_tag.get_text(" ", strip=True)
                    if location_tag else "")
        company = (company_location.get_text(" ", strip=True).replace(
            location, "").strip(" -–") if company_location else "")
        description_tag = card.select_one("p.description")
        date_tag = card.select_one("p.date")
        out.append({
            "title": clean_html(title_tag.get_text(" ", strip=True)),
            "company": clean_html(company),
            "location": clean_html(location),
            "url": urljoin(source["url"], title_link["href"]),
            "description": clean_html(description_tag.get_text(" ", strip=True)
                                       if description_tag else ""),
            "posted_at": _relative_date(date_tag.get_text(" ", strip=True)
                                        if date_tag else ""),
        })
    return out


def fetch_rss(source: dict, config: dict) -> list[dict]:
    resp = requests.get(source["url"], headers={"User-Agent": UA},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    fields = source.get("fields", {})
    out = []
    for entry in parsed.entries:
        row = {}
        for key, path in fields.items():
            if not path:
                continue
            value = entry.get(path, "")
            row[key] = clean_html(str(value)) if value else ""
        if row:
            out.append(row)
    return out


FETCHERS = {
    "json": fetch_json,
    "rss": fetch_rss,
    "next_json": fetch_next_json,
    "egu": fetch_egu,
    "hellowork": fetch_hellowork,
    "france_travail": fetch_france_travail,
}


# ── Collecte ─────────────────────────────────────────────────────────

def collect(config: dict, *, dry: bool = False, check: bool = False) -> int:
    conn = None if dry or check else db()
    run_id = log_run(conn, "collect") if conn else None
    stats = {"kept": 0, "keyword_out": 0, "duplicate": 0, "error": 0,
             "sources": 0}
    problems: list[str] = []
    run_fingerprints: set[str] = set()

    delay = float(config.get("general", {}).get("request_delay_seconds", 1))
    for index, source in enumerate(config.get("sources", [])):
        name = source.get("name", source.get("url", "?"))
        if not source.get("enabled", True):
            continue
        fetcher = FETCHERS.get(source.get("type", "rss"))
        if fetcher is None:
            problems.append(f"{name}: type inconnu ({source.get('type')})")
            continue

        try:
            if index:
                time.sleep(delay)
            items = fetcher(source, config)
        except Exception as exc:  # noqa: BLE001 — une source morte ne casse pas le run
            stats["error"] += 1
            problems.append(f"{name}: {type(exc).__name__} {exc}")
            continue

        stats["sources"] += 1

        if check:
            print(f"  OK   {name:<24} {len(items):>4} éléments")
            continue

        limit = int(config.get("general", {}).get("max_description_chars", 1500))
        for item in items:
            title = item.get("title", "")
            if not title:
                continue
            description = (item.get("description") or "")[:limit]

            kept, term = keyword_match(
                config, title, description, item.get("location", "")
            )
            if not kept:
                stats["keyword_out"] += 1
                continue

            fp = fingerprint(title, item.get("company", ""),
                             item.get("location", ""), item.get("url", ""))

            if fp in run_fingerprints:
                stats["duplicate"] += 1
                continue
            run_fingerprints.add(fp)

            if dry:
                stats["kept"] += 1
                print(f"  KEEP {title[:70]!r} [{term}]")
                continue

            cur = conn.execute(
                "INSERT OR IGNORE INTO offers "
                "(fingerprint, source, title, company, location, url, "
                " description, posted_at, first_seen, keyword_hit) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (fp, source["name"], title, item.get("company", ""),
                 item.get("location", ""), item.get("url", ""), description,
                 item.get("posted_at", ""), now(), 1),
            )
            if cur.rowcount:
                stats["kept"] += 1
            else:
                stats["duplicate"] += 1
                existing = conn.execute(
                    "SELECT source, description FROM offers WHERE fingerprint = ?",
                    (fp,),
                ).fetchone()
                if existing and existing["source"] == source["name"]:
                    description_changed = existing["description"] != description
                    conn.execute(
                        "UPDATE offers SET title=?, company=?, location=?, url=?, "
                        "description=?, posted_at=?, "
                        "score=CASE WHEN ? THEN NULL ELSE score END, "
                        "score_reason=CASE WHEN ? THEN NULL ELSE score_reason END, "
                        "score_model=CASE WHEN ? THEN NULL ELSE score_model END, "
                        "scored_at=CASE WHEN ? THEN NULL ELSE scored_at END "
                        "WHERE fingerprint=?",
                        (title, item.get("company", ""), item.get("location", ""),
                         item.get("url", ""), description, item.get("posted_at", ""),
                         description_changed, description_changed, description_changed,
                         description_changed, fp),
                    )
                    conn.commit()

    if check:
        if problems:
            print("\nSources en échec :")
            for p in problems:
                print(f"  KO   {p}")
        return 1 if problems else 0

    if conn:
        end_run(conn, run_id, " ".join(f"{k}={v}" for k, v in stats.items()))

    print(f"Sources interrogées : {stats['sources']}")
    print(f"Nouvelles offres retenues : {stats['kept']}")
    print(f"Écartées (mots-clés)      : {stats['keyword_out']}")
    print(f"Doublons ignorés          : {stats['duplicate']}")
    if stats["error"]:
        print(f"Sources en échec         : {stats['error']}")
        for p in problems:
            print(f"  - {p}")
    if dry:
        print("\n(--dry : rien n'a été écrit)")
    if conn:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="teste les sources sans écrire")
    parser.add_argument("--dry", action="store_true",
                        help="affiche le filtrage sans écrire en base")
    parser.add_argument("--profile", default=None,
                        help="profil à utiliser (défaut : profil unique actif)")
    args = parser.parse_args()

    if args.check:
        print("Test de joignabilité des sources…")
    return collect(load_config(args.profile), dry=args.dry, check=args.check)


if __name__ == "__main__":
    sys.exit(main())
