#!/usr/bin/env python3
"""
Non-régression sur du VRAI signal : les deux captures de la RF00234 prises via
un RM4 Pro (dev/fixtures/real_rf00234.json).

Le faux RM4 valide la logique avec un protocole que j'ai écrit moi-même — donc
un protocole qui, par construction, ne piège pas le décodeur. Ces trames-là ne
pardonnent rien : c'est elles qui ont révélé que frames[0] porte le bruit de
capture, et donc que le diff était faux.
"""
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "rf_lab"))

import decoder  # noqa: E402

FIX = json.load(open(os.path.join(HERE, "fixtures", "real_rf00234.json")))
E = FIX["expected"]
GAP = E["gap"]

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}{'  — ' + str(extra) if extra else ''}")


bitmap = {}
for c in FIX["captures"]:
    name = c["name"]
    pkt = decoder.decode_packet(c["b64"])
    frames = decoder.decode_pwm(pkt["durations"], GAP)
    modal = decoder.pick_frame(frames)
    agree = sum(1 for f in frames if f["bits"] == modal)
    bitmap[name] = modal

    check(f"{name[:28]:28} en-tête 0x{pkt['header']:02x}", pkt["header"] == E["header"])
    # le nombre de répétitions dépend de la durée de l'appui : 6 à 8 observées
    check(f"{name[:28]:28} {len(frames)} répétitions",
          len(frames) == E["nframes"][name] and len(frames) >= E["nframes_min"],
          len(frames))
    check(f"{name[:28]:28} trame de {len(modal)} bits", len(modal) == E["frame_bits"], len(modal))
    check(f"{name[:28]:28} {agree}/{len(frames)} répétitions d'accord",
          agree == E["agree"][name], f"attendu {E['agree'][name]}")
    check(f"{name[:28]:28} bits exacts", modal == E["modal"][name])

# Le nœud du problème : light1 a une 1re répétition bruitée (72 bits). Si on
# prenait frames[0], les deux captures seraient décalées et le diff serait faux.
noisy = FIX["captures"][1]["b64"]
frames = decoder.decode_pwm(decoder.decode_packet(noisy)["durations"], GAP)
check("la 1re répétition est bien celle qui porte le bruit",
      len(frames[0]["bits"]) != len(decoder.pick_frame(frames)),
      f"frames[0]={len(frames[0]['bits'])}b vs majoritaire={len(decoder.pick_frame(frames))}b")
check("pick_frame ignore le bruit et rend la majoritaire",
      decoder.pick_frame(frames) == E["modal"]["light1_lum10_cct3000_fan1_v1"])

# Le diff : des colonnes précises, pas du bruit
a = decoder.analyze({k: v for k, v in bitmap.items()
                     if k in ("light0_lum10_cct3000_fan1_v1",
                              "light1_lum10_cct3000_fan1_v1")})
check("longueurs alignées entre captures", a["truncated"] is False)
check("colonnes qui varient (lumière seule)",
      a["varying"] == E["varying_light_only"], a["varying"])

# Déterminisme : le même état capturé 2× donne la MÊME trame. C'est ce qui
# exclut un compteur anti-rejeu (§7) et prouve que le dernier octet est calculé.
check("même état capturé 2× -> trames identiques",
      bitmap["light1_lum10_cct3000_fan0_v0"] == bitmap["light1_lum10_cct3000_fan0_v0__bis"])

# Le checksum, trouvé sur ces captures : (0x55 - somme des octets 0..6) & 0xFF
C = E["checksum"]
det = decoder.detect_checksum(list(bitmap.values()), C["crc_start"], C["crc_end"])
check("checksum détecté automatiquement",
      det["kind"] == C["kind"] and det["k"] == C["k"],
      f"{det['kind']} k=0x{det['k']:02x} (confiance {det['confidence']})")
for name, b in bitmap.items():
    got = decoder.compute_checksum(b, C["kind"], C["crc_start"], C["crc_end"], C["k"])
    check(f"checksum recalculé — {name[:26]:28}",
          got == decoder.field_value(b, C["crc_start"], C["crc_end"]),
          f"0x{got:02x}")

# ---------------------------------------------------------------- le protocole
F, R = E["fields"], E["ranges"]
by_name = {c["name"]: c["meta"] for c in FIX["captures"]}
val = lambda b, k: decoder.field_value(b, *F[k])

# Les 64 bits sont-ils tous attribués ?
covered = set()
for a, b in F.values():
    covered |= set(range(a, b))
check("les 64 bits de la trame sont attribués", len(covered) == 64,
      f"{len(covered)}/64, manque {sorted(set(range(64)) - covered)}")

# Chaque champ reste-t-il dans ses bornes sur TOUTES les captures ?
for k, (lo, hi) in R.items():
    bad = [(n, val(b, k)) for n, b in bitmap.items() if not (lo <= val(b, k) <= hi)]
    check(f"{k:8} toujours dans [{lo}, {hi}]", not bad, bad[:2])

# Le préambule + ID ne bouge JAMAIS (§7 : y toucher et le ventilo ignore tout)
check("préambule + ID identique sur les 41 états",
      len({b[F["preambule"][0]:F["preambule"][1]] for b in bitmap.values()}) == 1)

# Les sous-systèmes observés
subs = {b[26:28] for b in bitmap.values()}
check("3 sous-systèmes : lumière, ventilo, timer",
      subs == set(E["subsystems"]), sorted(subs))

# --- la luminosité : série des 11 paliers, un seul paramètre variant
seq = {val(b, "lum"): b for n, b in bitmap.items()
       if n.startswith("lumseq_") or n == "light1_lum10_cct3000_fan1_v1"}
check(f"série des 11 paliers de luminosité ({len(seq)})",
      sorted(seq) == list(range(1, 12)), sorted(seq))
for lum_label, raw in E["lum_map"].items():
    name = f"lumseq_{int(lum_label):03d}"
    if name in bitmap:
        check(f"lum {lum_label:>3} -> brut {raw:>2}", val(bitmap[name], "lum") == raw)

# --- LA preuve : générer chaque palier et retomber sur la vraie trame
ref = seq[2]
exact = sum(1 for raw, real in seq.items()
            if decoder.set_field(
                decoder.set_field(ref, *F["lum"], raw), *F["crc"],
                decoder.compute_checksum(decoder.set_field(ref, *F["lum"], raw),
                                         C["kind"], C["crc_start"], C["crc_end"], C["k"])
            ) == real)
check("10 paliers sur 11 générés à l'identique de la vraie trame", exact == 10, exact)

# --- le CCT : 7 teintes linéaires
for k_label, raw in E["cct_map"].items():
    hits = [b for n, b in bitmap.items() if by_name[n].get("cct") == int(k_label)
            and n.startswith("light1_lum70_")]
    if hits:
        check(f"cct {k_label:>4} K -> brut {raw}", val(hits[0], "cct") == raw)

# --- le timer : une DURÉE en unités de 2 minutes, pas un index de bouton
for h_label, raw in E["timer_map"].items():
    hits = [b for n, b in bitmap.items() if by_name[n].get("timer") == int(h_label)]
    if hits:
        got = val(hits[0], "timer")
        check(f"timer {h_label} h -> brut {raw} ({raw * E['timer_unit_minutes']} min)",
              got == raw, got)

# --- le mode moteur : nuit et éco s'EXCLUENT (un champ, pas deux booléens)
for n, b in bitmap.items():
    m, mode = by_name[n], val(b, "mode")
    want = 1 if m.get("nuit") else (2 if m.get("eco") else 0)
    check(f"mode moteur — {n[:26]:28}", mode == want, f"{mode} attendu {want}") \
        if (m.get("nuit") or m.get("eco")) else None
check("aucune capture n'a nuit ET eco (le champ vaut 3 au plus)",
      all(val(b, "mode") <= 2 for b in bitmap.values()))

# --- on/off : couper ne remet JAMAIS le niveau à zéro
for organ, level in (("light", "lum"), ("fan", "speed")):
    off = [b for b in bitmap.values() if val(b, organ) == 0]
    if off:
        check(f"{organ} éteint : {level} garde sa valeur",
              all(val(b, level) != 0 for b in off),
              f"{level}={[val(b, level) for b in off]}")

# --- le checksum couvre tout le protocole
det = decoder.detect_checksum(list(bitmap.values()), C["crc_start"], C["crc_end"])
check("checksum détecté sur les 41 états",
      det["kind"] == C["kind"] and det["k"] == C["k"],
      f"{det['kind']} k=0x{det['k']:02x} (confiance {det['confidence']})")

# Timings PWM réels
pkt = decoder.decode_packet(FIX["captures"][0]["b64"])
d, f = pkt["durations"], decoder.decode_pwm(pkt["durations"], GAP)[1]
zeros = [(d[f["idx"][j]], d[f["idx"][j] + 1]) for j in range(len(f["bits"])) if f["bits"][j] == "0"]
ones = [(d[f["idx"][j]], d[f["idx"][j] + 1]) for j in range(len(f["bits"])) if f["bits"][j] == "1"]
check("bit 0 = (court, long)", all(m < s for m, s in zeros), f"{zeros[0]}")
check("bit 1 = (long, court)", all(m > s for m, s in ones), f"{ones[0]}")

# Rebuild sur signal réel, en partant de la capture bruitée
src = FIX["captures"][1]
pkt = decoder.decode_packet(src["b64"])
frames = decoder.decode_pwm(pkt["durations"], GAP)
ref = decoder.pick_frame(frames)
target = E["modal"]["light0_lum10_cct3000_fan1_v1"]
dur = decoder.rebuild_frame(pkt["durations"], frames, target, ref)
check("rebuild ON -> OFF vérifié malgré la répétition bruitée",
      decoder.verify_rebuild(dur, target, GAP))
check("rebuild préserve le nombre de durées", len(dur) == len(pkt["durations"]))
check("toutes les répétitions portent la cible",
      all(target in x["bits"] for x in decoder.decode_pwm(dur, GAP)))

# Le RM4 ne met PAS de terminateur 0d 05 sur ses captures brutes : en rajouter
# un collerait une impulsion parasite à la fin de chaque trame émise.
for c in FIX["captures"]:
    p = decoder.decode_packet(c["b64"])
    check(f"pas de terminateur 0d 05 — {c['name'][:26]}", p["terminator"] is False)

# Round-trip b64 sur vrai signal (calibration du TICK, §4)
for c in FIX["captures"]:
    p = decoder.decode_packet(c["b64"])
    again = decoder.to_b64(decoder.encode_packet(
        p["durations"], p["header"], p["repeats"], p["terminator"]))
    check(f"round-trip b64 identique — {c['name'][:26]}", again == c["b64"])

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
