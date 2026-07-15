#!/usr/bin/env python3
"""
Les copies de shared/ n'ont pas dérivé.

Le contexte de build Docker d'un addon HA se limite à son dossier : un
COPY ../shared/ est impossible. Les deux addons embarquent donc une copie de
decoder.py et profile.py. Ce test est le garde-fou : sans lui, une correction
dans le labo ne suivrait pas dans le pont, et la divergence ne se verrait qu'au
moment où le ventilo n'obéit plus.

Réparer :  ./dev/sync_shared.sh
"""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True

for f in ("decoder.py", "profile.py"):
    src = os.path.join(ROOT, "shared", f)
    ref = hashlib.sha256(open(src, "rb").read()).hexdigest()
    for addon in ("rf_lab", "rf_bridge"):
        path = os.path.join(ROOT, addon, f)
        if not os.path.exists(path):
            print(f"  ✗ {addon}/{f} manquant")
            ok = False
            continue
        got = hashlib.sha256(open(path, "rb").read()).hexdigest()
        same = got == ref
        ok &= same
        print(f"  {'✓' if same else '✗'} {addon}/{f} identique à shared/"
              + ("" if same else "  — lance ./dev/sync_shared.sh"))

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
