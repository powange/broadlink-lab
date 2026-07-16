#!/usr/bin/env python3
"""
Sur quelle fréquence émet cette télécommande ?

Le projet capture en 433,92 MHz EN DUR, sans balayage (§3.1) : la fréquence était
connue, et le sweep coûte 30 s par capture. Ça a été vérifié sur la RF00234 — et
supposé sur tout le reste.

Le piège que ça tend est silencieux, et coûteux : un récepteur radio a de la
bande passante. Une télécommande décalée de quelques centaines de kHz est
CAPTURÉE quand même — d'autant mieux qu'on la tient contre le RM4 en capturant —
mais RÉÉMETTRE sur 433,92 vers un récepteur accordé ailleurs ne réveille
personne. Symptôme : les captures sont propres, le décodage est parfait, le
checksum tombe juste… et l'appareil ignore tout, sans le moindre signe.

Cet outil demande au RM4 la vraie fréquence, en balayant. À lancer devant toute
NOUVELLE télécommande, avant de conclure quoi que ce soit d'un échec d'émission.

    dev/venv/bin/python dev/find_frequency.py --device 192.168.0.42
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))

import broadlink                       # noqa: E402
import discover                        # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="IP du Broadlink (sous WSL2 : obligatoire)")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="secondes de balayage (défaut : 60)")
    args = ap.parse_args()

    if args.device:
        dev = broadlink.hello(args.device, timeout=discover.DEFAULT_TIMEOUT)
    else:
        found = broadlink.discover(timeout=discover.DEFAULT_TIMEOUT)
        if not found:
            sys.exit("aucun Broadlink trouvé. Sous WSL2 le broadcast ne passe pas : "
                     "utilise --device <ip> (dev/find_rm4.py le retrouve).")
        dev = found[0]
    dev.auth()
    print(f"RM4 : {dev.model} @ {dev.host[0]}\n")

    dev.sweep_frequency()
    try:
        print("APPUIE SUR UNE TOUCHE DE LA TÉLÉCOMMANDE, ET MAINTIENS-LA.")
        print("Le balayage a besoin d'un signal continu pour se caler.")
        print(f"(jusqu'à {args.timeout:.0f} s ; Ctrl-C pour arrêter)\n")
        # monotonic : l'horloge murale saute, et une resynchro NTP couperait le
        # balayage net ou le ferait durer indéfiniment (cf. capture_worker).
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            found, freq = dev.check_frequency()
            if found:
                print(f"  ✓ TROUVÉE : {freq} MHz\n")
                if abs(freq - 433.92) > 0.05:
                    print(f"  ⚠ Ce n'est PAS le 433,92 que le projet utilise en dur.")
                    print(f"    C'est très probablement pourquoi tes émissions sont")
                    print(f"    ignorées alors que les captures sont parfaites.")
                    print(f"    Relance le labo avec :  FREQUENCY={freq} dev/serve.py …")
                    print(f"    et RECAPTURE — les trames actuelles restent valides,")
                    print(f"    mais c'est la fréquence d'ÉMISSION qui compte.")
                else:
                    print("    C'est bien le 433,92 supposé par le projet : la")
                    print("    fréquence n'explique pas l'échec d'émission. Reste la")
                    print("    portée — le RM4 est-il à portée de CE récepteur ?")
                return 0
            print(f"  … balayage, {deadline - time.monotonic():.0f} s restantes", end="\r")
            time.sleep(1)
        print("\n  ✗ rien trouvé. Maintiens la touche enfoncée pendant tout le")
        print("    balayage, et rapproche la télécommande du RM4.")
        return 1
    finally:
        dev.cancel_sweep_frequency()
        print("\n(balayage arrêté, le RM4 est ressorti du mode écoute)")


if __name__ == "__main__":
    sys.exit(main())
