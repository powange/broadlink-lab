#!/usr/bin/env python3
"""
Un SECOND protocole réel : la Mantra RF00143.

L'outil est-il générique, ou taillé sur mesure pour la RF00234 ? Ce test répond,
sur du vrai signal : timings différents, longueur différente, disposition des bits
différente — et pas une ligne de code spécifique.

Il fige aussi une découverte transférable : les deux modèles partagent le même
checksum `sub8` k=0x55 et la même correspondance de luminosité. C'est une
signature de la famille de puces Mantra — sur la prochaine télécommande de la
marque, le piège n°1 sera résolu avant d'avoir capturé.
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
GAP = E["gap"]
ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}{'  — ' + str(extra) if extra else ''}")


bitmap = {}
for c in FIX["captures"]:
    pkt = decoder.decode_packet(c["b64"])
    frames = decoder.decode_pwm(pkt["durations"], GAP)
    bits = decoder.pick_frame(frames)
    bitmap[c["name"]] = bits
    check(f"{c['name'][:26]:28} {len(bits)} bits", len(bits) == E["frame_bits"], len(bits))
    check(f"{c['name'][:26]:28} bits exacts", bits == E["modal"][c["name"]])

# --- un protocole DIFFÉRENT de la RF00234 : c'est tout l'intérêt
pkt = decoder.decode_packet(FIX["captures"][0]["b64"])
f = decoder.decode_pwm(pkt["durations"], GAP)[1]
d = pkt["durations"]
zeros = [(d[f["idx"][j]], d[f["idx"][j] + 1]) for j in range(len(f["bits"])) if f["bits"][j] == "0"]
ones = [(d[f["idx"][j]], d[f["idx"][j] + 1]) for j in range(len(f["bits"])) if f["bits"][j] == "1"]
check("PWM : bit 0 = (court, long)", all(m < s for m, s in zeros), f"{zeros[0]}")
check("PWM : bit 1 = (long, court)", all(m > s for m, s in ones), f"{ones[0]}")
check("timings différents de la RF00234 (263/788 µs)",
      abs(zeros[0][0] - 263) > 20 or abs(zeros[0][1] - 788) > 20, f"{zeros[0]}")
check("trame de 48 bits, pas 64", E["frame_bits"] == 48)

# --- l'impulsion de clôture est bien une TERMINAISON, pas un bit perdu.
# 48 bits est un multiple de 8, et le checksum tombe juste : preuve définitive.
t = f["tail"]
check("l'impulsion de clôture est une terminaison, pas un bit",
      t and not t["looks_like_data"],
      f"mark={t['mark']}µs vs court={t['short']} long={t['long']}" if t else None)

# --- LE point : le MÊME checksum que l'autre modèle
C = E["checksum"]
det = decoder.detect_checksum(list(bitmap.values()), C["crc_start"], C["crc_end"])
check("checksum détecté automatiquement",
      det["kind"] == C["kind"] and det["k"] == C["k"],
      f"{det['kind']} k=0x{det['k']:02x} (confiance {det['confidence']})")
check("c'est le MÊME que la RF00234 : sub8 k=0x55, signature Mantra",
      det["kind"] == "sub8" and det["k"] == 0x55)
for name, b in bitmap.items():
    got = decoder.compute_checksum(b, C["kind"], C["crc_start"], C["crc_end"], C["k"])
    check(f"checksum recalculé — {name[:26]:28}",
          got == decoder.field_value(b, C["crc_start"], C["crc_end"]), f"0x{got:02x}")

# --- la carte, déduite automatiquement
F = E["fields"]
by_name = {c["name"]: c["meta"] for c in FIX["captures"]}
rows = [{"name": n, "bits": b, "meta": by_name[n]} for n, b in bitmap.items()]
r = infer.suggest(rows, ["light", "lum"])
found = {}
for x in r["fields"]:
    found.setdefault(x["name"], []).append((x["start"], x["end"]))
check("light déduit en [24,25)", tuple(F["light"]) in found.get("light", []),
      found.get("light"))
check("lum déduit en [28,32) avec les bornes 2-11",
      tuple(F["lum"]) in found.get("lum", []) and
      any(x["min"] == 2 and x["max"] == 11 for x in r["fields"]
          if (x["start"], x["end"]) == tuple(F["lum"])),
      found.get("lum"))

# La déduction propose AUSSI des faux positifs : avec un seul paramètre varié,
# les bits du code de commande et du checksum suivent la luminosité par
# construction. C'est la limite documentée — mieux vaut la figer que la nier.
check("les faux positifs sont attendus tant qu'un seul paramètre varie",
      len(found.get("lum", [])) > 1,
      f"{len(found.get('lum', []))} tranches proposées pour lum")

# --- la correspondance luminosité, IDENTIQUE à l'autre modèle
for label, raw in E["lum_map"].items():
    hits = [b for n, b in bitmap.items()
            if by_name[n].get("lum") == int(label) and by_name[n].get("light")]
    if hits:
        check(f"lum {label:>3} -> brut {raw:>2} (comme la RF00234)",
              decoder.field_value(hits[0], *F["lum"]) == raw)

# --- éteindre ne remet pas le niveau à zéro : vrai sur les DEUX modèles
off = [b for n, b in bitmap.items() if not by_name[n].get("light")]
check("lumière éteinte : le bit 24 tombe", all(b[F["light"][0]] == "0" for b in off))
check("lumière éteinte : la luminosité garde sa valeur (comme la RF00234)",
      all(decoder.field_value(b, *F["lum"]) != 0 for b in off),
      f"lum={[decoder.field_value(b, *F['lum']) for b in off]}")

# --- round-trip : la calibration vaut aussi pour ce protocole
for c in FIX["captures"]:
    p = decoder.decode_packet(c["b64"])
    again = decoder.to_b64(decoder.encode_packet(
        p["durations"], p["header"], p["repeats"], p["terminator"]))
    check(f"round-trip b64 — {c['name'][:26]:28}", again == c["b64"])

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
