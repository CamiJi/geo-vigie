#!/usr/bin/env bash
# geo-vigie — pipeline complet : collecte → notation → rapport.
# Usage : ./run.sh [--preview] [--limit N] [--profile ID | --all]
#   sans --profile ni --all : tous les profils actifs (--all explicite,
#   c'est aussi le défaut — le timer systemd appelle run.sh sans argument).
set -euo pipefail

cd "$(dirname "$0")"
source .venv/bin/activate

PREVIEW=""
LIMIT=""
PROFILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --preview)  PREVIEW="--preview"; shift ;;
    --limit)    LIMIT="--limit $2"; shift 2 ;;
    --profile)  PROFILE="--profile $2"; shift 2 ;;
    --all)      PROFILE=""; shift ;;
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
done

echo "== 1/3 Collecte =="
# La collecte est partagée entre profils : sans --profile elle écrit le
# catalogue global (mots-clés de tous les profils actifs à l'étape 3).
python collect.py $PROFILE

echo
echo "== 2/3 Notation =="
# score.py exige un choix dès deux profils actifs : sans --profile, on
# note chaque profil actif (le timer systemd n'appelle run.sh sans args).
if [[ -n "$PROFILE" ]]; then
  python score.py $LIMIT $PROFILE
else
  for p in $(python -c "from common import active_profile_ids; print(' '.join(active_profile_ids()))"); do
    python score.py $LIMIT --profile "$p"
  done
fi

echo
echo "== 3/3 Rapport =="
if [[ -n "$PROFILE" ]]; then
  python report.py $PREVIEW $PROFILE
else
  for p in $(python -c "from common import active_profile_ids; print(' '.join(active_profile_ids()))"); do
    python report.py $PREVIEW --profile "$p"
  done
fi
