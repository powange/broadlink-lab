#!/usr/bin/env bash
# shared/ est la source de vérité. Les deux addons en embarquent une copie parce
# que le contexte de build Docker d'un addon HA se limite à son propre dossier :
# un COPY ../shared/ est impossible. dev/test.sh vérifie qu'elles n'ont pas dérivé.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# discover.py n'est utile qu'au labo : le pont émet, il ne cherche pas.
for addon in rf_lab rf_bridge; do
  FILES="decoder.py profile.py"
  [ "$addon" = "rf_lab" ] && FILES="$FILES discover.py"
  for f in $FILES; do
    cp "$ROOT/shared/$f" "$ROOT/$addon/$f"
    echo "  shared/$f -> $addon/$f"
  done
done
