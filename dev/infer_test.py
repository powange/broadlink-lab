#!/usr/bin/env python3
"""
La déduction automatique retrouve-t-elle la carte qu'on a établie à la main ?

Le test le plus honnête possible : on donne les 42 vraies captures et leurs
métadonnées, et on vérifie que la machine redécouvre §10 toute seule.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "shared"))

import decoder  # noqa: E402
import infer    # noqa: E402

FIX = json.load(open(os.path.join(HERE, "fixtures", "real_rf00234.json")))
ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}{'  — ' + str(extra) if extra else ''}")


rows = []
for c in FIX["captures"]:
    pkt = decoder.decode_packet(c["b64"])
    bits = decoder.pick_frame(decoder.decode_pwm(pkt["durations"], 2000))
    m = dict(c["meta"])
    # les vieilles captures portent nuit/eco ; le schéma d'aujourd'hui a `mode`
    m["mode"] = "nuit" if m.pop("nuit", False) else ("eco" if m.pop("eco", False) else "normal")
    rows.append({"name": c["name"], "bits": bits, "meta": m})

r = infer.suggest(rows, ["light", "lum", "cct", "fan", "speed", "reverse", "mode", "timer"])
found = {f["name"]: f for f in r["fields"]}

# La carte de §10, établie à la main sur des jours. La machine doit la retrouver.
for name, (start, end) in {"light": (32, 33), "cct": (33, 36), "lum": (36, 40),
                           "reverse": (41, 42), "mode": (42, 44)}.items():
    f = found.get(name)
    check(f"{name:8} retrouvé en [{start},{end})",
          f and (f["start"], f["end"]) == (start, end),
          f"[{f['start']},{f['end']})" if f else "non trouvé")

check("lum : MSB-first et monotone (lum10 -> 2, lum20 -> 3…)",
      found["lum"]["msb_first"] and found["lum"]["monotone"])
check("lum : bornes réelles 1-11 déduites, pas 0-15",
      (found["lum"]["min"], found["lum"]["max"]) == (1, 11),
      f"{found['lum']['min']}-{found['lum']['max']}")
check("cct : 7 teintes déduites", (found["cct"]["min"], found["cct"]["max"]) == (1, 7))

# LE bonus : le checksum se dénonce en échouant partout. Il dépend de tous les
# champs, donc aucun paramètre isolé ne l'explique.
crc = [u for u in r["unexplained"] if u["start"] >= 56]
check("le checksum est signalé comme inexpliqué (il dépend de TOUT)",
      crc and crc[0]["start"] == 56 and crc[0]["end"] == 64, crc)

# L'octet de commande aussi : il suit le bouton pressé, pas l'état.
cmd = [u for u in r["unexplained"] if u["start"] < 32]
check("l'octet de commande est signalé inexpliqué (il suit le bouton, pas l'état)",
      cmd and cmd[0]["end"] <= 32, cmd)

# Et surtout : il trouve NOS erreurs d'étiquetage. `fan` était faux sur les
# premières captures — le ventilo était à l'arrêt malgré l'étiquette fan1.
prob = {p["param"]: p for p in r["problems"]}
check("l'étiquetage fautif de `fan` est détecté", "fan" in prob,
      list(prob) or "aucun problème signalé")
if "fan" in prob:
    check("il nomme les deux captures qui se contredisent",
          len(prob["fan"]["conflicts"]) >= 1,
          prob["fan"]["conflicts"][0] if prob["fan"]["conflicts"] else None)

# Aucun champ ne doit empiéter sur le préambule : il ne varie jamais.
check("aucun champ déduit sous le bit 26 (le préambule ne varie pas)",
      all(f["start"] >= 26 for f in r["fields"]),
      [f["name"] for f in r["fields"] if f["start"] < 26])

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
