#!/usr/bin/env python3
"""
Profil v2 : `requires` et `semantics`.

Ce que la trame ENCODE n'est pas ce que le récepteur EN FAIT. La v1 supposait
qu'une trame porte l'état et que l'appareil l'applique en entier — vrai de la
Mantra RF00234, faux de la Mantra R00143 (CLAUDE.md §10), dont le récepteur
n'applique que les champs libres plus UN champ désigné par l'octet de commande.

Deux notions, invisibles dans les bits, établies au ventilateur :

  requires   : ce champ ne s'applique que si `cmd` vaut telle valeur
  semantics  : le récepteur BASCULE ce champ au lieu de le régler

Le profil R00143 ci-dessous est celui du vrai matériel, avec sa vraie carte et
sa vraie capture de référence.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))

import profile as P  # noqa: E402

FIX = json.load(open(os.path.join(HERE, "fixtures", "real_rf00143.json")))
ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}{'  — ' + str(extra) if extra else ''}")


# La carte réelle de la R00143, sémantique comprise (§10).
FIELDS = [
    {"name": "id", "start": 0, "end": 16, "role": "const", "identity": True},
    {"name": "mode", "start": 16, "end": 18, "role": "data", "min": 0, "max": 2,
     "requires": {"cmd": 13}},
    # Le récepteur IGNORE le bit 18 : il inverse le sens à chaque trame cmd=12.
    {"name": "reverse", "start": 18, "end": 19, "role": "data", "min": 0, "max": 1,
     "requires": {"cmd": 12}, "semantics": "toggle"},
    {"name": "cct", "start": 19, "end": 24, "role": "data", "min": 4, "max": 24},
    {"name": "light", "start": 24, "end": 25, "role": "data", "min": 0, "max": 1},
    {"name": "speed", "start": 25, "end": 28, "role": "data", "min": 0, "max": 7,
     "requires": {"cmd": 10}},
    {"name": "lum", "start": 28, "end": 32, "role": "data", "min": 2, "max": 11},
    {"name": "timer", "start": 32, "end": 36, "role": "data", "min": 0, "max": 8,
     "requires": {"cmd": 14}},
    # const du point de vue de l'ÉTAT — il ne décrit rien de l'appareil — mais
    # c'est lui qui décide de ce que le récepteur applique. D'où `requires`.
    {"name": "cmd", "start": 36, "end": 40, "role": "const"},
    {"name": "crc", "start": 40, "end": 48, "role": "crc"},
]

PROF = {
    "version": 2,
    "device": {"id": "mantra_flower", "name": "Mantra Flower",
               "manufacturer": "Mantra", "model": "R00143"},
    "rf": {"frequency": 433.92, "gap": FIX["expected"]["gap"],
           "reference_b64": FIX["captures"][0]["b64"]},
    "fields": FIELDS,
    "checksum": {"kind": "sub8", "k": 0x55},
    "entities": [
        {"type": "light", "id": "light", "name": "Lumière", "power": "light",
         "brightness": {"field": "lum", "min": 2, "max": 11},
         "color_temp": {"field": "cct", "min": 4, "max": 24, "kelvin": [3000, 5000]}},
        {"type": "fan", "id": "fan", "name": "Ventilateur", "power": "light",
         "percentage": {"field": "speed", "min": 0, "max": 7},
         "direction": "reverse",
         "preset": {"field": "mode", "options": ["normal", "nuit", "eco"]}},
    ],
}

check("un profil v2 réel valide", P.validate(PROF) == [], P.validate(PROF))

# --- LE point : combien de trames, et lesquelles ?
check("changer la lumière seule : UNE trame, sans contrainte",
      P.emit_groups(PROF, ["light"]) == [{}], P.emit_groups(PROF, ["light"]))
check("le bloc lampe entier tient toujours en une trame",
      P.emit_groups(PROF, ["light", "lum", "cct"]) == [{}])
check("changer la vitesse : une trame, à cmd=10",
      P.emit_groups(PROF, ["speed"]) == [{"cmd": 10}], P.emit_groups(PROF, ["speed"]))
# Le point le plus important du format : le bloc lampe voyage GRATUITEMENT avec
# la trame de vitesse. Inutile de lui en dédier une.
check("vitesse + lumière : toujours UNE trame — la lampe voyage avec",
      P.emit_groups(PROF, ["speed", "light", "lum"]) == [{"cmd": 10}],
      P.emit_groups(PROF, ["speed", "light", "lum"]))
check("vitesse + sens : DEUX trames, cmd=10 puis cmd=12",
      P.emit_groups(PROF, ["speed", "reverse"]) == [{"cmd": 12}, {"cmd": 10}]
      or P.emit_groups(PROF, ["speed", "reverse"]) == [{"cmd": 10}, {"cmd": 12}],
      P.emit_groups(PROF, ["speed", "reverse"]))
check("les quatre champs ventilo : quatre trames",
      len(P.emit_groups(PROF, ["speed", "reverse", "mode", "timer"])) == 4,
      P.emit_groups(PROF, ["speed", "reverse", "mode", "timer"]))
# Le cas qui compte : HA redemande le sens courant. Rien ne change, donc rien ne
# doit partir — une trame « pour rien » inverserait le ventilateur, puisque le
# récepteur bascule sur cmd=12 sans lire le bit.
check("rien à appliquer : AUCUNE trame", P.emit_groups(PROF, []) == [],
      P.emit_groups(PROF, []))

# --- le piège du resync : « tout » ne doit PAS inclure les toggles
groups = P.emit_groups(PROF)
check("« tout » exclut les toggles — sinon chaque resynchro inverserait le sens",
      {"cmd": 12} not in groups, groups)
check("… mais garde les champs absolus", {"cmd": 10} in groups and {"cmd": 13} in groups)
check("les toggles sont nommés", P.toggles(PROF) == ["reverse"], P.toggles(PROF))

# --- l'identité : ce qui permet de reconnaître une trame entendue.
# PAS « les champs const » : `const` dit « ne réécris pas ». L'octet de commande
# est const ET change à chaque bouton — matcher dessus ferait rejeter au pont ses
# PROPRES trames. C'est un vrai bug, attrapé par les tests d'intégration.
check("l'identité est le préambule, pas tous les const",
      [f["name"] for f in P.identity(PROF)] == ["id"],
      [f["name"] for f in P.identity(PROF)])
check("l'octet de commande est const mais n'identifie rien",
      "cmd" not in [f["name"] for f in P.identity(PROF)])

# --- la RF00234 : aucun requires, donc le comportement v1 à l'identique
V1 = {**PROF, "version": 1,
      "fields": [{k: v for k, v in f.items() if k not in ("requires", "semantics")}
                 for f in FIELDS]}
check("un profil v1 reste valide (rétrocompatible)", P.validate(V1) == [], P.validate(V1))
check("sans requires : une seule trame, comme avant",
      P.emit_groups(V1, ["speed", "reverse", "mode", "timer", "light"]) == [{}])
check("sans semantics : aucun toggle", P.toggles(V1) == [])

# --- validation : un profil bancal ne se verrait qu'au ventilateur qui n'obéit
#     qu'à moitié, sans message. Autant échouer au chargement.
def errs(mutate):
    p = json.loads(json.dumps(PROF))
    mutate(p)
    return P.validate(p)


def field(p, name):
    return next(f for f in p["fields"] if f["name"] == name)


check("requires vers un champ inexistant -> refusé",
      any("n'existe pas" in e for e in
          errs(lambda p: field(p, "speed").update(requires={"bouton": 1}))))
check("requires qui déborde la largeur du champ -> refusé",
      any("ne tient pas" in e for e in
          errs(lambda p: field(p, "speed").update(requires={"cmd": 99}))),
      errs(lambda p: field(p, "speed").update(requires={"cmd": 99})))
check("requires sur soi-même -> refusé",
      any("lui-même" in e for e in
          errs(lambda p: field(p, "speed").update(requires={"speed": 1}))))
check("requires vide -> refusé",
      any("objet non vide" in e for e in
          errs(lambda p: field(p, "speed").update(requires={}))))
check("sémantique inconnue -> refusée",
      any("inconnue" in e for e in
          errs(lambda p: field(p, "speed").update(semantics="bascule"))))
check("un champ const ne peut pas être un toggle -> refusé",
      any("jamais piloté" in e for e in
          errs(lambda p: field(p, "cmd").update(semantics="toggle"))))
check("une version inconnue -> refusée",
      any("inconnue" in e for e in errs(lambda p: p.update(version=99))))
check("identity sur un champ réécrit -> refusé — il n'identifierait rien",
      any("identifie rien" in e for e in
          errs(lambda p: field(p, "speed").update(identity=True))))

# validate doit intercepter les profils corrupteurs — c'est sa raison d'être :
# échouer au chargement plutôt qu'au ventilateur qui n'obéit qu'à moitié.
check("champs qui se chevauchent -> refusé (l'écriture de l'un corromprait l'autre)",
      any("déjà pris" in e for e in
          errs(lambda p: p["fields"].append(
              {"name": "x", "start": 25, "end": 27, "role": "data"}))))
check("champ crc sans checksum -> refusé (le CRC ne serait jamais recalculé)",
      any("jamais recalculé" in e for e in errs(lambda p: p.update(checksum={"kind": "none"}))))
check("min > max -> refusé",
      any("min" in e and "max" in e for e in
          errs(lambda p: field(p, "lum").update(min=11, max=2))))
check("start/end absent ou négatif -> refusé (sinon set_field écrit à côté)",
      any("invalide" in e for e in
          errs(lambda p: p["fields"].append({"name": "y", "end": 50, "role": "data"})))
      and any("invalide" in e for e in
              errs(lambda p: p["fields"].append({"name": "z", "start": -2, "end": 4,
                                                 "role": "data"}))))



# --- le pont doit pouvoir fabriquer les trames : les valeurs de départ
d = P.defaults(PROF)
check("les valeurs par défaut viennent de la référence, const et crc exclus",
      "cmd" not in d and "crc" not in d and "id" not in d and d["light"] == 1,
      d)

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
