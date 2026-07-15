#!/usr/bin/env python3
"""
Lance l'addon en local, hors Home Assistant.

    python3 dev/serve.py                  # faux RM4 + 7 captures de §9 pré-semées
    python3 dev/serve.py --no-seed        # faux RM4, store vide
    python3 dev/serve.py --device 192.168.0.42   # VRAI RM4, IP explicite
    python3 dev/serve.py --real           # VRAI RM4, découverte broadcast

Le mode par défaut ne touche NI réseau NI matériel : aucune IP à fournir.

Les modes réels permettent de faire tout le reverse depuis la machine de dev,
sans installer l'addon sur HAOS.

ATTENTION SOUS WSL2 : --real ne marchera pas. WSL2 est derrière un NAT, le
broadcast UDP de découverte ne traverse pas jusqu'au LAN. L'unicast, lui, passe :
utilise --device avec l'IP du RM4 (visible dans l'intégration Broadlink de HA,
ou dans le bail DHCP du routeur).

Autre piège (§7) : l'intégration Broadlink de HA garde une session ouverte.
Si tu prends des timeouts pendant les captures, désactive-la le temps du reverse.

L'UI est servie à la racine, comme derrière l'ingress.
"""
import argparse
import json
import logging
import os
import platform
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADDON = os.path.join(ROOT, "rf_lab")
sys.path.insert(0, HERE)      # protocol, fake_broadlink
sys.path.insert(0, ADDON)     # app, decoder
sys.path.insert(0, os.path.join(ROOT, "shared"))

import protocol  # noqa: E402


def main():
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    p.add_argument("--device", metavar="IP",
                   help="IP d'un vrai RM4 (implique --real). Obligatoire sous WSL2.")
    p.add_argument("--real", action="store_true",
                   help="vrai RM4 par découverte broadcast (ne traverse pas le NAT WSL2)")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--frequency", type=float, default=433.92)
    p.add_argument("--store", default=None,
                   help="défaut : dev/.captures.json (faux RM4) "
                        "ou dev/.captures.real.json (vrai RM4)")
    p.add_argument("--no-seed", action="store_true",
                   help="ne pas pré-semer les captures de §9 (faux RM4 uniquement)")
    p.add_argument("--seed-real", action="store_true",
                   help="semer les VRAIES captures RF00234 (dev/fixtures/) + leur "
                        "carte de champs, au lieu du protocole synthétique")
    p.add_argument("--log-level", default="info")
    args = p.parse_args()
    real = args.real or bool(args.device)

    # Stores séparés : les captures synthétiques du faux RM4 n'ont rien à faire
    # dans un vrai reverse, elles pollueraient le diff avec des lignes qui ne
    # correspondent à aucun appui réel sur la télécommande.
    if args.store is None:
        args.store = os.path.join(
            HERE, ".captures.real.json" if real else ".captures.json")

    os.environ["STORE"] = args.store
    os.environ["PORT"] = str(args.port)
    os.environ["FREQUENCY"] = str(args.frequency)
    os.environ["LOG_LEVEL"] = args.log_level
    if args.device:
        os.environ["DEVICE_IP"] = args.device

    if real:
        try:
            import broadlink  # noqa: F401
        except ImportError:
            sys.exit("python-broadlink absent : pip install -r dev/requirements-dev.txt")
        if args.device:
            print(f"→ vrai RM4, unicast vers {args.device}")
        else:
            print("→ vrai RM4, découverte broadcast")
            if "microsoft" in platform.release().lower():
                print("  ⚠ WSL2 détecté : le broadcast ne traversera pas le NAT.")
                print("    Utilise --device <ip> (IP visible dans l'intégration Broadlink de HA).")
    else:
        # Substitution explicite plutôt qu'un shadowing de sys.path : on voit
        # noir sur blanc ce qui remplace quoi.
        import fake_broadlink
        sys.modules["broadlink"] = fake_broadlink
        sys.modules["broadlink.exceptions"] = fake_broadlink.exceptions
        sys.modules["broadlink.const"] = fake_broadlink.const
        print(f"→ faux RM4 (aucun matériel, aucun réseau) @ {fake_broadlink.KNOWN_IP}")

    if args.seed_real:
        import real_seed
        with open(args.store, "w") as fh:
            json.dump(real_seed.store(), fh, indent=2)
        print(f"→ {len(real_seed.store()['captures'])} vraies captures RF00234 semées "
              f"+ carte des champs")
    elif real:
        print("→ store vierge : à toi de capturer (séquence recommandée en §9)")
    elif not args.no_seed and not os.path.exists(args.store):
        with open(args.store, "w") as fh:
            json.dump(protocol.store_seed(), fh, indent=2)
        print(f"→ {len(protocol.SEQ)} captures de §9 semées dans {args.store}")

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    import app
    print(f"→ store  {args.store}")
    print(f"→ UI     http://127.0.0.1:{args.port}/\n")
    app.app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
