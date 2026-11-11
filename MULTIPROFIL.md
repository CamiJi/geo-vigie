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
geo_filter:                       # MALUS hors périmètre (souple, 10/11)
  soft: true
  pattern: '(?i)\b(?:75|77|78|9[1-5]|28|27|60)\b|paris|île-de-france'
  penalty: 25                     # points retirés APRÈS notation
location:
  base: "Fontenay-le-Fleury (78)"   # injectée au prompt, jamais le nom
  zones: ["Île-de-France", "couronne ~40 km (28, 27, 60)"]
  remote: accepte
  relocation: non
keywords:
  include_strong: [...]
  include_weak: [...]
  exclude: [...]          # partout (titre, description, lieu)
  exclude_title: [...]    # TITRE seulement (postes moindres, ex. responsable de salle)
scoring:
  min_salary_net_month: 2200        # plancher
  salary_signal: tail               # salaire absent = neutre, mention en fin
  min_score: 55
  top_n: 15
report:
  subject: "[vigie-gabriela] {count} offre(s) — {date}"
mailer:
  to: adresse-gabriela@...
  cc: camille@...
```

Sujet porté par `report.subject` (clé historique de l'étape 1, source
unique) ; `mailer.to` / `mailer.cc` portent les destinataires, avec
repli sur `MAIL_TO` / `MAIL_CC` du `.env` quand `mailer` est absent.

`geo_filter` a deux modes. **Dur** (défaut) : `pattern` est appliqué à
la collecte (aucune paire offre/profil hors zone, compteur `geo_out`),
à la notation (zéro appel LLM hors zone) et au rapport. **Souple**
(`soft: true`, mode de Gabriela depuis le 10/11) : tout est collecté et
noté, puis `common.geo_penalty()` retire `penalty` points aux offres hors
périmètre — elles restent donc visibles si elles sont excellentes. Lieu
vide ou ambigu → ni filtre ni malus, le prompt tranche.

### Recadrage produit du 2026-10-21 (après le premier mail — Camille)

1. **Filtre géo dur IDF** — avant le LLM : hors Île-de-France = éliminé
   de facto (`geo_filter`, cf. ci-dessus). Bilan : **91 des 127 offres
   matchées (72 %) hors périmètre** (Giessen, Beijing, Lausanne,
   Megève…) écartées sans coût de notation.
2. **Niveau de poste** — Gabriela est directrice adjointe d'hôtel : tout
   poste hiérarchiquement moindre (responsable de salle, chef de rang,
   commis…) = dégradation → plafond 30 / hors-profil. Postes équivalents
   ou supérieurs requis (règle `Niveau de poste` + `exclude_title`).
   Les directions non opérationnelles (commercial, corporate, revenue)
   restent pertinentes si la trajectoire de direction hôtelier est plausible.
3. **Salaire secondaire** — l'absence de salaire n'est **pas** un signal
   (`salary_signal: tail` : mention neutre en fin de justification) ;
   le critère premier est la qualification. Un salaire sous le plancher
   reste éliminatoire (plafond 30).

### Recadrage produit du 2026-11-10 (retour d'expérience — Camille)

Aucun mail utile en trois semaines malgré un pipeline sain. Diagnostic :
le filtre dur du 21/10 s'appliquait **après** des sources nationales
dont seule la page 1 était lue (`?l=78` et `lieux=` sont ignorés en HTML
public). Bilan mesuré sur un relevé : **82 offres matchées éliminées par
le seul filtre géo**, dont ~30 postes de direction hôtelière (Rennes,
Nantes, Bayonne, Montpellier…), pour **3 postes de direction hôtelière en
IDF** dans tout le catalogue ; sur 8 offres notées, 1 seule ≥ 55.

Décisions :

1. **Filtre dur → malus** (`geo_filter.soft: true`, `penalty: 25`) sur un
   périmètre IDF + couronne ~40 km (28, 27, 60). La géographie sort de la
   note du LLM (pondération 50 / 25 / 25 métier-niveau-secteur) : le malus
   est mécanique, donc reproductible.
2. **4 sources de direction** ajoutées (HelloWork : directeur
   d'établissement, adjoint de direction hôtel, directeur de résidence
   hôtelière, responsable hôtelier) — 5 à 7 cartes IDF sur 20 chacune,
   mesuré depuis le serveur.
3. **Bruit d'exécution** coupé sur le titre (barman, concierge, second de
   cuisine, gouvernant, agent hôtelier, technicien), mots-clés EHPAD /
   médico-social / résidence services ajoutés.

Résultat : **15 offres ≥ 55** au premier mail (Paris, 77, 78, 93, 95,
dont adjoints de direction, responsables hôteliers et directions
d'établissement), contre 1 auparavant. Reste à faire : API France Travail
OAuth (`commune` + `distance`) pour remplacer la page nationale page 1.

### Recadrage produit du 2026-11-11 (géolocalisation — Camille)

Le malus seul laissait passer des offres à 60-70 km (score brut 80+ → 55+
après −25). Exemples réels : Vert-Saint-Denis 51 km, Neufmoutiers 58 km,
Rennes 285 km, Les Sables 382 km, Uvernet-Fours 611 km — 8 offres envoyées
à Gabriela entre le 10 et le 11/11, toutes au-delà du périmètre souhaité.

Ajout du géocodage BAN (Base Adresse Nationale, sans clé) : distance
réelle en km depuis Fontenay-le-Fleury, stockée dans `offers.lat/lon`.
`geo_filter.max_km: 45` → coupure dure au-delà (sauf mention de
télétravail/remote dans le lieu). Malus gradué dans le périmètre :
≤ 15 km → 0, 15-30 → 5, 30-45 → 10. Quentin n'a toujours aucun
`geo_filter` : il est mobile.

Résultat : 14 offres ≥ 55 hors périmètre éliminées, 9 conservées dans un
rayon de 45 km. Plus aucun envoi d'offre à 300+ km.

Re-scoring complet de Gabriela (36 offres nettes, 0 échec) et **étude
renvoyée** le 2026-10-22 : 3 offres ≥ 55 (Revenue Manager TOO Hôtel,
Directeur Room Service Fouquet's, Key Accounts Airbnb — tous Paris, tous
arbitrés sur le niveau vs direction adjointe).

## Étapes

| # | Étape | Livrable / validation |
|---|---|---|
| 1 | Restructuration config en `profiles/` | `profiles/quentin/profile.yaml` = copie conforme de l'actuel ; pipeline behave à l'identique |
| 2 | Scission DB (`scores`, `reports`) | Migration SQLite : scores existants → profil `quentin` |
| 3 | Collecte partagée, matching par profil | Une offre stockée si ≥ 1 profil matche ; paires (offre, profil) enregistrées pour scoring |
| 4 | Sources hôtellerie / management | ✅ fait (commit `7da1488`) : balayage 2026-10-21 + fetchers `lhotellerie` / `michaelpage`, RSS JdP avec `max_items: 50`, requêtes FT/HelloWork — 16/16 sources OK |
| 5 | `score.py` multi-profils | ✅ fait : persona, geo et **plancher salarial** injectés depuis le profile.yaml (`build_prompt`) ; prompt quentin byte-identique ; 41/41 tests |
| 6 | `report.py` multi-mailers + baromètre | ✅ fait : destinataires `mailer.to/cc` par profil (fallback `.env`), section **baromètre** volumes/évolution + scores dans le mail (text et HTML) |
|  7 | Orchestration | ✅ fait : `run.sh [--profile ID \| --all]` (défaut = tous les profils actifs, le timer sans argument reste sûr) ; runs tagués par profil (`profile=gabriela ok=2 ko=0`, collect : `matchs gabriela=61, quentin=58`) |
| 8 | CV Gabriela | ✅ fait : `profiles/gabriela/cv.txt` synthétisé depuis le LinkedIn brut (sans les posts), anonymisé (marqueur `Languages`) |
| 9 | Recette | ✅ fait le 2026-10-21 : `--check` (16 sources), `--dry` (gabriela=82 / quentin=79), `score --limit 1` par profil, `report --preview` par profil, `./run.sh --preview --limit 2` de bout en bout — **reste** : premier run du timer sur la machine cible (pas de systemd dans cet environnement) |

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

- **France Travail en mode shell (observé le 21/10 après-midi, résolu)** : pendant la
  recette, les 2 sources FT basculent d'un HTML server-renderé (20-92 cards
  le matin) vers un shell SPA « secours » de 7 Ko pour toute requête sous
  `/offres/` — 0 card, quel que soit l'UA (honnête ou navigateur) ou les
  entêtes. Dégradation transitoire (déploiement FT) : **retour au normal le
  22/10** (3 + 1 offres au run du matin). Le collecte tolère (0 item =
  0 offre) et `--check` alerte désormais sur une source à 0 élément
  (`VIDE … ⚠`, exit 1) — utile pour ce type d'incident.

- **Volume/bruit** : les sources généralistes remontent énormément d'offres
  hôtelières (souvent sous le plancher salarial) → calibrage `--dry` strict et
  keywords titre-first pour limiter le coût LLM.
- **Salaire rarement annoncé** en hôtellerie : le plancher n'est applicable
  que si l'offre le mentionne ; le reste est arbitré par le persona.
- **Sources 403** fréquentes (comme documenté pour Indeed/APEC) : tester avant
  d'ajouter, ne pas forcer.
- **geo-vigie n'a pas de remote Git** : sauvegarde/versionning à organiser
  (le README le signale déjà).
