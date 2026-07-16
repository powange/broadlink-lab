#!/usr/bin/env python3
"""
Le profil d'appareil — le seul lien entre les deux addons.

RF Lab en produit un à la fin d'une session de reverse ; RF Bridge le lit et en
fait un appareil Home Assistant. Les deux addons ne se parlent jamais : ils
s'échangent un fichier dans /share/broadlink_lab/.

Un profil contient TOUT ce qu'il faut pour piloter l'appareil, y compris la
capture de référence — c'est elle qui porte le préambule et l'ID appairé de la
télécommande. Le pont n'a donc besoin d'aucune capture, juste d'un RM4 pour
émettre. Corollaire : un profil est lié à UNE télécommande physique. Le copier
chez un voisin ne marcherait pas, son récepteur est appairé à un autre ID.

Format (version 2) :

    {
      "version": 2,
      "device":   {"id","name","manufacturer","model"},
      "rf":       {"frequency","gap","reference_b64"},
      "fields":   [{"name","start","end","msb_first","role","min","max",
                    "requires":{...}, "semantics":"absolute|toggle"}],
      "checksum": {"kind","k"},
      "entities": [ ... voir ENTITY_TYPES ... ]
    }

`entities` décrit comment présenter les champs à Home Assistant. C'est la seule
partie « métier » : le reste est la carte des bits, produite par le labo.

CE QUE LA TRAME ENCODE ≠ CE QUE LE RÉCEPTEUR EN FAIT

La v1 supposait qu'une trame porte l'état et que le récepteur l'applique en
entier. C'est vrai de la Mantra RF00234, et FAUX de la Mantra R00143 (CLAUDE.md
§10), dont le récepteur n'applique que les champs libres plus UN champ désigné
par l'octet de commande. Deux notions manquaient, et aucune ne se lit dans les
bits — seul le matériel les révèle :

  "requires":   {"cmd": 10}   ce champ ne s'applique que si `cmd` vaut 10.
                              Régler vitesse ET sens demande donc DEUX trames.
                              Absent = le champ s'applique toujours.

  "semantics":  "toggle"      le récepteur IGNORE la valeur du champ et BASCULE.
                              Il ne se règle pas, il s'inverse : n'émettre que si
                              l'état demandé diffère. Défaut : "absolute".

Un profil v1 reste lisible : sans `requires` ni `semantics`, le comportement est
celui d'avant. Mais un profil v2 chargé par un vieux pont s'appliquerait de
travers en silence — d'où le refus par le numéro de version, plutôt qu'un
ventilateur qui n'obéit qu'à moitié sans le moindre message.
"""

VERSION = 2
# Les versions qu'on sait lire. Un profil v1 n'a ni `requires` ni `semantics` :
# tous ses champs s'appliquent toujours, ce qui est exactement l'ancien modèle.
SUPPORTED = (1, 2)

# Ce que le RÉCEPTEUR fait d'un champ, indépendamment de ce que la trame encode.
#   absolute : il lit la valeur et s'y met (le cas courant)
#   toggle   : il ignore la valeur et bascule à chaque trame reçue
SEMANTICS = ("absolute", "toggle")

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


def _check_semantics(f):
    sem = f.get("semantics", "absolute")
    if sem not in SEMANTICS:
        return [f"champ « {f.get('name')} » : sémantique « {sem} » inconnue "
                f"(attendu : {', '.join(SEMANTICS)})"]
    if sem == "toggle" and f.get("role") not in (None, "data"):
        return [f"champ « {f.get('name')} » : un champ « {f.get('role')} » ne peut "
                f"pas être un toggle, il n'est jamais piloté"]
    return []


def _check_requires(f, fields):
    """
    `requires` désigne les valeurs qu'une trame doit porter pour que CE champ
    soit appliqué. Un profil qui se trompe ici ne se verrait qu'au ventilateur
    qui n'obéit qu'à moitié — sans message. Autant être strict au chargement.
    """
    req = f.get("requires")
    if req is None:
        return []
    if not isinstance(req, dict) or not req:
        return [f"champ « {f.get('name')} » : requires doit être un objet non vide, "
                f"ex. {{\"cmd\": 10}}"]
    errs = []
    for name, val in req.items():
        if name == f.get("name"):
            errs.append(f"champ « {f['name']} » : requires sur lui-même")
            continue
        try:
            target = _field(fields, name)
        except ProfileError as exc:
            errs.append(f"champ « {f.get('name')} » : requires — {exc}")
            continue
        width = target.get("end", 0) - target.get("start", 0)
        if not isinstance(val, int) or isinstance(val, bool) or not 0 <= val < 2 ** width:
            errs.append(f"champ « {f['name']} » : requires {name}={val!r} ne tient "
                        f"pas dans les {width} bits de « {name} »")
    return errs


def emit_groups(profile, applying=None):
    """
    Les trames à émettre pour appliquer les champs `applying` (des noms).

    Retourne une liste de contraintes : `[{}]` = une seule trame, telle quelle ;
    `[{"cmd": 10}, {"cmd": 12}]` = deux trames, l'une portant cmd=10, l'autre
    cmd=12.

    Le point : sur un récepteur à `requires`, une trame n'applique que les champs
    LIBRES plus celui que la contrainte désigne. Régler vitesse et sens demande
    donc deux trames. Les champs libres (le bloc lampe de la R00143) voyagent
    avec chacune — inutile de leur en dédier une.

    `applying=None` signifie « tout », TOGGLES EXCLUS : les inclure ferait
    basculer le sens de rotation à chaque resynchronisation.

    `applying=[]` — rien à appliquer — ne rend AUCUNE trame. C'est le cas quand
    HA redemande l'état courant d'un toggle : émettre « pour rien » y inverserait
    le ventilateur.
    """
    fields = data_fields(profile)
    if applying is None:
        applying = [f["name"] for f in fields if f.get("semantics") != "toggle"]
    applying = set(applying)
    if not applying:
        return []

    groups = []
    for f in fields:
        req = f.get("requires")
        if req and f["name"] in applying and req not in groups:
            groups.append(dict(req))
    # aucun champ exigeant : une trame suffit, elle porte déjà tout l'état
    return groups or [{}]


def toggles(profile):
    """Les champs que le récepteur bascule au lieu de les régler."""
    return [f["name"] for f in profile.get("fields", [])
            if f.get("semantics") == "toggle"]


def validate(profile):
    """
    Valide un profil et retourne les erreurs (liste vide si tout va bien).

    Volontairement strict : un profil bancal ne se verrait qu'au moment où le
    ventilo n'obéit pas, sans le moindre message. Autant échouer au chargement.
    """
    errs = []
    try:
        if profile.get("version") not in SUPPORTED:
            errs.append(f"version {profile.get('version')} inconnue "
                        f"(connues : {', '.join(map(str, SUPPORTED))})")

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
            errs += _check_semantics(f)
            errs += _check_requires(f, fields)

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
