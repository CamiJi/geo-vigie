# geo-vigie — passage multiprofil (cadrage 2026-10-21)

Objectif : transformer la vigie mono-profil (géologie/Quentin) en **robot
multiprofil** — chaque profil a ses mots-clés, sa géographie, son plancher
salarial, son persona de scoring et son mailer. Les offres sont collectées une
seule fois et partagées ; les scores et les mails sont des vues par profil.

Profils prévus : `quentin` (référence, migration à l'identique), `gabriela`,
puis extension libre.

---

## Décisions produit (validées 2026-10-21)

| Sujet | Décision | Détail |
|---|---|---|
| Géographie (Gabriela) | **Île-de-France** comme base, **remote accepté** | Le scoring geo est une contrainte injectée dans le prompt, pas un filtre dur. Les offres hors IDF ne sont pas supprimées d'office : si le poste est remote et convaincant, le score peut rester haut. |
| Géolocalisation fine | **Prolongation** (pas dans la V1) | Plus tard : géocoder l'annonce et appliquer un périmètre max (ex. 30 km à vol d'oiseau depuis Fontenay-le-Fleury). |
| Cible de poste | **Management / service / hôtellerie-restauration**, sans interdire le **changement de secteur** | Qualités transversales (management, accueil, responsabilité, process) valorisées. Un poste en entreprise, télétravail proche de chez eux, justifie un changement de secteur. Le persona LLM doit être explicite là-dessus. |
| Rémunération | **Plancher : 2 200 € net/mois** (charges fixes, crédit) | Sous ce plancher → plafonnement du score (verdict hors-profil, plafond ~30). Si le salaire n'est **pas mentionné** : ne pas pénaliser (sinon tout disparaîtrait) mais le signaler dans la justification. Fourchette indicative : 2 200 € net/mois ≈ 31 k€ net/an ≈ 38-40 k€ brut/an. |
| Mail (Gabriela) | Comme Quentin : **elle reçoit sur sa boîte, Camille en copie** | Même SMTP Brevo global. `to` / `cc` dans le profil. Adresse de Gabriela à renseigner avant la recette. |
| Posture d'usage | **Sonder le marché**, pas recherche captive | L'outil doit détecter une **accélération ou un ralentissement** des offres. → section **baromètre** dans le mail : volume d'offres par relevé, évolution récente, scores moyens. L'historique existe déjà dans SQLite. |
| Fréquence | 72 h (inchangé) | Suffisant pour un sondage. Révisable. |

## Prolongations (hors périmètre immédiat)

1. **Géocodage d'annonce + périmètre max** : géocoder le lieu de l'offre
   (API gratuite ex. Base Adresse Nationale / Nominatim), calcul vol d'oiseau
   depuis la position du profil, filtre ou bonus/malus de score.
2. **Sas payant à petit prix** : copier-coller d'un profil LinkedIn ou upload
   d'un CV → création instantanée d'une vigie (SaaS multi-tenant léger).
   Nécessitera isolation par tenant, paiement, onboarding automatisé.

---

## Architecture cible

```
config.yaml              # partagé : general, sources, llm (globaux)
profiles/
  quentin/
    profile.yaml         # keywords, geo, plancher salarial, scoring, mailer, persona
    cv.txt               # gitignoré
  gabriela/
    profile.yaml
    cv.txt
collect.py               # 1 collecte, matching par profil (au moins 1 match → stockée)
score.py                 # note chaque paire (offre, profil) validée, persona par profil
report.py                # mail par profil + section baromètre
run.sh [--profile X|--all]
db/vigie.sqlite
  offers                 # catalogue partagé (inchangé, fingerprint titre+lieu)
  scores                 # (offer_id, profile_id, score, reason, model, scored_at) UNIQUE
  reports                # (offer_id, profile_id, sent_at) UNIQUE
  runs                   # inchangé, détail taggé par profil
```

### profile.yaml (contrat)

```yaml
id: gabriela
label: "Gabriela S."
active: true
cv_file: cv.txt
persona: >-          # 1 phrase injectée dans le prompt de scoring
  Recruteur senior en hôtellerie-restauration et management d'établissements.
location:
  base: "Fontenay-le-Fleury (78)"   # injectée au prompt, jamais le nom
  zones: ["Île-de-France"]
  remote: accepte
  relocation: non
keywords:
  include_strong: [...]
  include_weak: [...]
  exclude: [...]
scoring:
  min_salary_net_month: 2200        # plancher
  min_score: 55
  top_n: 15
mailer:
  to: adresse-gabriela@...
  cc: camille@...
  subject: "[vigie-gabriela] {count} offre(s) — {date}"
```

## Étapes

| # | Étape | Livrable / validation |
|---|---|---|
| 1 | Restructuration config en `profiles/` | `profiles/quentin/profile.yaml` = copie conforme de l'actuel ; pipeline behave à l'identique |
| 2 | Scission DB (`scores`, `reports`) | Migration SQLite : scores existants → profil `quentin` |
| 3 | Collecte partagée, matching par profil | Une offre stockée si ≥ 1 profil matche ; paires (offre, profil) enregistrées pour scoring |
| 4 | Sources hôtellerie / management | HelloWork, France Travail (slugs publics), Welcome to the Jungle… — discipline robots.txt + UA + 1 req/s, testées 1 par 1 |
| 5 | `score.py` multi-profils | Persona, CV et contraintes géo par profil ; **plancher salarial** appliqué dans le prompt |
| 6 | `report.py` multi-mailers + baromètre | Mail Gabriela (Camille en cc) ; section volumes/évolution |
|  7 | Orchestration | `run.sh --all`, systemd timer inchangé, runs tagués par profil |
| 8 | CV Gabriela | `profiles/gabriela/cv.txt` synthétisé depuis le LinkedIn brut (sans les posts), anonymisé |
| 9 | Recette | `--check`, `--dry` (calibrage keywords), `score --limit 1`, `report --preview`, premier run timer |

## Risques

- **Volume/bruit** : les sources généralistes remontent énormément d'offres
  hôtelières (souvent sous le plancher salarial) → calibrage `--dry` strict et
  keywords titre-first pour limiter le coût LLM.
- **Salaire rarement annoncé** en hôtellerie : le plancher n'est applicable
  que si l'offre le mentionne ; le reste est arbitré par le persona.
- **Sources 403** fréquentes (comme documenté pour Indeed/APEC) : tester avant
  d'ajouter, ne pas forcer.
- **geo-vigie n'a pas de remote Git** : sauvegarde/versionning à organiser
  (le README le signale déjà).
