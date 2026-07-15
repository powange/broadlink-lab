#!/usr/bin/env python3
"""
Le profil d'appareil — le seul lien entre les deux addons.

RF Lab en produit un à la fin d'une session de reverse ; RF Bridge le lit et en
fait un appareil Home Assistant. Les deux addons ne se parlent jamais : ils
s'échangent un fichier dans /share/rf_lab/.

Un profil contient TOUT ce qu'il faut pour piloter l'appareil, y compris la
capture de référence — c'est elle qui porte le préambule et l'ID appairé de la
télécommande. Le pont n'a donc besoin d'aucune capture, juste d'un RM4 pour
émettre. Corollaire : un profil est lié à UNE télécommande physique. Le copier
chez un voisin ne marcherait pas, son récepteur est appairé à un autre ID.

Format (version 1) :

    {
      "version": 1,
      "device":   {"id","name","manufacturer","model"},
      "rf":       {"frequency","gap","reference_b64"},
      "fields":   [{"name","start","end","msb_first","role","min","max"}],
      "checksum": {"kind","k"},
      "entities": [ ... voir ENTITY_TYPES ... ]
    }

`entities` décrit comment présenter les champs à Home Assistant. C'est la seule
partie « métier » : le reste est la carte des bits, produite par le labo.
"""

VERSION = 1

# Types d'entités que RF Bridge sait publier en MQTT discovery.
#   light  : power (champ 1 bit) + brightness + color_temp optionnels
#   fan    : power + percentage + direction + preset optionnels
#   number : un champ numérique brut, avec échelle (le timer : ×2 minutes)
#   switch : un champ 1 bit
ENTITY_TYPES = ("light", "fan", "number", "switch")


class ProfileError(ValueError):
    """Profil invalide — message destiné à l'utilisateur."""


def _field(fields, name):
    f = next((x for x in fields if x["name"] == name), None)
    if f is None:
        raise ProfileError(f"le champ « {name} » n'existe pas dans la carte")
    return f


def _check_ref(ref, fields, label):
    """Vérifie une référence {field, min?, max?} vers un champ de la carte."""
    if isinstance(ref, str):
        ref = {"field": ref}
    if not isinstance(ref, dict) or "field" not in ref:
        raise ProfileError(f"{label} : il faut au moins {{\"field\": \"...\"}}")
    f = _field(fields, ref["field"])
    if f.get("role") not in (None, "data"):
        raise ProfileError(
            f"{label} : « {ref['field']} » a le rôle {f.get('role')}, "
            f"seul un champ « data » peut être piloté")
    return ref


def validate(profile):
    """
    Valide un profil et retourne les erreurs (liste vide si tout va bien).

    Volontairement strict : un profil bancal ne se verrait qu'au moment où le
    ventilo n'obéit pas, sans le moindre message. Autant échouer au chargement.
    """
    errs = []
    try:
        if profile.get("version") != VERSION:
            errs.append(f"version {profile.get('version')} inconnue (attendu {VERSION})")

        dev = profile.get("device") or {}
        for k in ("id", "name"):
            if not dev.get(k):
                errs.append(f"device.{k} manquant")

        rf = profile.get("rf") or {}
        if not rf.get("reference_b64"):
            errs.append("rf.reference_b64 manquant — c'est lui qui porte l'ID appairé")
        if not rf.get("frequency"):
            errs.append("rf.frequency manquant")

        fields = profile.get("fields") or []
        if not fields:
            errs.append("fields vide")
        names = [f.get("name") for f in fields]
        if len(set(names)) != len(names):
            errs.append("deux champs portent le même nom")
        for f in fields:
            if f.get("end", 0) <= f.get("start", -1):
                errs.append(f"champ « {f.get('name')} » : tranche de bits invalide")

        if not any(f.get("role") == "crc" for f in fields) \
                and (profile.get("checksum") or {}).get("kind", "none") != "none":
            errs.append("un checksum est déclaré mais aucun champ n'a le rôle « crc »")

        ents = profile.get("entities") or []
        if not ents:
            errs.append("entities vide — le pont n'aurait rien à publier")
        for e in ents:
            t = e.get("type")
            if t not in ENTITY_TYPES:
                errs.append(f"type d'entité inconnu : {t}")
                continue
            try:
                if t in ("light", "fan", "switch"):
                    _check_ref(e.get("power"), fields, f"{t}.power")
                if t == "light":
                    if e.get("brightness"):
                        _check_ref(e["brightness"], fields, "light.brightness")
                    if e.get("color_temp"):
                        _check_ref(e["color_temp"], fields, "light.color_temp")
                if t == "fan":
                    if e.get("percentage"):
                        _check_ref(e["percentage"], fields, "fan.percentage")
                    if e.get("direction"):
                        _check_ref(e["direction"], fields, "fan.direction")
                    if e.get("preset"):
                        p = _check_ref(e["preset"], fields, "fan.preset")
                        if not e["preset"].get("options"):
                            errs.append("fan.preset : options manquantes")
                if t == "number":
                    _check_ref(e.get("field"), fields, "number.field")
            except ProfileError as exc:
                errs.append(str(exc))
    except Exception as exc:                  # noqa: BLE001
        errs.append(f"profil illisible : {exc}")
    return errs


def build(device, rf, fields, checksum, entities):
    """Assemble un profil. Lève ProfileError s'il ne valide pas."""
    p = {"version": VERSION, "device": device, "rf": rf, "fields": fields,
         "checksum": checksum, "entities": entities}
    errs = validate(p)
    if errs:
        raise ProfileError("; ".join(errs))
    return p


def data_fields(profile):
    """Les champs pilotables : ni const, ni crc."""
    return [f for f in profile["fields"] if f.get("role") not in ("const", "crc")]


def defaults(profile):
    """État de départ : les valeurs brutes lues dans la capture de référence."""
    import decoder
    pkt = decoder.decode_packet(profile["rf"]["reference_b64"])
    bits = decoder.pick_frame(
        decoder.decode_pwm(pkt["durations"], profile["rf"].get("gap", 2000)))
    return {f["name"]: decoder.field_value(bits, f["start"], f["end"],
                                           f.get("msb_first", True))
            for f in data_fields(profile)}
