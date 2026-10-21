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
| 4 | Sources hôtellerie / management | ✅ fait (commit `7da1488`) : balayage 2026-10-21 + fetchers `lhotellerie` / `michaelpage`, RSS JdP avec `max_items: 50`, requêtes FT/HelloWork — 16/16 sources OK |
| 5 | `score.py` multi-profils | ✅ fait : persona, geo et **plancher salarial** injectés depuis le profile.yaml (`build_prompt`) ; prompt quentin byte-identique ; 41/41 tests |
| 6 | `report.py` multi-mailers + baromètre | Mail Gabriela (Camille en cc) ; section volumes/évolution |
|  7 | Orchestration | `run.sh --all`, systemd timer inchangé, runs tagués par profil — **partiel** : `run.sh` sans `--profile` note et rédige déjà chaque profil actif (sécurité timer, fait à l'étape 5) |
| 8 | CV Gabriela | ✅ fait : `profiles/gabriela/cv.txt` synthétisé depuis le LinkedIn brut (sans les posts), anonymisé (marqueur `Languages`) |
| 9 | Recette | `--check`, `--dry` (calibrage keywords), `score --limit 1`, `report --preview`, premier run timer |

### Bug connu préexistant (découvert à l'étape 5, à trancher)

`keyword_match` applique aux `exclude` une frontière de mot **à gauche
seulement** : le terme `intern` de quentin exclut toute offre contenant
« **intern**ationale » (titre, description ou lieu) → pertes silencieuses
d'offres géo crédibles. Démo :

```
"Géologue confirmé - Mission internationale" + "déplacements internationaux"
  → (False, '')   # écarté par `intern`
```

Piste de correction : frontière **droite** pour les `exclude` (formes
explicites `stages`, `stagiaires`, `alternances`, `interns`,
`apprentissage`, `pédagogue…` — les troncatures `pédago` / `apprentiss`
seraient dépliées). Gabriela évite le piège en n'utilisant pas `intern`.

## Balayage des sources candidates pour Gabriela (2026-10-21)

Liste fournie par Camille (27 domaines hors LinkedIn) — testées en 3 phases
joignabilité → URL de recherche → structure HTML.

**Validées — fetchers codés à l'étape 4 (commit `7da1488`) :**

| Source | Type | Structure relevée |
|---|---|---|
| `lhotellerie-restauration.fr/emploi/emploi-hotel-restaurant` | HTML cards | `div.multiple-items-3.mb-4` : titre, lieu, extrait, lien `/emploi/<id>/` — pagination `?page=2` OK, 2 183 offres, filtres région (IDF) |
| `michaelpage.fr/jobs` | HTML cards | `div.job-tile.search-job-tile` : titre, lieu, contrat, **salaire affiché (50-60 k€/an)**, lien `/job-detail/…/ref/…` → plancher salarial exploitable au scoring |
| `journaldespalaces.com/carriere/feed/rss.xml` | RSS (type déjà supporté) | offres hôtellerie de luxe réelles, quasi gratuit à intégrer |

**Requêtes à ajouter sur les sources déjà actives** (réutilisent les
fetchers existants) : France Travail + HelloWork avec mots-clés hôtellerie
(`directeur hôtel`, `responsable établissement`, `revenue manager`…).

**Bloquées / mortes (confirmé, à ne pas retester)** :
- 403 ou challenge JS : APEC, Indeed, Jooble, Glassdoor, Cadremploi,
  eFinancialCareers, Adzuna (site), hospitality-on (section emploi).
- DNS/domaine inexistant : jobtourisme, jobseko, place-emploi-public,
  jobs.compta-online, dafmag (SSL), cadresmploi.
- SPA/JS sans listing HTML (**démontré le 21/10, pas supposé**) : HTML brut
  sans une seule fiche (WTTJ 556 ko vides, Caterer, Robert Half → page
  catégories), 29 bundles JS scannés sans endpoint de recherche d'offres
  (seuls des endpoints utilisateurs/sessions), `sitemap.xml` 404 (Caterer),
  aucun formulaire GET (RH), endpoints devinés → 404.
  → **Réouvrables** avec l'URL XHR réelle relevée manuellement une fois dans
  l'onglet Réseau du navigateur (WTTJ priorité 1 : management IDF + salaires).
- LinkedIn : exclu ToS (décision initiale du projet).
- API Adzuna : injoignable depuis ce réseau + clé gratuite requise → piste reportée.

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
