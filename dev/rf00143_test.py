#!/usr/bin/env python3
"""
Le SECOND protocole réel du projet : la Mantra RF00143, décodée à 48 bits sur 48.

L'outil est-il générique, ou taillé sur mesure pour la RF00234 ? Ce test répond,
sur du vrai signal : timings différents, trame deux fois plus courte, disposition
des bits sans rapport — et pas une ligne de code spécifique.

Il fige aussi ce que les deux modèles partagent, qui est transférable : le
checksum `sub8` k=0x55, la correspondance de luminosité, et l'encodage du mode
moteur. C'est une signature de famille de puces — sur la prochaine télécommande
Mantra, le piège n°1 sera résolu avant d'avoir capturé.

Et ce qu'ils NE partagent pas, qui l'est tout autant : ici le ventilateur n'a pas
de bit d'alimentation. « Éteint » s'écrit « vitesse 0 », et la vitesse est perdue
— quand la RF00234 la conserve derrière un bit dédié. La symétrie « un bit
d'alimentation par organe » de la RF00234 n'est donc PAS une loi. La lampe, elle,
la respecte, sur la même télécommande.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "shared"))

import decoder  # noqa: E402
import infer    # noqa: E402

FIX = json.load(open(os.path.join(HERE, "fixtures", "real_rf00143.json")))
E = FIX["expected"]
GAP, F = E["gap"], E["fields"]
ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}{'  — ' + str(extra) if extra else ''}")


def V(bits, name):
    return decoder.field_value(bits, *F[name])


# --- décodage : les bits sortis du signal n'ont pas bougé
rows = []
for i, c in enumerate(FIX["captures"]):
    pkt = decoder.decode_packet(c["b64"])
    bits = decoder.pick_frame(decoder.decode_pwm(pkt["durations"], GAP))
    rows.append({"name": c["name"], "meta": c["meta"], "bits": bits})
    if bits != E["bits"][i] or len(bits) != E["frame_bits"]:
        check(f"{c['name'][:30]:32} bits", False, f"{len(bits)} bits")
check(f"les {len(rows)} captures redonnent les mêmes bits qu'au jour de la capture",
      all(r["bits"] == E["bits"][i] for i, r in enumerate(rows)))
check("préambule + ID constants sur toutes",
      len({r["bits"][:16] for r in rows}) == 1, E["preamble"])

# --- un protocole DIFFÉRENT de la RF00234 : c'est tout l'intérêt du fichier
pkt = decoder.decode_packet(FIX["captures"][0]["b64"])
f = decoder.decode_pwm(pkt["durations"], GAP)[1]
d = pkt["durations"]
pulse = lambda want: [(d[f["idx"][j]], d[f["idx"][j] + 1])          # noqa: E731
                      for j in range(len(f["bits"])) if f["bits"][j] == want]
zeros, ones = pulse("0"), pulse("1")
check("PWM : bit 0 = (court, long)", all(m < s for m, s in zeros), f"{zeros[0]}")
check("PWM : bit 1 = (long, court)", all(m > s for m, s in ones), f"{ones[0]}")
check("timings différents de la RF00234 (263/788 µs)",
      abs(zeros[0][0] - 263) > 20 or abs(zeros[0][1] - 788) > 20, f"{zeros[0]}")
check("trame de 48 bits, pas 64", E["frame_bits"] == 48)

# 48 bits est un multiple de 8 et le checksum tombe juste : l'impulsion de
# clôture est bien une terminaison, pas un bit qu'on aurait perdu.
t = f["tail"]
check("l'impulsion de clôture est une terminaison, pas un bit",
      t and not t["looks_like_data"],
      f"mark={t['mark']}µs vs court={t['short']} long={t['long']}" if t else None)

# --- LA CARTE COMPLÈTE. Une seule fonction, et elle doit tout expliquer.
C = E["checksum"]


def model(m):
    """L'état saisi -> ce que la trame doit porter."""
    return {
        "mode": E["mode_map"][m["mode"]],
        "reverse": int(m["reverse"]),
        "cct": E["cct_map"][str(m["cct"])],
        "light": int(m["light"]),
        # LE point de ce protocole : pas de bit d'alimentation pour le ventilo.
        # Éteint = 0, et l'éco écrase la vitesse par 7 quelle qu'elle soit.
        "speed": (E["eco_speed"] if m["mode"] == "eco"
                  else (m["speed"] if m["fan"] else 0)),
        "lum": E["lum_map"][str(m["lum"])],
        "timer": m["timer"],
    }


bad = [(r["name"], k, V(r["bits"], k), want)
       for r in rows for k, want in model(r["meta"]).items() if V(r["bits"], k) != want]
check(f"la carte explique les 7 champs des {len(rows)} captures, sans exception",
      not bad, bad[:2] if bad else f"{len(rows) * 7} champs")

check("checksum recalculé sur toutes",
      all(decoder.compute_checksum(r["bits"], C["kind"], C["crc_start"],
                                   C["crc_end"], C["k"]) == V(r["bits"], "crc")
          for r in rows))
det = decoder.detect_checksum([r["bits"] for r in rows], C["crc_start"], C["crc_end"])
check("checksum retrouvé tout seul", det["kind"] == C["kind"] and det["k"] == C["k"],
      f"{det['kind']} k=0x{det['k']:02x} (confiance {det['confidence']})")

# --- CE QUI SE TRANSFÈRE d'un modèle Mantra à l'autre
check("MÊME checksum que la RF00234 : sub8 k=0x55", C["kind"] == "sub8" and C["k"] == 0x55)
check("MÊME correspondance de luminosité : lum10 -> 2 … lum100 -> 11",
      E["lum_map"]["10"] == 2 and E["lum_map"]["100"] == 11)
check("MÊME encodage du mode moteur : 00 normal, 01 nuit, 10 éco",
      E["mode_map"] == {"normal": 0, "nuit": 1, "eco": 2})

# --- CE QUI NE SE TRANSFÈRE PAS, et c'est aussi précieux à savoir
off = [r for r in rows if not r["meta"]["fan"]]
check("le ventilateur n'a PAS de bit d'alimentation : éteint = vitesse 0",
      len(off) >= 2 and all(V(r["bits"], "speed") == 0 for r in off),
      f"{len(off)} captures fan0, vitesses saisies "
      f"{sorted(r['meta']['speed'] for r in off)}")
check("… et la vitesse est PERDUE, là où la RF00234 la conserve",
      all(r["meta"]["speed"] != 0 for r in off))

# La lampe, elle, suit bien la règle de la RF00234 — sur la MÊME télécommande.
dark = [r for r in rows if not r["meta"]["light"]]
check("la lampe, elle, garde son niveau en s'éteignant (comme la RF00234)",
      len(dark) >= 2 and all(V(r["bits"], "lum") == E["lum_map"][str(r["meta"]["lum"])]
                             for r in dark),
      f"{len(dark)} captures light0, lum conservés "
      f"{[V(r['bits'], 'lum') for r in dark]}")

# --- la déduction automatique, sur ce protocole-ci
keys = ["light", "lum", "cct", "fan", "speed", "reverse", "mode", "timer"]
r = infer.suggest(rows, keys)
found = {}
for x in r["fields"]:
    found.setdefault(x["name"], []).append([x["start"], x["end"]])
for name in ("mode", "reverse", "cct", "light", "lum", "timer"):
    check(f"{name:8} déduit en {F[name]}", F[name] in found.get(name, []),
          found.get(name))
check("cct : les 21 teintes déduites, bornes 4-24",
      any(x["min"] == 4 and x["max"] == 24 for x in r["fields"] if x["name"] == "cct"))

# `fan` et `speed` se partagent [25,28) : aucun des deux ne l'explique SEUL, et
# l'outil ne doit surtout pas conclure à un étiquetage fautif — l'étiquetage est
# juste. Il doit rendre le contrôle « un seul paramètre varie », qui vise juste.
prob = {p["param"]: p for p in r["problems"]}
check("`fan` et `speed` sont signalés inexpliqués (ils partagent un champ)",
      "fan" in prob and "speed" in prob, list(prob))
check("et l'outil n'accuse PAS l'étiquetage — il est correct ici",
      all("mal étiquetée" not in p["reason"] for p in prob.values()))
for k in ("fan", "speed"):
    if k in prob:
        touched = {i for p in prob[k]["isolated"] for i in p["bits"]}
        check(f"le contrôle « un seul paramètre » place « {k} » dans [25,28)",
              touched & set(range(25, 28)) and not touched & set(range(16, 25)),
              sorted(touched))
check("[25,28) reste inexpliqué plutôt que faussement attribué",
      any(u["start"] == 25 and u["end"] == 28 for u in r["unexplained"]),
      [(u["start"], u["end"]) for u in r["unexplained"]])

# --- round-trip : la calibration vaut aussi pour ce protocole
for c in FIX["captures"]:
    p = decoder.decode_packet(c["b64"])
    if decoder.to_b64(decoder.encode_packet(p["durations"], p["header"], p["repeats"],
                                            p["terminator"])) != c["b64"]:
        check(f"round-trip b64 — {c['name']}", False)
check(f"round-trip b64 sur les {len(FIX['captures'])} captures", True)

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
