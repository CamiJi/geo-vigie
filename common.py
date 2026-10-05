"""geo-vigie — utilitaires partagés (config, base, normalisation)."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db" / "vigie.sqlite"


# ── Config ───────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_env() -> dict:
    """Charge .env en mémoire. Pas de dépendance python-dotenv."""
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            # Commentaires : ligne entière OU en fin de valeur.
            # Sans ça, `MAIL_FROM=x@y.z   # note` embarque la note dans
            # l'adresse et SMTP refuse la connexion.
            line = line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    # L'environnement réel prime sur le fichier.
    for key in ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "SMTP_HOST",
                "SMTP_PORT", "SMTP_SECURITY", "SMTP_USER", "SMTP_LOGIN",
                "SMTP_PASS", "MAIL_FROM", "MAIL_TO", "MAIL_CC"):
        if os.environ.get(key):
            env[key] = os.environ[key]

    # Brevo nomme ce champ « Login » ; d'autres fournisseurs « User ».
    # On accepte les deux, avec un message clair si les deux manquent.
    if not env.get("SMTP_USER"):
        env["SMTP_USER"] = env.get("SMTP_LOGIN", "")
    return env


# ── Base ─────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT UNIQUE NOT NULL,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    company      TEXT DEFAULT '',
    location     TEXT DEFAULT '',
    url          TEXT DEFAULT '',
    description  TEXT DEFAULT '',
    posted_at    TEXT DEFAULT '',
    first_seen   TEXT NOT NULL,
    keyword_hit  INTEGER DEFAULT 0,
    score        INTEGER,
    score_reason TEXT,
    score_model  TEXT,
    scored_at    TEXT,
    reported_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_score ON offers(score DESC);
CREATE INDEX IF NOT EXISTS idx_offers_reported ON offers(reported_at);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    started   TEXT NOT NULL,
    finished  TEXT,
    detail    TEXT
);
"""


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_run(conn: sqlite3.Connection, kind: str, detail: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO runs (kind, started, detail) VALUES (?, ?, ?)",
        (kind, now(), detail),
    )
    conn.commit()
    return cur.lastrowid


def end_run(conn: sqlite3.Connection, run_id: int, detail: str = "") -> None:
    conn.execute(
        "UPDATE runs SET finished = ?, detail = COALESCE(NULLIF(?, ''), detail) WHERE id = ?",
        (now(), detail, run_id),
    )
    conn.commit()


# ── Normalisation ────────────────────────────────────────────────────

def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c))


def slug(text: str) -> str:
    """Base de comparaison insensible aux accents/casse pour les mots-clés."""
    return strip_accents(text or "").lower()


def clean_html(text: str) -> str:
    """Retire les balises HTML et compresse les espaces."""
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    without_tags = re.sub(r"&nbsp;?", " ", without_tags)
    without_tags = re.sub(r"&amp;", "&", without_tags)
    return re.sub(r"\s+", " ", without_tags).strip()


def fingerprint(title: str, company: str, location: str, url: str) -> str:
    """Dédup : la même offre publiée sur 3 sites = 1 seule ligne.

    Clé = **titre + lieu**, sans l'entreprise. L'entreprise est le champ le
    plus bruité : la même offre remonte `Artefact` / `Artefactlinkedin` /
    `Artefactjobs` selon la source, et créait 3 lignes pour un seul poste.
    En cas de titre+lieu vides, on retombe sur l'URL.
    """
    base = " | ".join(slug(x) for x in (title, location) if x and x.strip())
    if not base:
        base = slug(url)
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()
    if not base:
        base = slug(company) or slug(url) or "unknown"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def keyword_match(config: dict, *parts: str) -> tuple[bool, str]:
    """Retourne (retenu, terme retenu). Insensible aux accents/casse.

    Deux niveaux de termes, pour éviter les faux positifs :

    - `include_strong` : termes distinctifs (géolog, métallogénie…).
      Cherchés partout (titre, description, lieu) — une offre qui parle
      d'hydrogéologie quelque part dans son texte est pertinente.
    - `include_weak` : termes génériques (terrain, carrière, mines, sols…).
      Cherchés **dans le titre uniquement**, avec frontière de mot.
      Sans ça, « développement de **carrières** » ou « Ter-**minier**-ung »
      font passer n'importe quelle offre.

    Le matching utilise des frontières de mot (`\\b`) : `minier` ne peut plus
    matcher à l'intérieur de `terminierung`.
    """
    kw = config["keywords"]
    title = slug(parts[0] if parts else "")
    description = slug(parts[1] if len(parts) > 1 else "")
    haystack = slug(" ".join(p for p in parts if p))

    for term in kw.get("exclude", []):
        if _has_word(haystack, term):
            return False, ""

    strong_terms = kw.get("include_strong", kw.get("include", []))
    for term in strong_terms:
        if _has_word(title, term):
            return True, term

    # Une mention isolée dans une longue description suffit rarement à faire
    # une offre de géologie (ex. poste communication qui parle de géologues).
    # Sans correspondance au titre, exiger deux termes métier distincts.
    body_hits = [term for term in strong_terms
                 if _has_word(description, term)]
    if len(set(body_hits)) >= 2:
        return True, body_hits[0]

    for term in kw.get("include_weak", []):
        if _has_word(title, term):
            return True, term

    return False, ""


def _has_word(text: str, term: str) -> bool:
    """`term` présent dans `text` (slugué) avec frontières de mot.

    Permet un préfixe libre (géolog → géologue, géologique) mais pas
    une collision au milieu d'un mot : minier ∉ terminierung.
    """
    t = slug(term).strip()
    if not t:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}", text) is not None
