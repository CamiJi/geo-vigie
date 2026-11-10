"""geo-vigie — utilitaires partagés (config, base, normalisation)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db" / "vigie.sqlite"


# ── Config ───────────────────────────────────────────────────────────

PROFILES_DIR = ROOT / "profiles"


def load_config(profile: str | None = None) -> dict:
    """Charge `config.yaml` (partagé) fusionné avec un profil.

    - `profile` : nom d'un dossier de `profiles/` (ex. `"gabriela"`).
      Si omis, le **profil unique actif** est choisi automatiquement ;
      avec plusieurs profils actifs, un choix est obligatoire (erreur claire).

    La fusion est simple : les clés du `profile.yaml` écrasent celles de
    `config.yaml` (blocs complets : keywords, scoring, report, cv_file) —
    `general` et `sources` restent partagés. `cv_file` est renvoyé en
    **chemin absolu**, relatif au dossier du profil.
    """
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    profile_id = profile or default_profile_id()
    if profile_id is None:
        # Rétrocompat : aucun profil encore configuré → l'ancien YAML seul.
        return config

    profile_file = PROFILES_DIR / profile_id / "profile.yaml"
    if not profile_file.exists():
        known = ", ".join(list_profiles()) or "aucun"
        raise SystemExit(
            f"Profil inconnu : {profile_id!r} (profils disponibles : {known})")

    with open(profile_file, encoding="utf-8") as fh:
        prof = yaml.safe_load(fh) or {}

    merged = {**config, **prof}
    merged["profile_id"] = profile_id

    cv_rel = prof.get("cv_file")
    if cv_rel and not Path(cv_rel).is_absolute():
        merged["cv_file"] = str(PROFILES_DIR / profile_id / cv_rel)
    return merged


def load_shared_config() -> dict:
    """Config partagée seule (general, sources) — sans résolution de profil.

    Utilisée par la collecte, qui matche tous les profils actifs et n'est
    propre à personne (éviterait de lever « plusieurs profils actifs »).
    """
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def list_profiles() -> list[str]:
    """Profils disponibles : dossiers de `profiles/` avec un profile.yaml."""
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(
        entry.name for entry in PROFILES_DIR.iterdir()
        if entry.is_dir() and (entry / "profile.yaml").exists()
    )


def default_profile_id() -> str | None:
    """Profil actif unique, ou None s'il n'y en a aucun.

    Plusieurs profils actifs → impossible de deviner : il faut `--profile`
    (ou VIGIE_PROFILE dans l'environnement).
    """
    env_choice = os.environ.get("VIGIE_PROFILE")
    if env_choice:
        return env_choice
    active = [pid for pid in list_profiles()
              if _load_profile_raw(pid).get("active", True)]
    if len(active) == 1:
        return active[0]
    if not active:
        return None
    raise SystemExit(
        "Plusieurs profils actifs : préciser le profil "
        f"({'--profile ' + ' | '.join(active)}) ou VIGIE_PROFILE.")


def _load_profile_raw(profile_id: str) -> dict:
    path = PROFILES_DIR / profile_id / "profile.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


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
#
# Modèle multiprofil : `offers` est le catalogue partagé (jamais de donnée
# propre à une personne) ; `scores` et `reports` sont des vues PAR PROFIL,
# clés sur (offer_id, profile_id).
#
#   offers  (fingerprint, source, titre, lieu, url, description…)
#   scores  (offer_id, profile_id, score /100, reason, model, scored_at)
#   reports (offer_id, profile_id, sent_at)   → mémorise ce qui a été envoyé
#
# `migrate_schema()` convertit l'ancien schéma mono-profil (colonnes score et
# reported_at directement dans offers) une seule fois, après backup.

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
    keyword_hit  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id     INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    profile_id   TEXT NOT NULL,
    score        INTEGER NOT NULL,
    score_reason TEXT DEFAULT '',
    score_model  TEXT DEFAULT '',
    scored_at    TEXT,
    UNIQUE (offer_id, profile_id)
);
CREATE INDEX IF NOT EXISTS idx_scores_profile_score ON scores(profile_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_scores_offer ON scores(offer_id);

CREATE TABLE IF NOT EXISTS matches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id     INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    profile_id   TEXT NOT NULL,
    term         TEXT DEFAULT '',
    matched_at   TEXT NOT NULL,
    UNIQUE (offer_id, profile_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_profile ON matches(profile_id);
CREATE INDEX IF NOT EXISTS idx_matches_offer ON matches(offer_id);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id     INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    profile_id   TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    UNIQUE (offer_id, profile_id)
);
CREATE INDEX IF NOT EXISTS idx_reports_profile_sent ON reports(profile_id, sent_at);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    started   TEXT NOT NULL,
    finished  TEXT,
    detail    TEXT
);
"""

# Colonnes de l'ancien schéma mono-profil, à migrer puis supprimer.
LEGACY_OFFER_COLUMNS = ("score", "score_reason", "score_model",
                        "scored_at", "reported_at")


def migrate_schema(conn: sqlite3.Connection, default_profile: str | None = None,
                   backup_path: Path | None = None) -> bool:
    """Bascule l'ancien schéma mono-profil → tables scores/reports (une fois).

    - Détecte l'ancien schéma par la présence de la colonne `offers.score`.
    - Sauvegarde la base (`backup_path`) avant les ALTER, irréversibles.
    - Les scores/reported existants sont attribués à `default_profile`
      (le profil unique actif à la migration ; "default" sinon).

    Retourne True si une migration a eu lieu, False si déjà à jour.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(offers)")}
    if "score" not in cols:
        return False

    profile = default_profile or "default"

    if backup_path is not None:
        if not backup_path.exists():
            shutil.copy2(DB_PATH, backup_path)

    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO scores (offer_id, profile_id, score, "
        "score_reason, score_model, scored_at) "
        "SELECT id, ?, score, score_reason, score_model, scored_at "
        "FROM offers WHERE score IS NOT NULL",
        (profile,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO reports (offer_id, profile_id, sent_at) "
        "SELECT id, ?, reported_at FROM offers WHERE reported_at IS NOT NULL",
        (profile,),
    )
    # Les index de l'ancien schéma portent sur des colonnes à supprimer :
    # SQLite refuse DROP COLUMN tant qu'ils existent.
    for index in ("idx_offers_score", "idx_offers_reported"):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for column in LEGACY_OFFER_COLUMNS:
        conn.execute(f"ALTER TABLE offers DROP COLUMN {column}")
    conn.commit()
    return True


def _migration_profile() -> str:
    """Profil destinataire des anciens scores pendant la migration."""
    try:
        return default_profile_id() or "default"
    except SystemExit:
        # Plusieurs profils actifs : impossible de trancher — les données
        # historiques partent sous "default" plutôt que d'interrompre le run.
        return "default"


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migration_profile = _migration_profile()
    if migrate_schema(conn, default_profile=migration_profile,
                      backup_path=DB_PATH.parent / (
                          f"{DB_PATH.name}.bak-"
                          f"{datetime.now(timezone.utc).date().isoformat()}"
                          "-pre-multiprofil")):
        print(f"Base migrée vers le schéma multiprofil (backup créé, "
              f"scores existants → profil {migration_profile!r}).")
    backfill_matches(conn, default_profile=migration_profile)
    return conn


def profile_of(config: dict) -> str:
    """profile_id de la config chargée, ou "default" (tests, rétrocompat)."""
    return config.get("profile_id") or "default"


def active_profile_ids() -> list[str]:
    """Profils actifs (du plus ancien au plus récent en ordre alpha)."""
    return [pid for pid in list_profiles()
            if _load_profile_raw(pid).get("active", True)]


def profile_configs(profile: str | None = None) -> dict[str, dict]:
    """Config fusionnée des profils à matcher pendant la collecte.

    - `profile` donné → ce profil seul (usage --profile / debug).
    - sinon → **tous les profils actifs** (collecte partagée) : une offre
      utile à Gabriela est retenue même si elle est hors-mots-clés de Quentin.
    """
    ids = [profile] if profile else active_profile_ids()
    return {pid: load_config(pid) for pid in ids}


def backfill_matches(conn: sqlite3.Connection, default_profile: str | None = None) -> bool:
    """Offres préexistantes sans aucun match → attribuées à un profil.

    Les offres en base ont été filtrées par les mots-clés de Quentin avant
    la table `matches` : le premier run les y rattache toutes, une seule fois
    (condition idempotente : ne s'exécute que si la table est vide).
    """
    if not conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]:
        return False
    if conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]:
        return False
    profile = default_profile or "default"
    conn.execute(
        "INSERT OR IGNORE INTO matches (offer_id, profile_id, matched_at) "
        "SELECT id, ?, ? FROM offers",
        (profile, now()),
    )
    conn.commit()
    print(f"matches backfillés → profil {profile!r} (offres préexistantes).")
    return True


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

    # Exclusions sur le TITRE seulement : un terme qui décrit un poste
    # moindre (« responsable de salle ») peut légitimement apparaître dans
    # la description d'une offre de direction (équipe encadrée, organisation).
    for term in kw.get("exclude_title", []):
        if _has_word(title, term):
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


def geo_ok(location: str, config: dict) -> bool:
    """Filtre géographique optionnel (`geo_filter` du profil).

    - sans `geo_filter` → tout passe (comportement historique).
    - `geo_filter.soft: true` → tout passe ici : le hors-périmètre n'est plus
      éliminé mais reçoit le malus de `geo_penalty()` après notation.
    - lieu vide ou ambigu → tout passe : le scoring LLM tranche alors avec
      la consigne géographique du prompt.
    - sinon → le lieu doit matcher le motif (ex. codes postaux IDF).
    """
    gf = config.get("geo_filter") or {}
    pattern = gf.get("pattern")
    if not pattern or gf.get("soft") or not (location or "").strip():
        return True
    return re.search(pattern, location) is not None


def geo_penalty(location: str, config: dict) -> int:
    """Malus appliqué après notation d'une offre hors périmètre (`soft`).

    Ne s'applique qu'à un `geo_filter` souple : un filtre dur a déjà écarté
    l'offre en amont. Lieu vide ou ambigu → 0 (le prompt tranche).
    """
    gf = config.get("geo_filter") or {}
    pattern = gf.get("pattern")
    if not gf.get("soft") or not pattern or not (location or "").strip():
        return 0
    if re.search(pattern, location):
        return 0
    return int(gf.get("penalty", 0))


def _has_word(text: str, term: str) -> bool:
    """`term` présent dans `text` (slugué) avec frontières de mot.

    Permet un préfixe libre (géolog → géologue, géologique) mais pas
    une collision au milieu d'un mot : minier ∉ terminierung.
    """
    t = slug(term).strip()
    if not t:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}", text) is not None
