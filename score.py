#!/usr/bin/env python3
"""geo-vigie — note chaque nouvelle offre /100 selon le CV, avec justification.

La notation est par profil : chaque paire (offre, profil) est notée une fois
dans la table `scores`.

Usage :
    python score.py            # score les offres non notées pour le profil
    python score.py --limit 5  # borné (utile pour un premier essai)
    python score.py --rescore  # ignore le score déjà posé
    python score.py --profile gabriela
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from common import (
    ROOT,
    db,
    end_run,
    load_config,
    load_env,
    log_run,
    now,
    profile_of,
)

PROMPT = """{persona}

## CV DU CANDIDAT

{cv}

## OFFRE

Titre    : {title}
Entreprise : {company}
Lieu     : {location}
Source   : {source}
Description :
{description}

## CONSIGNE

Renvoie STRICTEMENT un objet JSON, sans texte autour, sans balises Markdown :

{{
  "score": <entier 0-100>,
  "reason": "<2 phrases maximum, en français : les 1 ou 2 raisons \
déterminantes de ce score>",
  "verdict": "<un des: pertinent | possible | hors-profil>"
}}

Pondération :
{weights}

Règles :
{rules}
"""

# Blocs par défaut du profil de référence (quentin) : sans `persona`,
# `location` ni `scoring.weights/rules` dans son profile.yaml, le prompt
# construit reste strictement identique à l'ancien PROMPT mono-profil.
DEFAULT_PERSONA = """Tu es un recruteur spécialisé en géosciences (géologie, hydrogéologie,\
géotechnique, mines, hydrocarbures). Tu évalues la pertinence d'une offre \
pour UN candidat précis."""

DEFAULT_WEIGHTS = """- 45 % adéquation des compétences métier avec le CV
- 20 % adéquation du niveau (junior/confirmé/senior) par rapport au CV
- 20 % localisation et mobilité déclarées dans le CV
- 15 % secteur et type de contrat"""

DEFAULT_RULES = """- 0 si l'offre n'a rien à voir avec les géosciences.
- Ne pénalise pas l'absence d'expérience que le CV n'a jamais eue (ex. poste \
à l'étranger si le CV est ouvert à la mobilité).
- Si l'offre est vague ou une arnaque plausible, plafonne à 40.
- La justification doit être actionnable : le candidat doit comprendre \
pourquoi en lisant 2 phrases."""


def _location_sentence(config: dict) -> str:
    """Contrainte géo factuelle injectée au prompt (pas un filtre dur)."""
    loc = config.get("location") or {}
    if not loc:
        return ""
    zones = ", ".join(loc.get("zones") or []) or "—"
    parts = [f"Localisation : base {loc.get('base', '—')} ; "
             f"zones visées : {zones}."]
    if loc.get("remote"):
        parts.append(f"Télétravail : {loc['remote']}.")
    if loc.get("relocation"):
        parts.append(f"Relocation : {loc['relocation']}.")
    return " ".join(parts)


def _floor_rule(config: dict) -> str:
    """Règle de plancher générée depuis `scoring.min_salary_net_month`.

    Sous le plancher → score plafonné à 30 (verdict hors-profil) ; salaire
    non mentionné → aucun malus, mais signalé dans la justification.
    """
    floor = (config.get("scoring") or {}).get("min_salary_net_month")
    if not floor:
        return ""
    floor = int(floor)
    annual_net = floor * 12
    annual_gross = round(annual_net / 0.78 / 1000)  # ≈ 34 k€ brut/an
    return (
        f"- Plancher salarial : {floor} € net/mois (≈ {annual_net} € net/an, "
        f"ordre de grandeur ≈ {annual_gross} k€ brut/an). Si l'offre annonce "
        "un salaire clairement sous ce plancher, plafonne le score à 30 et "
        "mets le verdict « hors-profil ». Si l'offre ne mentionne aucun "
        "salaire, NE pénalise pas le score, mais fais commencer `reason` par "
        "« Salaire non mentionné : »."
    )


def build_prompt(config: dict, *, cv: str, title: str, company: str,
                 location: str, source: str, description: str) -> str:
    """Prompt de scoring propre au profil (persona, geo, plancher, règles)."""
    persona = config.get("persona") or DEFAULT_PERSONA
    loc = _location_sentence(config)
    if loc:
        persona = f"{persona}\n{loc}"
    weights = (config.get("scoring") or {}).get("weights") or DEFAULT_WEIGHTS
    rules = (config.get("scoring") or {}).get("rules") or DEFAULT_RULES
    floor = _floor_rule(config)
    if floor:
        rules = f"{rules.rstrip()}\n{floor}"
    return PROMPT.format(
        persona=persona,
        cv=cv,
        title=title,
        company=company,
        location=location,
        source=source,
        description=description,
        weights=weights.strip(),
        rules=rules.strip(),
    )


def load_cv(config: dict) -> str:
    cv_rel = config.get("cv_file", "cv/geologue-placeholder.md")
    path = ROOT / cv_rel
    if not path.exists():
        raise SystemExit(f"CV introuvable : {path}\n"
                         "Ajoute le fichier et/ou règle `cv_file` dans config.yaml")
    cv = path.read_text(encoding="utf-8")
    # Le scoring n'a besoin ni du nom, ni des moyens de contact du candidat.
    # Le CV fourni est en sections ; couper l'en-tête jusqu'à Languages retire
    # le nom + le bloc Contact (téléphone, email et ville précise).
    cv = re.sub(r"(?is)^.*?(?=^Languages\b)", "", cv, count=1,
                flags=re.MULTILINE)
    return (
        "Profil anonymisé. Localisation : France. Mobilité internationale : "
        "non précisée dans le CV.\n\n"
        + cv.strip()
    )


def ask_llm(prompt: str, env: dict, model: str, temperature: int) -> dict:
    """Appel OpenRouter-compatible, avec backoff sur 429/5xx temporaires."""
    url = f"{env['LLM_BASE_URL'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {env['LLM_API_KEY']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }

    max_retries = 3
    for attempt in range(max_retries + 1):
        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt == max_retries:
                resp.raise_for_status()
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2 ** (attempt + 1)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = max(
                        0,
                        (retry_at - datetime.now(timezone.utc)).total_seconds(),
                    )
                except (TypeError, ValueError, OverflowError):
                    delay = 2 ** (attempt + 1)
            time.sleep(min(delay, 60))
            continue
        resp.raise_for_status()
        break

    content = resp.json()["choices"][0]["message"]["content"]
    return parse_json(content)


def parse_json(content: str) -> dict:
    """Tolère les balises Markdown et le texte avant/après le JSON."""
    text = content.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"Réponse sans JSON : {content[:200]!r}")
    data = json.loads(match.group(0))
    if "score" not in data:
        raise ValueError(f"JSON sans `score` : {data}")
    data["score"] = max(0, min(100, int(data["score"])))
    data.setdefault("reason", "")
    data.setdefault("verdict", "")
    return data


def score(config: dict, *, limit: int | None = None, rescore: bool = False) -> int:
    env = load_env()
    if not env.get("LLM_API_KEY"):
        raise SystemExit("LLM_API_KEY manquante (.env) — scoring impossible.")
    if not env.get("LLM_BASE_URL"):
        raise SystemExit("LLM_BASE_URL manquant (.env).")

    cv = load_cv(config)
    model = config.get("scoring", {}).get("model") or env.get("LLM_MODEL")
    if not model:
        raise SystemExit("LLM_MODEL manquant (.env) ou `scoring.model` vide.")
    temperature = int(config.get("scoring", {}).get("temperature", 0))

    conn = db()
    run_id = log_run(conn, "score")
    profile = profile_of(config)

    active_sources = [
        source["name"] for source in config.get("sources", [])
        if source.get("enabled", True)
    ]
    if not active_sources:
        print("Aucune source active à noter.")
        end_run(conn, run_id, f"profile={profile} aucune source active")
        conn.close()
        return 0

    placeholders = ",".join("?" for _ in active_sources)
    # Vue par profil : l'offre n'est scorable que si les mots-clés du profil
    # l'ont retenue (table matches) ET si elle n'est pas déjà notée pour lui.
    where = "s.id IS NULL" if not rescore else "1=1"
    sql = (
        "SELECT o.* FROM offers o "
        "JOIN matches m ON m.offer_id = o.id AND m.profile_id = ? "
        "LEFT JOIN scores s ON s.offer_id = o.id AND s.profile_id = ? "
        f"WHERE {where} "
        f"AND o.source IN ({placeholders}) "
        "ORDER BY CASE WHEN o.posted_at != '' THEN o.posted_at "
        "ELSE o.first_seen END DESC, o.id DESC"
    )
    params: list = [profile, profile, *active_sources]
    if limit is not None:   # --limit 0 = aucun appel, bornage strict
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("Aucune offre à noter.")
        end_run(conn, run_id, f"profile={profile} 0 notée")
        conn.close()
        return 0

    print(f"Notation de {len(rows)} offre(s) avec {model} (profil : {profile})…")
    ok = failed = 0
    for row in rows:
        prompt = build_prompt(
            config,
            cv=cv,
            title=row["title"],
            company=row["company"] or "—",
            location=row["location"] or "—",
            source=row["source"],
            description=row["description"] or "(aucune description fournie)",
        )
        try:
            data = ask_llm(prompt, env, model, temperature)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  KO  {row['title'][:60]!r} → {exc}")
            continue

        conn.execute(
            "INSERT INTO scores (offer_id, profile_id, score, score_reason, "
            "score_model, scored_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(offer_id, profile_id) DO UPDATE SET "
            "score=excluded.score, score_reason=excluded.score_reason, "
            "score_model=excluded.score_model, scored_at=excluded.scored_at",
            (row["id"], profile, data["score"], data["reason"], model, now()),
        )
        conn.commit()
        ok += 1
        print(f"  {data['score']:>3}  {row['title'][:60]!r}")
        if data["reason"]:
            print(f"      ↳ {data['reason']}")

    end_run(conn, run_id, f"profile={profile} ok={ok} ko={failed}")
    conn.close()
    print(f"\nNotées : {ok} — échecs : {failed}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--rescore", action="store_true")
    parser.add_argument("--profile", default=None,
                        help="profil à utiliser (défaut : profil unique actif)")
    args = parser.parse_args()
    return score(load_config(args.profile), limit=args.limit, rescore=args.rescore)


if __name__ == "__main__":
    sys.exit(main())
