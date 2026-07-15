#!/usr/bin/env python3
"""
Protocole RF synthétique pour les tests locaux.

Ce n'est PAS le protocole du Mantra Nenufar — il est inconnu, c'est justement ce
que l'outil sert à découvrir. C'est un protocole plausible (PWM, préambule
d'identité constant, champs contigus) qui permet de valider toute la chaîne
capture → diff → nommage → génération → émission sans matériel.

Trame de 24 bits :

    0        8      12    14       17           24
    | id     | lum  | cct | speed  | fixe       |
      constant  4b     2b    3b      constant

Le bloc `id` et le bloc `fixe` ne bougent jamais : ils jouent le rôle du
préambule + ID appairé décrit en §7 (« bits fixes = ID télécommande »). Si un
test les voit varier, c'est un bug.
"""
import os
import sys

ADDON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rf_lab")
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import decoder  # noqa: E402

ID = "10110010"
FIXE = "1001101"

LAYOUT = {           # nom: (start, end)
    "id": (0, 8),
    "lum": (8, 12),
    "cct": (12, 14),
    "speed": (14, 17),
    "fixe": (17, 24),
}

# timings PWM, du même ordre qu'une vraie télécommande 433 MHz
MARK_LONG, MARK_SHORT = 1200, 400
GAP = 8000
REPEATS = 3

# séquence de capture recommandée par CLAUDE.md §9 :
# (nom, lum brut, cct brut, speed brut, métadonnées physiques)
SEQ = [
    ("lum10_cct3000_v0", 1, 0, 0, {"lum": 10, "cct": 3000, "speed": 0}),
    ("lum20_cct3000_v0", 2, 0, 0, {"lum": 20, "cct": 3000, "speed": 0}),
    ("lum30_cct3000_v0", 3, 0, 0, {"lum": 30, "cct": 3000, "speed": 0}),
    ("lum10_cct4000_v0", 1, 1, 0, {"lum": 10, "cct": 4000, "speed": 0}),
    ("lum10_cct5000_v0", 1, 2, 0, {"lum": 10, "cct": 5000, "speed": 0}),
    ("lum10_cct3000_v1", 1, 0, 1, {"lum": 10, "cct": 3000, "speed": 1}),
    ("lum10_cct3000_v2", 1, 0, 2, {"lum": 10, "cct": 3000, "speed": 2}),
]


def bits_for(lum, cct, speed):
    """État -> bitstring de 24 bits."""
    return (ID
            + format(lum & 0xF, "04b")
            + format(cct & 0x3, "02b")
            + format(speed & 0x7, "03b")
            + FIXE)


def durations_for(lum, cct, speed):
    """État -> durées µs, avec les répétitions et le gap inter-trames."""
    durations = []
    for _ in range(REPEATS):
        for b in bits_for(lum, cct, speed):
            durations += ([MARK_LONG, MARK_SHORT] if b == "1"
                          else [MARK_SHORT, MARK_LONG])
        durations += [MARK_SHORT, GAP]      # clôture de trame
    return durations


def packet_for(lum, cct, speed):
    """État -> paquet Broadlink brut (ce que check_data() rendrait)."""
    return decoder.encode_packet(
        durations_for(lum, cct, speed), decoder.HEADER_RF433, REPEATS)


def b64_for(lum, cct, speed):
    return decoder.to_b64(packet_for(lum, cct, speed))


def decode_state(data, gap=2000):
    """
    Paquet ou b64 -> {champ: valeur brute}. C'est ce qui permet au faux RM4 de
    dire ce qu'on vient de lui envoyer, et aux tests de vérifier une émission.
    Retourne None si la trame ne se décode pas.
    """
    pkt = decoder.decode_packet(data)
    frames = decoder.decode_pwm(pkt["durations"], gap)
    if not frames:
        return None
    bits = frames[0]["bits"]
    if len(bits) != 24:
        return None
    return {name: decoder.field_value(bits, a, b)
            for name, (a, b) in LAYOUT.items()}


# Le protocole de test n'expose que ces trois paramètres — pas ceux de la vraie
# RF00234 (reverse / nuit / éco / timer), que le faux ventilo ne simule pas.
SEED_META_SCHEMA = [
    {"key": "lum", "label": "Luminosité", "type": "number", "short": "lum",
     "always": True},
    {"key": "cct", "label": "CCT (K)", "type": "number", "short": "cct",
     "always": True},
    {"key": "speed", "label": "Vitesse", "type": "number", "short": "v",
     "min": 0, "max": 8, "always": True},
]


def store_seed():
    """Les 7 captures de §9, prêtes à écrire dans captures.json."""
    return {
        "captures": [
            {"id": f"{i:08x}", "name": name, "b64": b64_for(lum, cct, speed),
             "meta": meta}
            for i, (name, lum, cct, speed, meta) in enumerate(SEQ)
        ],
        "fields": [],
        "checksum": {"kind": "none"},
        "meta_schema": SEED_META_SCHEMA,
    }


if __name__ == "__main__":
    print(f"{'capture':22} {'bits':26} id/lum/cct/speed/fixe")
    for name, lum, cct, speed, _ in SEQ:
        st = decode_state(packet_for(lum, cct, speed))
        print(f"{name:22} {bits_for(lum, cct, speed):26} "
              f"{st['id']}/{st['lum']}/{st['cct']}/{st['speed']}/{st['fixe']}")
