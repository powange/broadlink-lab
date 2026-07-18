#!/usr/bin/env python3
"""
Sécurité du store sous concurrence — waitress sert RF Lab à 8 threads.

Trois propriétés, chacune un vrai bug corrigé :
  1. Deux écritures concurrentes ne se perdent pas (verrou de transaction).
  2. Un fichier temporaire unique par écriture -> pas de corruption entrelacée.
  3. Un store illisible ne rend pas l'addon mort ; il repart propre.

Et le contre-test : SANS le verrou, la perte de données est bien réelle — sinon
le test ne prouverait rien.
"""
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "rf_lab"))

os.environ["STORE"] = tempfile.mktemp(suffix=".json")
import app  # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}{'  — ' + str(extra) if extra else ''}")


def reset_store():
    if os.path.exists(os.environ["STORE"]):
        os.remove(os.environ["STORE"])


# --- 1. le verrou de transaction : N ajouts concurrents, aucun perdu.
# On reproduit exactement ce que fait add_capture, sous le décorateur writes_store.
def add_one(i):
    @app.writes_store
    def body():
        store = app.load_store()
        store["captures"].append({"id": f"c{i}", "name": f"cap{i}", "b64": "z", "meta": {}})
        app.save_store(store)
    body()


reset_store()
N = 40
threads = [threading.Thread(target=add_one, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
final = app.load_store()["captures"]
check(f"{N} écritures concurrentes, aucune perdue (verrou de transaction)",
      len(final) == N, f"{len(final)}/{N}")
check("aucun doublon ni id manquant",
      sorted(c["id"] for c in final) == sorted(f"c{i}" for i in range(N)))

# --- le contre-test : SANS le verrou, on perd des données. Prouve les dents du test.
def add_one_unlocked(i):
    store = app.load_store()
    store["captures"].append({"id": f"u{i}", "name": f"u{i}", "b64": "z", "meta": {}})
    # petit délai pour élargir la fenêtre de course
    for _ in range(2000):
        pass
    app.save_store(store)


reset_store()
threads = [threading.Thread(target=add_one_unlocked, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
lost = len(app.load_store()["captures"])
check("contre-test : SANS verrou, des écritures se perdent (le test a des dents)",
      lost < N, f"{lost}/{N} survivantes")

# --- 2. tmp unique : pas de fichier .tmp à nom fixe qui traîne, store lisible.
reset_store()
threads = [threading.Thread(target=add_one, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("aucun .tmp à nom fixe laissé derrière (nom unique par écriture)",
      not os.path.exists(os.environ["STORE"] + ".tmp"))
# le fichier final est du JSON valide, pas un entrelacement corrompu
with open(os.environ["STORE"]) as fh:
    book = json.load(fh)
check("le store final est du JSON valide (pas d'entrelacement)", "workspaces" in book)

# --- 3. store corrompu : on ne meurt pas, on repart propre, et on garde la preuve.
reset_store()
with open(os.environ["STORE"], "w") as fh:
    fh.write('{"workspaces": {"default": {"captures": [ THIS IS NOT JSON')
book = app.load_book()                      # ne doit PAS lever
check("un store illisible ne fait pas tout planter", "workspaces" in book)
check("le fichier corrompu est mis de côté (.corrupt) pour post-mortem",
      os.path.exists(os.environ["STORE"] + ".corrupt"))

reset_store()
for suffix in (".corrupt",):
    p = os.environ["STORE"] + suffix
    if os.path.exists(p):
        os.remove(p)

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
