"""
Store peuplé des VRAIES captures RF00234 et de leur carte de champs (§10).

Sert à tester tout ce qui dépend du protocole réel — l'export du package HA et
/api/set — contre du vrai signal. Le protocole synthétique de protocol.py ne fait
que 24 bits et n'a ni lumière, ni ventilo, ni timer : y appliquer la carte du
Mantra produirait des trames absurdes (et /api/set refuserait de les émettre,
ce qui est le comportement voulu).
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "real_rf00234.json")

FIELDS = [
    {"name": "preambule", "start": 0, "end": 26, "msb_first": True, "role": "const"},
    {"name": "cmd", "start": 26, "end": 32, "msb_first": True, "role": "const"},
    {"name": "light", "start": 32, "end": 33, "msb_first": True, "role": "data", "min": 0, "max": 1},
    {"name": "cct", "start": 33, "end": 36, "msb_first": True, "role": "data", "min": 1, "max": 7},
    {"name": "lum", "start": 36, "end": 40, "msb_first": True, "role": "data", "min": 1, "max": 11},
    {"name": "fan", "start": 40, "end": 41, "msb_first": True, "role": "data", "min": 0, "max": 1},
    {"name": "reverse", "start": 41, "end": 42, "msb_first": True, "role": "data", "min": 0, "max": 1},
    {"name": "mode", "start": 42, "end": 44, "msb_first": True, "role": "data", "min": 0, "max": 2},
    {"name": "speed", "start": 44, "end": 48, "msb_first": True, "role": "data", "min": 1, "max": 8},
    {"name": "timer", "start": 48, "end": 56, "msb_first": True, "role": "data", "min": 0, "max": 255},
    {"name": "crc", "start": 56, "end": 64, "msb_first": True, "role": "crc"},
]

META_SCHEMA = [
    {"key": "light", "label": "Lumière on", "type": "bool", "short": "light", "always": True},
    {"key": "lum", "label": "Luminosité", "type": "number", "short": "lum", "always": True},
    {"key": "cct", "label": "CCT (K)", "type": "number", "short": "cct", "always": True},
    {"key": "fan", "label": "Ventilo on", "type": "bool", "short": "fan", "always": True},
    {"key": "speed", "label": "Vitesse", "type": "number", "short": "v", "min": 0, "max": 8, "always": True},
    {"key": "reverse", "label": "Reverse", "type": "bool", "short": "rev"},
    {"key": "mode", "label": "Mode moteur", "type": "enum", "short": "m",
     "options": ["normal", "nuit", "eco"]},
    {"key": "timer", "label": "Timer (h)", "type": "enum", "short": "t", "options": [0, 1, 2, 4, 8]},
]


def store():
    fix = json.load(open(FIX))
    return {
        "captures": [{"id": f"{i:08x}", "name": c["name"], "b64": c["b64"],
                      "meta": c["meta"]}
                     for i, c in enumerate(fix["captures"])],
        "fields": FIELDS,
        "checksum": {"kind": "sub8", "k": 85},
        "meta_schema": META_SCHEMA,
    }


if __name__ == "__main__":
    s = store()
    print(f"{len(s['captures'])} captures réelles, {len(s['fields'])} champs")
