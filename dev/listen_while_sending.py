#!/usr/bin/env python3
"""
Le RM4 peut-il écouter et émettre à la fois ?

Non, pas « à la fois » : un seul émetteur-récepteur, donc du half-duplex, et un
émetteur sature de toute façon son propre récepteur. La vraie question est
ailleurs : **l'écoute survit-elle à une émission**, ou faut-il la réarmer ?

Ça décide d'une fonctionnalité qui vaut cher — écouter en permanence pour suivre
la VRAIE télécommande. Aujourd'hui l'état de Home Assistant est optimiste : c'est
ce qu'il a demandé, pas ce que la machine fait, et un appui sur la télécommande
physique le désynchronise pour toujours. Si le pont pouvait écouter entre deux
commandes, il suivrait.

`check_data()` ne suffit pas à répondre : il rend « storage full » aussi bien
quand le RM4 écoute sans rien avoir reçu que quand il n'écoute plus du tout. Le
MÊME code dans les deux cas. D'où ce test, qui a besoin d'une main sur la
télécommande.

    dev/venv/bin/python dev/listen_while_sending.py --device 192.168.0.42
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))

import broadlink                       # noqa: E402
import decoder                         # noqa: E402
import discover                        # noqa: E402


def a_frame():
    """Une vraie trame à émettre — celle de la R00143, pour rester réaliste."""
    fix = json.load(open(os.path.join(HERE, "fixtures", "real_rf00143.json")))
    p = decoder.decode_packet(fix["captures"][0]["b64"])
    return decoder.encode_packet(p["durations"], p["header"], p["repeats"],
                                 p["terminator"])


def listen(dev, seconds, label):
    """Arme l'écoute et attend une trame. Retourne les octets, ou None."""
    dev.find_rf_packet(frequency=433.92)
    print(f"\n{label}\n  APPUIE SUR LA TÉLÉCOMMANDE ({seconds:.0f} s)…")
    # monotonic : l'horloge murale saute, un bond couperait l'attente net
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            return dev.check_data()
        except Exception:                  # noqa: BLE001
            time.sleep(0.4)                # « storage full » = rien encore
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", required=True, help="IP du Broadlink")
    ap.add_argument("--wait", type=float, default=25.0)
    args = ap.parse_args()

    dev = broadlink.hello(args.device, timeout=discover.DEFAULT_TIMEOUT)
    dev.auth()
    print(f"RM4 : {dev.model} @ {dev.host[0]}")
    print("Arrête le serveur de dev d'abord (il garde une session ouverte).")

    # --- témoin : sans émission, l'écoute marche-t-elle ?
    got = listen(dev, args.wait, "1. TÉMOIN — écoute simple, aucune émission")
    if not got:
        print("  ✗ rien reçu même sans émettre : le test ne peut rien conclure.")
        print("    Rapproche la télécommande du RM4 et recommence.")
        return 1
    print(f"  ✓ trame reçue ({len(got)} octets) — l'écoute fonctionne")

    # --- le vrai test : on émet PENDANT que le RM4 écoute
    dev.find_rf_packet(frequency=433.92)
    print("\n2. LE TEST — le RM4 écoute, et on lui fait émettre une trame")
    dev.send_data(a_frame())
    print("  ✓ send_data accepté (il ne refuse jamais)")
    print(f"  APPUIE SUR LA TÉLÉCOMMANDE ({args.wait:.0f} s)…")
    deadline = time.monotonic() + args.wait
    after = None
    while time.monotonic() < deadline:
        try:
            after = dev.check_data()
            break
        except Exception:                  # noqa: BLE001
            time.sleep(0.4)

    print()
    if after:
        print("  ✓ TRAME REÇUE APRÈS L'ÉMISSION — l'écoute a SURVÉCU.")
        print("    Le pont pourrait donc écouter en permanence et suivre la vraie")
        print("    télécommande : ce serait la fin de l'état optimiste, la plus")
        print("    grosse limite du projet.")
    else:
        print("  ✗ RIEN — l'émission a coupé l'écoute (ou l'a désarmée).")
        print("    Pas rédhibitoire : il suffirait de RÉARMER après chaque")
        print("    émission. Le pont n'émet qu'en réponse à une commande, donc")
        print("    la fenêtre d'aveuglement serait de quelques dizaines de ms.")
    try:
        dev.cancel_sweep_frequency()
    except Exception:                      # noqa: BLE001
        pass
    print("\n(le RM4 est ressorti du mode écoute)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
