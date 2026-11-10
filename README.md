# geo-vigie 🔎

Vigie d'offres d'emploi **géologie / géosciences** — collecte multi-sources,
filtrage par mots-clés, notation d'adéquence par LLM selon un CV, rapport
quotidien par email.

> **Projet personnel.** Indépendant de Hermes et du workspace Cegos.
> Aucun secret Cegos ici : voir `../../sideprojects/hermes/docs/regles-separation.md`.

## Décisions prises (2026-10-05)

| Sujet | Décision | Pourquoi |
|---|---|---|
| Hébergement | **Nano perso** (`~/apps/geo-vigie`) | Service systemd utilisateur déjà actif pour Hermes |
| LinkedIn | **Exclu** | Pas d'API de recherche d'emploi publique en libre-service ; le scraping fait courir le risque de bannissement du compte |
| Rapport | **Email via SMTP externe** | Le nano n'a aucun agent mail (`mail`, `sendmail`, `msmtp` absents) |
| CV | **CV candidat fourni en PDF** | Extrait localement vers `cv/candidate.txt`, ignoré par Git |
| Score | Libellé **« adéquence /100 »** | Une vraie « probabilité d'obtenir le poste » est impossible : on ne connaît ni la concurrence ni le process |

## Bench coût par tâche (2026-10-21)

Méthode : `bench_cost.py` — la **vraie tâche** de scoring (PROMPT de `score.py`),
10 offres réelles × modèle, coût exact par appel (OpenRouter `usage.include`),
même profil, `temperature 0`. Le prix par token ne prédit **pas** le coût par
tâche : ce qui compte, c'est le volume de sortie facturé.

| Modèle | $/tâche | vs MiMo | Latence moy. | Tok. sorties | Fiabilité |
|---|---:|---:|---:|---:|---|
| `google/gemini-3.7-flash` | 0,00292 | 11× plus cher | 5,9 s | 547 | 10/10 |
| **`xiaomi/mimo-v2.6-flash`** | **0,00026** | référence | 5,4 s | 597 | 10/10 |
| `qwen/qwen3.7-flash` | 0,00044 | **1,7× plus cher** | 41,6 s | 2 784 | 9/10 (HTTP 429) |

Conclusions :
- **MiMo gardé** : meilleur coût par tâche ET plus fiable ; aucun benchmark
  public ne le bat sur cette tâche (résultat propre, daté, reproductible).
- **Qwen3.7-flash est plus cher à la tâche malgré des tarifs 2-5× inferieurs**
  : ~2 500 tokens de raisonnement caché facturés en sortie à chaque appel,
  8× plus lent, un 429 sur 10, et scores erratiques (2 décisions inversées au
  seuil `min_score` : offre 94 notée 65 vs 35/35 ; offre 45 notée 15 vs 55/62).
  Absent aussi du leaderboard τ²-Bench Airline (couverture insuffisante).
- **Gemini** reste réservé aux tâches où la synthèse prime — 11× plus cher ici.
- Pour revérifier un candidat modèle : `.venv/bin/python bench_cost.py --models <id>`
  (écrit le détail dans `db/bench-cost-*.txt`, jamais en base).

## Pipeline

```
collect.py  →  score.py  →  report.py
 sources       LLM note      email du jour
  (10)         /100 +        trié par
 mots-clés    justification  score
    ↓              ↓             ↓
 ────────────  SQLite  ──────────────
            db/vigie.sqlite
```

`run.sh` enchaîne les trois.

## Profils (multiprofil — étape 1 posée)

Les règles propres à une personne vivent dans `profiles/<id>/profile.yaml`
(mots-clés, CV, seuils, sujet de mail) ; `config.yaml` ne garde que le
partagé (`general`, `sources`). `common.load_config()` fusionne les deux :

```bash
python collect.py --profile quentin   # défaut : profil unique actif
python score.py   --profile quentin
VIGIE_PROFILE=quentin ./run.sh        # variable alternative
```

Dès **deux profils actifs**, `--profile` (ou `VIGIE_PROFILE`) devient
obligatoire — le robot refuse de deviner. Ajouter un profil = créer
`profiles/<id>/profile.yaml` + `profiles/<id>/cv.txt`. Voir `MULTIPROFIL.md`
pour le plan complet (Gabriela, mailers par profil, baromètre).

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # puis éditer — chmod 600 .env
```

## Utilisation

```bash
source .venv/bin/activate

python collect.py --check    # les sources répondent-elles ? (testé : 10/10 OK)
python collect.py --dry      # simule filtre/matching, sans écrire
python collect.py            # relevé réel → SQLite

python score.py --limit 1    # notation d'1 offre (vérifier le prompt avant d'aller loin)
python score.py              # tout ce qui est en attente

python report.py --preview   # lire le mail sans l'envoyer
python report.py             # envoi réel

./run.sh                     # les 3 d'un coup
./run.sh --preview           # pareil, sans envoyer
```

## Sources

Sources et résultats vérifiés en direct le 2026-10-05 ; `collect.py --check`
relance un contrôle des dix URL actives :

| Source | Langue / zone | Format testé | Observé |
|---|---|---|---|
| **Reed UK — geologist jobs** | Anglais, Royaume-Uni | JSON Next.js embarqué, `/jobs/` autorisé dans robots.txt | 25 cartes ; Engineering, Graduate et Assistant Geologist |
| **EGU — geology** | Anglais, Europe / monde | Cartes HTML du job board | 19 offres avec `limit=50` |
| **EGU — geoscience** | Anglais, Europe / monde | Cartes HTML du job board | 17 offres avec `limit=50` |
| **EGU — mineral exploration** | Anglais, Europe / monde | Cartes HTML du job board | 4 offres ciblées |
| **HelloWork — géologue** | Français, France + Suisse | Cartes de résultats, routes propres autorisées par robots.txt | 20 offres |
| **HelloWork — géologie** | Français, France | Cartes de résultats | 20 offres |
| **France Travail — géologue** | Français, France | Résultats publics par slug, autorisés par robots.txt | 20 cartes |
| Arbeitnow | Europe, multilingue | API JSON | 325 items, généraliste |
| Jobicy | Remote, anglais | API JSON | 50 items, généraliste |
| Remotive | Remote, anglais | API JSON | 17 items, généraliste |

Le test de contenu confirme des cartes d'emploi et des champs de poste réels,
pas seulement un code HTTP `200`. Le `--dry` garde les annonces géologiques
junior tout en rejetant les postes RH/commerciaux qui ne font que mentionner le
secteur minier dans leur description.

**Pistes testées puis non retenues :**

- EURAXESS : page et offres accessibles, mais le filtre `geologist` testé ne
  filtre pas le listing observé ; dix offres récentes hors sujet. Le RSS
  supposé renvoie 404. Ne pas activer avant de résoudre son formulaire POST.
- France Travail : l'API officielle exige un client OAuth (401 sans identifiants).
  En attendant, le robot utilise la page de résultats publique à URL propre,
  autorisée par robots.txt ; l'API reste préférable si l'application est créée.
- Earthworks Jobs : les annonces ont un JSON-LD exploitable, mais ses conditions
  interdisent explicitement les outils automatisés de scraping/navigation
  (§2.7). Source rejetée malgré sa qualité technique.
- Google News : articles et communiqués, pas des offres ; retiré.
- Indeed, APEC, Jooble et plusieurs jobboards : 403. URLs carrières BRGM,
  Andra, Hays, etc. testées : 404 ou réponse sans offre exploitable.
- Talent.com : page HTML dynamique sans annonces géologiques structurées dans
  la réponse reçue ; rejetée.

**LinkedIn : volontairement absent.** Pas d'API de recherche d'emploi publique
en libre-service (la *Job Posting API* sert à **poster** des offres, réservée
aux ATS/distributeurs autorisés ; le *Partner Program* est sur candidature).
Le scraping viole les `API Terms of Use` et peut faire bloquer le compte de
l'intéressé. On privilégie les pages carrières et plateformes qui publient
directement les offres plutôt que de recopier LinkedIn.

### Ajouter une source

1. Vérifier `HTTP 200` avec un `User-Agent` honnête.
2. Regarder la structure réelle de la réponse.
3. Renseigner dans `config.yaml` :

```yaml
- name: ma-source
  type: json           # ou rss
  enabled: true
  url: https://...
  items_path: data     # chemin vers la liste (type json)
  fields:
    title: title       # champ normalisé : nom réel dans la réponse
    company: company_name
    location: location
    url: url
    description: description
    posted_at: created_at
```

4. `python collect.py --check` puis `--dry`.

**Discipline** : `robots.txt`, `User-Agent` identifiable, 1 requête/s,
pas de navigateur headless (RAM).

## Limites connues

- **Sources françaises désormais actives** : deux recherches HelloWork et la
  page publique France Travail. Priorité suivante : API France Travail OAuth
  (plus stable/structurée), puis carrières BRGM dès qu'elles publient des offres
  accessibles — le portail BRGM consulté affichait zéro offre.
- **EURAXESS n'est pas actif** tant que sa recherche n'est pas réellement
  filtrée par mot-clé.
- **Les offres junior sont conservées** : `junior` ne figure pas dans
  `keywords.exclude` ; Reed renvoie notamment `Graduate Geologist`.
- **Scoring** : le CV réel est désormais utilisé. Les rôles professoraux et
  autres annonces de niveau inadéquat sont laissés au score, qui doit les
  écarter selon le profil plutôt que le filtre lexical.
- **429 OpenRouter** : `score.py` retente les erreurs 429/5xx avec backoff
  exponentiel et respecte `Retry-After`.
- **Faux positifs possibles** : arnaques, thèses, concours publics. Le verdict
  `hors-profil` du LLM les devrait les plafonner, mais c'est à surveiller.

## Sécurité / séparation

- `.env` en mode `600`, jamais commité (`.gitignore`).
- **Clé LLM personnelle**, pas une clé de l'autre projet.
- Le CV contient des données personnelles : `cv/*.pdf` et `cv/*.txt` sont
  ignorés par Git. Avant chaque appel LLM, le code retire l'en-tête et le bloc
  Contact (nom, adresse précise, téléphone, email) ; il ne transmet que le
  parcours professionnel anonymisé et la localisation générale France.
- Aucun flux sortant vers un service tiers sauf : sources publiques, provider
  LLM, SMTP.

## Timer systemd (nano perso)

Le timer systemd user exécute la collecte, le scoring et le rapport à intervalle
monotone de **72 heures** (et non `*/3` en cron, qui dérive aux changements de
mois). Le premier lancement a lieu 72 h après l'activation du timer.

```bash
mkdir -p ~/.config/systemd/user
cp ~/apps/geo-vigie/systemd/geo-vigie.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now geo-vigie.timer
systemctl --user list-timers geo-vigie.timer

# Logs du dernier lancement
journalctl --user -u geo-vigie.service -n 100 --no-pager
```

Le timer ne déclenche pas le service immédiatement à l'installation. Il s'appuie
sur systemd user + linger, déjà actif sur le nano pour Hermes. Le crontab existant
(backup Nestor) n'est pas modifié.

## Persistance

`sideprojects/` est ignoré par le `.gitignore` racine du workspace : comme
`hermes/`, **ce projet n'est couvert par aucun commit**. Un dépôt dédié et
privé reste à créer si tu veux du versionné.

### Migration mono-profil → multiprofil

L'ancien schéma stockait le score et la date d'envoi dans `offers` ; la
migration les déplace vers `scores` / `reports` en les attribuant **au
destinataire historique**. Cet historique est retrouvé dans cet ordre :
`VIGIE_LEGACY_PROFILE`, puis le profil actif unique, puis le profil dont le
destinataire correspond au `MAIL_TO` du `.env`.

Sans ce rattachement, les données partent sous un profil `default` invisible
pour les profils actifs : la déduplication est cassée et des offres déjà
envoyées sont renvoyées (incident du 2026-11-10, 7 doublons envoyés à
Quentin). Le programme avertit désormais explicitement dans ce cas.

