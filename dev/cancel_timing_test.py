#!/usr/bin/env python3
"""
L'annulation coupe-t-elle VRAIMENT court, ou attend-elle les 3 essais ?

Testé ici plutôt que dans l'UI parce qu'il faut un environnement maîtrisé : la
mesure d'horloge murale n'a de sens que si rien d'autre ne tourne. Sous WSL2,
dans la suite complète, un time.sleep(2) a été mesuré à 31 s — toute assertion
de latence y devient ininterprétable.

On appelle get_device() directement, sans serveur ni navigateur.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "shared"))

os.environ.update(FAKE_HELLO_DELAY="1", STORE="/tmp/.ct.json",
                  DEVICE_IP="192.168.0.254")
import fake_broadlink
sys.modules["broadlink"] = fake_broadlink
sys.modules["broadlink.exceptions"] = fake_broadlink.exceptions
sys.modules["broadlink.const"] = fake_broadlink.const
sys.path.insert(0, os.path.join(ROOT, "rf_lab"))
import app  # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}{'  — ' + str(extra) if extra else ''}")


# 3 essais × 1 s = 3 s si on laisse faire
app._connect_cancel.clear()
t0 = time.monotonic()
try:
    app.get_device(force=True)
except Exception as exc:
    full = time.monotonic() - t0
    check("sans annulation : les 3 essais sont faits", full >= 2.8, f"{full:.1f}s")
    check("le message dit combien d'essais et suggère la bonne piste",
          "3 essais" in str(exc) and "aliment" in str(exc), str(exc)[-40:])

# avec annulation à mi-parcours : on doit sortir AVANT la fin des 3 essais
app._connect_cancel.clear()
app._dev = None
threading.Timer(1.2, app._connect_cancel.set).start()
t0 = time.monotonic()
try:
    app.get_device(force=True)
    check("annulé -> lève une erreur", False, "aucune exception")
except Exception as exc:
    dt = time.monotonic() - t0
    check("l'annulation coupe court", dt < full - 0.5, f"{dt:.1f}s au lieu de {full:.1f}s")
    check("elle le dit explicitement", "annulée" in str(exc), str(exc))

# le drapeau ne doit pas rester armé : la connexion suivante repartirait morte
app._connect_cancel.clear()
app._dev = None
os.environ["DEVICE_IP"] = "192.168.0.99"
app.DEVICE_IP_OPTION = "192.168.0.99"
try:
    d = app.get_device(force=True)
    check("après annulation, une nouvelle connexion repart", d.model.startswith("RM4"), d.model)
except Exception as exc:
    check("après annulation, une nouvelle connexion repart", False, exc)

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
