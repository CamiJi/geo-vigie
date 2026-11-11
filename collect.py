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
    geo_ok,
    keyword_match,
    load_shared_config,
    log_run,
    now,
    profile_configs,
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


def _french_date(text: str) -> str:
    """Date absolue française « JJ/MM/AAAA » → ISO triable, sinon texte brut."""
    value = clean_html(text)
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
    if not match:
        return value
    day, month, year = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc).isoformat(
            timespec="seconds")
    except ValueError:
        return value


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

    entries = list(parsed.entries)
    # Certains flux (ex. Journal des Palaces) contiennent des années d'offres :
    # `max_items` garde les plus récentes, sans exploser le scoring.
    max_items = int(source.get("max_items") or 0)
    if max_items and len(entries) > max_items:
        entries = sorted(
            entries,
            key=lambda e: (e.get("published_parsed") is not None,
                           e.get("published_parsed") or ()),
            reverse=True,
        )[:max_items]

    fields = source.get("fields", {})
    out = []
    for entry in entries:
        row = {}
        for key, path in fields.items():
            if not path:
                continue
            value = entry.get(path, "")
            row[key] = clean_html(str(value)) if value else ""
        if row:
            out.append(row)
    return out


def fetch_lhotellerie(source: dict, config: dict) -> list[dict]:
    """Parse L'Hôtellerie-Restauration job cards (Titelive).

    Une offre = un `<a href="/emploi/<hex>/…">` enveloppant un div.card avec
    h3 (titre), p.location, p.card-text (résumé), p.date (JJ/MM/AAAA).
    Les liens logo/`sticky-job-link` sans card sont écartés naturellement.
    """
    resp = requests.get(source["url"], headers={"User-Agent": UA},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    base = source["url"].split("/emploi/")[0]
    out, seen = [], set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        match = re.match(r"(/emploi/[0-9a-f]{8}/[^?]+)", href)
        if not match:
            continue
        title_tag = anchor.select_one("h3.text-primary.fw-bold")
        location_tag = anchor.select_one("p.location")
        if not title_tag or not location_tag:
            continue
        clean_url = urljoin(base, match.group(1))
        if clean_url in seen:
            continue
        seen.add(clean_url)
        date_tag = anchor.select_one("p.date")
        desc_tag = anchor.select_one("p.card-text")
        out.append({
            "title": clean_html(title_tag.get_text(" ", strip=True)),
            "company": "",
            "location": clean_html(location_tag.get_text(" ", strip=True)),
            "url": clean_url,
            "description": clean_html(desc_tag.get_text(" ", strip=True)
                                       if desc_tag else ""),
            "posted_at": _french_date(date_tag.get_text(strip=True)
                                      if date_tag else ""),
        })
    return out


def fetch_jina_markdown(source: dict, config: dict) -> list[dict]:
    """Lit une page via le lecteur r.jina.ai (proxy qui contourne les
    blocages IP/Cloudflare) et extrait les offres d'un flux converti en
    Markdown.

    Utilisé pour Journal des Palaces : son flux RSS renvoie 403 depuis l'IP
    du serveur (Cloudflare), mais r.jina.ai le lit et le renvoie en
    Markdown structuré (`### [titre](url)` + date). Fragile par nature :
    si le format change, le parseur renvoie 0 offre et la source est marquée
    en échec par `source_health`. Pas de clé requise (niveau gratuit).
    """
    resp = requests.get(source["url"], headers={"User-Agent": UA,
                                               "X-Return-Format": "markdown"},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    text = resp.text
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]

    matches = list(re.finditer(
        r"###\s*\[(.+?)\]\((https://www\.journaldespalaces\.com/"
        r"carriere/offre-\d+-[^)]+\.html)\)", text))
    if not matches:
        raise ValueError("aucune offre parsée (format changé ?)")

    max_items = int(source.get("max_items") or 0) or 50
    seen: set[str] = set()
    out = []
    for i, match in enumerate(matches):
        if len(out) >= max_items:
            break
        title = clean_html(match.group(1).strip())
        url = match.group(2).strip()
        if url in seen:
            continue
        seen.add(url)
        segment = text[match.end():(matches[i + 1].start()
                                    if i + 1 < len(matches) else match.end() + 300)]
        date_match = re.search(
            r"(\w{3},\s*\d{1,2}\s+\w{3}\s+\d{4}"
            r"\s+\d{2}:\d{2}:\d{2}\s*[+-]\d{4})", segment)
        posted = date_match.group(1).strip() if date_match else ""
        company = (title.split(" recrute ", 1)[0].strip()
                   if " recrute " in title else "")
        out.append({
            "title": title,
            "company": clean_html(company),
            "location": "",
            "url": url,
            "description": "",
            "posted_at": posted,
        })
    if not out:
        raise ValueError("aucune offre parsée (format changé ?)")
    return out


def fetch_michaelpage(source: dict, config: dict) -> list[dict]:
    """Parse Michael Page job tiles (lieu, contrat, SALAIRE, télétravail).

    Le salaire et la nature du poste sont injectés dans la description :
    ils alimentent directement le plancher salarial au scoring.
    """
    resp = requests.get(source["url"], headers={"User-Agent": UA},
                        timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for card in soup.select("div.job-tile"):
        link = card.select_one("h3 a[href]")
        if not link:
            continue
        pieces = []
        for cls, label in (("job-contract-type", "Contrat"),
                           ("job-salary", "Salaire"),
                           ("job-nature", "Nature")):
            tag = card.select_one(f".{cls}")
            if tag:
                value = clean_html(tag.get_text(" ", strip=True))
                if value:
                    pieces.append(f"{label} : {value}")
        summary = card.select_one(".job-summary")
        description = (clean_html(summary.get_text(" ", strip=True))
                       if summary else "")
        if pieces:
            description = (description + " — " + " · ".join(pieces)
                           ).strip(" —")
        location = card.select_one(".job-location")
        out.append({
            "title": clean_html(link.get_text(" ", strip=True)),
            "company": "",
            "location": clean_html(location.get_text(" ", strip=True)
                                    if location else ""),
            "url": urljoin(source["url"], link["href"]),
            "description": description,
            "posted_at": "",
        })
    return out


FETCHERS = {
    "json": fetch_json,
    "rss": fetch_rss,
    "next_json": fetch_next_json,
    "egu": fetch_egu,
    "hellowork": fetch_hellowork,
    "france_travail": fetch_france_travail,
    "lhotellerie": fetch_lhotellerie,
    "michaelpage": fetch_michaelpage,
    "jina_markdown": fetch_jina_markdown,
}


# ── Matching par profil ──────────────────────────────────────────────

def _match_profiles(profiles: dict, title: str, description: str,
                    location: str) -> tuple[dict[str, str], int]:
    """({profile_id: terme retenu}, nb de paires écartées par le filtre géo).

    Le `geo_filter` du profil est un filtre DUR : une offre hors périmètre
    ne coûte aucune notation LLM et ne peut figurer dans le rapport.
    """
    matched: dict[str, str] = {}
    geo_out = 0
    for pid, pconf in profiles.items():
        kept, term = keyword_match(pconf, title, description, location)
        if not kept:
            continue
        if not geo_ok(location, pconf):
            geo_out += 1
            continue
        matched[pid] = term
    return matched, geo_out


def _update_offer(conn, fp: str, title: str, item: dict, description: str,
                  location: str) -> None:
    from common import geocode as _geocode
    _lat, _lon = _geocode(location) or (None, None)
    conn.execute(
        "UPDATE offers SET title=?, company=?, location=?, url=?, "
        "description=?, posted_at=?, lat=?, lon=? WHERE fingerprint=?",
        (title, item.get("company", ""), location, item.get("url", ""),
         description, item.get("posted_at", ""), _lat, _lon, fp),
    )


# ── Collecte ─────────────────────────────────────────────────────────

def _record_source(conn, run_id, source: str, ok: bool, items: int = 0,
                   error: str = "") -> None:
    """Trace la santé d'une source pour un relevé (table `source_health`).

    `run_id` regroupe toutes les sources d'un même relevé : sans lui, un
    horodatage par source empêche de dire « ce relevé-ci ».
    """
    if conn is None:
        return
    conn.execute(
        "INSERT INTO source_health (run_id, source, ok, items, error, "
        "checked_at) VALUES (?,?,?,?,?,?)",
        (run_id, source, 1 if ok else 0, items, error[:200], now()),
    )
    conn.commit()


def collect(config: dict, *, dry: bool = False, check: bool = False,
            restrict: str | None = None) -> int:
    conn = None if dry or check else db()
    run_id = log_run(conn, "collect") if conn else None
    stats = {"kept": 0, "keyword_out": 0, "geo_out": 0, "duplicate": 0,
             "error": 0, "sources": 0}
    problems: list[str] = []
    run_fingerprints: set[str] = set()
    profile_hits: dict[str, int] = {}

    # Collecte partagée : on matche TOUS les profils actifs (ou un seul si
    # --profile l'a restreint pour le debug). Le check ne matche rien.
    profiles = {} if check else profile_configs(restrict)
    if not profiles and not check:
        raise SystemExit(
            "Aucun profil actif à matcher — créer profiles/<id>/profile.yaml "
            "ou activer un profil (`active: true`).")

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
            if not check:
                _record_source(conn, run_id, name, False, 0,
                               f"{type(exc).__name__}: {exc}")
            continue

        stats["sources"] += 1
        if not check:
            _record_source(conn, run_id, name, True, len(items))

        if check:
            # 0 élément = quasi toujours un incident (rendu passé en JS,
            # page vide, blocage) : on l'alerte et on sort en erreur.
            if not items:
                problems.append(f"{name}: 0 élément "
                                "(page vide, rendu JS ou blocage ?)")
                print(f"  VIDE {name:<24}    0 élément  ⚠")
            else:
                print(f"  OK   {name:<24} {len(items):>4} éléments")
            continue

        limit = int(config.get("general", {}).get("max_description_chars", 1500))
        for item in items:
            title = item.get("title", "")
            if not title:
                continue
            description = (item.get("description") or "")[:limit]
            location = item.get("location", "")

            if dry:
                matched, geo_out = _match_profiles(
                    profiles, title, description, location)
                stats["geo_out"] += geo_out
                if not matched:
                    stats["keyword_out"] += 1
                    continue
                stats["kept"] += 1
                for pid in matched:
                    profile_hits[pid] = profile_hits.get(pid, 0) + 1
                detail = ", ".join(f"{pid}:{term}" for pid, term in matched.items())
                print(f"  KEEP {title[:70]!r} [{detail}]")
                continue

            fp = fingerprint(title, item.get("company", ""), location,
                             item.get("url", ""))
            if fp in run_fingerprints:
                stats["duplicate"] += 1
                continue
            run_fingerprints.add(fp)

            existing = conn.execute(
                "SELECT id, source, description FROM offers WHERE fingerprint = ?",
                (fp,),
            ).fetchone()
            description_changed = (
                existing is not None
                and existing["source"] == source["name"]
                and existing["description"] != description
            )

            # Matching PAR PROFIL : retenue si AU MOINS un profil actif la
            # matche — une offre utile à Gabriela entre même si elle est
            # hors-mots-clés de Quentin.
            matched, geo_out = _match_profiles(
                profiles, title, description, location)
            stats["geo_out"] += geo_out

            if not matched:
                if description_changed:
                    # Contenu devenu hors-profil : scores et matches
                    # portaient sur l'ancien texte → invalidation.
                    conn.execute("DELETE FROM scores WHERE offer_id = ?",
                                 (existing["id"],))
                    conn.execute("DELETE FROM matches WHERE offer_id = ?",
                                 (existing["id"],))
                    _update_offer(conn, fp, title, item, description, location)
                    conn.commit()
                stats["keyword_out"] += 1
                continue

            from common import geocode as _geocode
            _lat, _lon = _geocode(location) or (None, None)
            cur = conn.execute(
                "INSERT OR IGNORE INTO offers "
                "(fingerprint, source, title, company, location, url, "
                " description, posted_at, first_seen, keyword_hit, lat, lon) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (fp, source["name"], title, item.get("company", ""),
                 location, item.get("url", ""), description,
                 item.get("posted_at", ""), now(), 1, _lat, _lon),
            )
            if cur.rowcount:
                stats["kept"] += 1
                offer_id = cur.lastrowid
            else:
                stats["duplicate"] += 1
                offer_id = existing["id"]
                if description_changed:
                    conn.execute("DELETE FROM scores WHERE offer_id = ?",
                                 (offer_id,))
                    conn.execute("DELETE FROM matches WHERE offer_id = ?",
                                 (offer_id,))
                _update_offer(conn, fp, title, item, description, location)

            # Paires (offre, profil) — au premier run, un nouveau profil
            # rattache aussi les offres déjà en base.
            for pid, term in matched.items():
                conn.execute(
                    "INSERT INTO matches (offer_id, profile_id, term, matched_at) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(offer_id, profile_id) DO UPDATE SET "
                    "term=excluded.term, matched_at=excluded.matched_at",
                    (offer_id, pid, term, now()),
                )
                profile_hits[pid] = profile_hits.get(pid, 0) + 1
            conn.commit()

    if check:
        if problems:
            print("\nSources en échec :")
            for p in problems:
                print(f"  KO   {p}")
        return 1 if problems else 0

    if conn:
        detail = " ".join(f"{k}={v}" for k, v in stats.items())
        if profile_hits:
            detail += " | matchs " + ", ".join(
                f"{pid}={n}" for pid, n in sorted(profile_hits.items()))
        end_run(conn, run_id, detail)

    print(f"Sources interrogées : {stats['sources']}")
    print(f"Nouvelles offres retenues : {stats['kept']}")
    print(f"Écartées (mots-clés)      : {stats['keyword_out']}")
    print(f"Doublons ignorés          : {stats['duplicate']}")
    if profile_hits:
        print("Matchs retenus par profil : "
              + ", ".join(f"{pid}={n}" for pid, n in sorted(profile_hits.items())))
    if stats["geo_out"]:
        print(f"Écartées (filtre géo du profil) : {stats['geo_out']}")
    if stats["error"]:
        print(f"Sources en échec         : {stats['error']}")
        for p in problems:
            print(f"  - {p}")
    if dry:
        print("\n(--dry : rien n'a été écrit)")
    if conn:
        # Backfill géocodage (BAN) pour les offres anciennes : une fois.
        missing = conn.execute(
            "SELECT id, location FROM offers WHERE lat IS NULL").fetchall()
        if missing:
            from common import geocode as _geocode
            done = 0
            for row in missing:
                coords = _geocode(row["location"] or "")
                if coords:
                    conn.execute("UPDATE offers SET lat=?, lon=? WHERE id=?",
                                 (coords[0], coords[1], row["id"]))
                    done += 1
            conn.commit()
            if done:
                print(f"Géocodage : {done}/{len(missing)} offre(s) localisée(s).")
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
    # Config partagée seule : la collecte matche tous les profils actifs,
    # elle n'est pas propre à un profil (pas de résolution automatique ici).
    return collect(load_shared_config(), dry=args.dry, check=args.check,
                   restrict=args.profile)


if __name__ == "__main__":
    sys.exit(main())
