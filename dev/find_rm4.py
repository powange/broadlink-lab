#!/usr/bin/env python3
"""
Retrouve l'IP d'un Broadlink sur le LAN, depuis WSL2.

    python3 dev/find_rm4.py                 # balaie 192.168.0.0/24
    python3 dev/find_rm4.py 192.168.1.0/24

La logique est dans shared/discover.py — l'UI de RF Lab s'en sert aussi
(bouton « Chercher » de la barre d'état). Ce script n'est qu'un CLI par-dessus,
pratique quand l'addon n'est pas encore installé.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))

try:
    import discover
except ImportError:
    sys.exit("python-broadlink absent : pip install -r dev/requirements-dev.txt")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cidr", nargs="?", default="192.168.0.0/24")
    p.add_argument("--timeout", type=float, default=discover.DEFAULT_TIMEOUT,
                   help="par adresse. Ne pas descendre sous 6 s : ces appareils "
                        "dorment, un timeout court produit un faux négatif (§8)")
    p.add_argument("--workers", type=int, default=64)
    args = p.parse_args()

    print(f"→ balayage unicast de {args.cidr}…\n")
    found = discover.sweep(args.cidr, args.timeout, args.workers)
    for d in found:
        rf = "RF 433 ✓" if d["rf"] else "IR seul ?"
        print(f"  {d['ip']:16} 0x{d['devtype']:04x}  {d['mac']}  "
              f"{d['model']:22} {rf}  {d['name']}")
    if not found:
        print("  aucun Broadlink trouvé.\n")
        print("  Pistes : l'appareil dort (réessaie), il est éteint, ou il est")
        print("  sur un autre sous-réseau. L'intégration Broadlink de HA affiche")
        print("  son IP, tout comme la table DHCP du routeur.")
        return 1
    print(f"\n→ {len(found)} appareil(s). Pour lancer le labo dessus :")
    print(f"   dev/venv/bin/python dev/serve.py --device {found[0]['ip']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
