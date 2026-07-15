#!/usr/bin/env bash
# Suite de tests locale complète : décodeur, API, UI, export YAML.
# Aucun matériel requis. Usage : ./dev/test.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PORT="${PORT:-8099}"
PY="${PY:-python3}"
STORE="$HERE/.captures.test.json"

[ -x "$HERE/venv/bin/python" ] && PY="$HERE/venv/bin/python"

CANCEL_PORT=$((PORT + 1))
META_PORT=$((PORT + 2))
REAL_PORT=$((PORT + 3))
CANCEL_STORE="$HERE/.captures.cancel.json"
META_STORE="$HERE/.captures.meta.json"
REAL_STORE="$HERE/.captures.seedreal.json"

cleanup() {
  for pid in "${SRV:-}" "${SRV2:-}" "${SRV3:-}" "${SRV4:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null && wait "$pid" 2>/dev/null || true
  done
  rm -f "$STORE" "$CANCEL_STORE" "$META_STORE" "$REAL_STORE"
  rm -rf "$HERE/.profiles" "$HERE/.bridge_state"
}
trap cleanup EXIT

# Un port déjà occupé (typiquement une instance --device laissée tourner) ferait
# passer toute la suite contre le mauvais serveur, en silence et en vert.
for port in "$PORT" "$CANCEL_PORT" "$META_PORT" "$REAL_PORT"; do
  if curl -sf -m 2 "http://127.0.0.1:$port/api/status" >/dev/null 2>&1; then
    echo "✗ le port $port est déjà utilisé par un autre serveur RF Lab."
    echo "  Arrête-le d'abord :  pkill -f dev/serve.py"
    echo "  (ou lance la suite sur d'autres ports :  PORT=8200 ./dev/test.sh)"
    exit 1
  fi
done

wait_up() {  # $1 = port, $2 = pid
  for _ in $(seq 1 40); do
    curl -sf "http://127.0.0.1:$1/api/status" >/dev/null 2>&1 && return 0
    kill -0 "$2" 2>/dev/null || { echo "le serveur est mort :"; cat "$HERE/.serve.log"; exit 1; }
    sleep 0.25
  done
  echo "le serveur ne répond pas sur :$1"; cat "$HERE/.serve.log"; exit 1
}

echo "=== 1. copies de shared/ (anti-dérive entre les 2 addons) ==="
"$PY" "$HERE/shared_test.py"

echo
echo "=== 2. décodeur (round-trip §9, sans dépendances) ==="
"$PY" "$ROOT/rf_lab/test_decoder.py"

echo
echo "=== 3. signal RÉEL (captures RF00234 via RM4 Pro) ==="
"$PY" "$HERE/real_test.py"

echo
echo "=== 4. protocole synthétique ==="
"$PY" "$HERE/protocol.py"

echo
echo "=== 5. serveur local (faux RM4) ==="
rm -f "$STORE"
"$PY" "$HERE/serve.py" --port "$PORT" --store "$STORE" > "$HERE/.serve.log" 2>&1 &
SRV=$!
wait_up "$PORT" "$SRV"
echo "  ✓ /api/status répond sur :$PORT"

# 2e instance où personne n'appuie jamais sur la télécommande : c'est la seule
# façon de tester l'annulation et le timeout (l'autre rend une trame en 0,8 s).
FAKE_LATENCY_POLLS=9999 "$PY" "$HERE/serve.py" --port "$CANCEL_PORT" \
  --store "$CANCEL_STORE" > "$HERE/.serve2.log" 2>&1 &
SRV2=$!
wait_up "$CANCEL_PORT" "$SRV2"
echo "  ✓ instance « personne n'appuie » sur :$CANCEL_PORT"

# 3e instance, store vierge : expose le schéma de métadonnées par défaut
# (celui de la vraie RF00234), que le seed du protocole de test remplacerait.
# FAKE_HELLO_DELAY : une mauvaise IP doit durer, sinon l'annulation de connexion
# n'est pas testable (device_test.mjs). N'affecte que les IP inconnues.
rm -f "$META_STORE"
FAKE_HELLO_DELAY=2 "$PY" "$HERE/serve.py" --port "$META_PORT" --no-seed \
  --store "$META_STORE" > "$HERE/.serve3.log" 2>&1 &
SRV3=$!
wait_up "$META_PORT" "$SRV3"
echo "  ✓ instance « store vierge » sur :$META_PORT"

# 4e instance : les VRAIES captures RF00234 + leur carte de champs. L'export du
# package HA et /api/set supposent ce protocole-là — les exercer sur le protocole
# synthétique (24 bits, ni lumière ni ventilo) n'aurait aucun sens.
"$PY" "$HERE/serve.py" --port "$REAL_PORT" --seed-real \
  --store "$REAL_STORE" > "$HERE/.serve4.log" 2>&1 &
SRV4=$!
wait_up "$REAL_PORT" "$SRV4"
echo "  ✓ instance « vraies captures RF00234 » sur :$REAL_PORT"

if [ ! -d "$HERE/node_modules" ]; then
  echo "  … installation de jsdom"
  (cd "$HERE" && npm install --silent)
fi

echo
echo "=== 6. IP du Broadlink configurable depuis l'UI ==="
node "$HERE/device_test.mjs" "http://127.0.0.1:$META_PORT/"

echo
echo "=== 7. UI de bout en bout ==="
node "$HERE/ui_test.mjs" "http://127.0.0.1:$PORT/"

echo
echo "=== 8. annulation de capture ==="
node "$HERE/cancel_test.mjs" "http://127.0.0.1:$CANCEL_PORT/"

echo
echo "=== 9. paramètres d'état configurables ==="
node "$HERE/meta_test.mjs" "http://127.0.0.1:$META_PORT/"

echo
echo "=== 10. profil d'appareil (sur vrai signal) ==="
node "$HERE/profile_test.mjs" "http://127.0.0.1:$REAL_PORT/"

echo
echo "=== 11. RF Bridge : MQTT discovery + commandes -> trames ==="
"$PY" "$HERE/bridge_test.py"

echo
echo "✓ tout est vert"
