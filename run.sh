#!/usr/bin/env bash
# geo-vigie — pipeline complet : collecte → notation → rapport.
# Usage : ./run.sh [--preview] [--limit N] [--profile ID]
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
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
done

echo "== 1/3 Collecte =="
# La collecte est partagée entre profils : sans --profile elle écrit le
# catalogue global (mots-clés de tous les profils actifs à l'étape 3).
python collect.py $PROFILE

echo
echo "== 2/3 Notation =="
# On borne si --limit est passé, sinon on note tout ce qui est en attente.
python score.py $LIMIT $PROFILE

echo
echo "== 3/3 Rapport =="
python report.py $PREVIEW $PROFILE
