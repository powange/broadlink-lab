#!/usr/bin/env python3
"""
RF Lab — addon Home Assistant.
Parle au RM4 Pro en direct (python-broadlink), sans passer par l'intégration HA :
- pas de learn_command asynchrone via notification
- pas de sweep de fréquence (on sait que c'est du 433,92)
- accès aux octets bruts immédiatement
"""

import json
import logging
import os
import threading
import time
import uuid

import broadlink
from broadlink.exceptions import ReadError, StorageError
from flask import Flask, jsonify, request, send_from_directory

import decoder
import discover
import profile as profile_mod

LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rflab")

# IP de départ, venue des options de l'addon. L'UI peut la changer (POST
# /api/device) et sa valeur est alors persistée dans le store : un RM4 en DHCP
# change d'adresse, et il ne faut pas avoir à éditer la config + redémarrer.
DEVICE_IP_OPTION = os.environ.get("DEVICE_IP") or None
FREQUENCY = float(os.environ.get("FREQUENCY", "433.92"))

# /data est le volume de l'addon. Surchargeable pour lancer l'app hors container
# (cf. dev/serve.py) — les valeurs par défaut sont celles de la prod.
STORE = os.environ.get("STORE", "/data/captures.json")
PORT = int(os.environ.get("PORT", "8099"))

app = Flask(__name__, static_folder=None)

_dev = None
_lock = threading.Lock()
_capture = {"state": "idle", "message": "", "result": None}
_cancel = threading.Event()
_connect_cancel = threading.Event()


# ------------------------------------------------------------ persistance

# Les paramètres que porte la trame absolue, tels qu'exposés par la RF00234.
# La trame contient TOUT l'état à chaque appui : un paramètre qui bouge sans être
# noté ici fait varier des bits sans corrélation apparente, ce qui imite à s'y
# méprendre un checksum (§7). Éditable depuis l'UI via POST /api/meta-schema.
#
# type: number | bool | enum   short: préfixe pour le nommage automatique
# always: toujours présent dans le nom auto, même à 0 (convention §9)
DEFAULT_META_SCHEMA = [
    # On/off : `always` car l'état ÉTEINT est aussi informatif que l'allumé, il
    # doit rester visible dans le nom. Question ouverte que le diff tranchera :
    # « lumière éteinte » est-il un bit dédié, ou juste lum=0 ? Idem ventilo /
    # vitesse=0. Tant qu'on ne sait pas, on note les deux séparément.
    {"key": "light", "label": "Lumière on", "type": "bool", "short": "light",
     "always": True},
    {"key": "lum", "label": "Luminosité", "type": "number", "short": "lum",
     "always": True},
    {"key": "cct", "label": "CCT (K)", "type": "number", "short": "cct",
     "always": True},
    {"key": "fan", "label": "Ventilo on", "type": "bool", "short": "fan",
     "always": True},
    {"key": "speed", "label": "Vitesse", "type": "number", "short": "v",
     "min": 0, "max": 8, "always": True},
    # toggle : noter l'état RÉSULTANT, pas le bouton pressé
    {"key": "reverse", "label": "Reverse", "type": "bool", "short": "rev"},
    # nuit et éco s'EXCLUENT : c'est un seul champ à 3 valeurs dans la trame
    # (bits 42-43), pas deux booléens. Les modéliser séparément laissait saisir
    # « nuit ET éco », un état que la télécommande ne peut pas produire.
    {"key": "mode", "label": "Mode moteur", "type": "enum", "short": "m",
     "options": ["normal", "nuit", "eco"]},
    {"key": "timer", "label": "Timer (h)", "type": "enum", "short": "t",
     "options": [0, 1, 2, 4, 8]},
]

DEFAULT_STORE = {"captures": [], "fields": [], "checksum": {"kind": "none"},
                 "meta_schema": DEFAULT_META_SCHEMA, "device_ip": None}


def load_store():
    if not os.path.exists(STORE):
        return json.loads(json.dumps(DEFAULT_STORE))     # copie profonde
    with open(STORE) as fh:
        store = json.load(fh)
    for key, default in DEFAULT_STORE.items():
        store.setdefault(key, json.loads(json.dumps(default)))
    return store


def save_store(data):
    tmp = STORE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, STORE)


# ------------------------------------------------------------ device

def device_ip():
    """L'UI l'emporte sur l'option de l'addon, qui l'emporte sur le broadcast."""
    return load_store().get("device_ip") or DEVICE_IP_OPTION


def get_device(force=False):
    global _dev
    if _dev is not None and not force:
        return _dev

    ip = device_ip()
    # Un Broadlink est un appareil WiFi qui dort : le premier paquet le réveille
    # et se perd. Un seul essai le déclare absent alors qu'il est là (§8).
    # Revers de la médaille : 3 essais × 6 s = 18 s bloqué sur une IP fautive.
    # D'où le drapeau d'annulation, testé ENTRE les essais — on ne peut pas
    # interrompre un recv() en cours, mais on peut refuser d'en lancer un autre.
    last = None
    for attempt in (1, 2, 3):
        if _connect_cancel.is_set():
            raise RuntimeError("connexion annulée")
        try:
            if ip:
                log.info("Connexion à %s (essai %d)", ip, attempt)
                dev = broadlink.hello(ip, timeout=discover.DEFAULT_TIMEOUT)
            else:
                log.info("Découverte broadcast (essai %d)…", attempt)
                found = broadlink.discover(timeout=discover.DEFAULT_TIMEOUT)
                if not found:
                    raise RuntimeError(
                        "aucun Broadlink trouvé en broadcast — renseigne l'IP "
                        "dans la barre d'état, ou lance une recherche")
                dev = found[0]
            dev.auth()
            log.info("Appairé : %s @ %s", dev.model, dev.host[0])
            _dev = dev
            return dev
        except Exception as exc:              # noqa: BLE001
            last = exc
            log.info("essai %d échoué (%s) — l'appareil dort peut-être", attempt, exc)
    if _connect_cancel.is_set():
        raise RuntimeError("connexion annulée")
    raise RuntimeError(f"{last} (3 essais — l'appareil est-il alimenté ?)")


# ------------------------------------------------------------ capture

def _leave_learning(dev):
    """
    Sort le RM4 du mode apprentissage RF (commande 0x1e). Sans ça il resterait
    en écoute après une annulation, et la capture suivante pourrait ramasser un
    appui qui ne lui était pas destiné.
    """
    try:
        dev.cancel_sweep_frequency()
    except Exception:                     # noqa: BLE001
        log.warning("sortie du mode apprentissage refusée", exc_info=True)


def capture_worker(timeout=30):
    global _capture
    dev = None
    try:
        dev = get_device()
        _capture = {"state": "listening", "message": f"Appuie sur la touche ({FREQUENCY} MHz)…", "result": None}
        dev.find_rf_packet(frequency=FREQUENCY)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if _cancel.is_set():
                _leave_learning(dev)
                _capture = {"state": "cancelled", "message": "Capture annulée",
                            "result": None}
                return
            try:
                data = dev.check_data()
            except (ReadError, StorageError):
                time.sleep(0.4)
                continue
            _capture = {"state": "done", "message": "Trame capturée",
                        "result": decoder.to_b64(data)}
            return

        if dev is not None:
            _leave_learning(dev)
        _capture = {"state": "timeout", "message": "Rien reçu", "result": None}
    except Exception as exc:              # noqa: BLE001
        log.exception("capture")
        _capture = {"state": "error", "message": str(exc), "result": None}


@app.post("/api/capture/start")
def capture_start():
    # Acquisition non bloquante dans le thread de la requête : atomique, là où
    # un test `_lock.locked()` suivi d'un sleep laissait passer deux captures
    # simultanées. Le verrou est relâché par le worker.
    if not _lock.acquire(blocking=False):
        return jsonify(error="capture déjà en cours"), 409

    _cancel.clear()
    global _capture
    _capture = {"state": "listening", "message": "Démarrage…", "result": None}

    def run():
        try:
            capture_worker()
        finally:
            _lock.release()

    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/capture/cancel")
def capture_cancel():
    if not _lock.locked():
        return jsonify(ok=True, message="aucune capture en cours")
    _cancel.set()
    return jsonify(ok=True)


@app.get("/api/capture/poll")
def capture_poll():
    return jsonify(_capture)


@app.post("/api/captures")
def add_capture():
    body = request.json or {}
    store = load_store()
    store["captures"].append({
        "id": uuid.uuid4().hex[:8],
        "name": body.get("name", "sans-nom"),
        "b64": body["b64"],
        "meta": body.get("meta", {}),   # {lum: 10, cct: 3000, speed: 0}
    })
    save_store(store)
    return jsonify(ok=True)


@app.delete("/api/captures/<cid>")
def del_capture(cid):
    store = load_store()
    store["captures"] = [c for c in store["captures"] if c["id"] != cid]
    save_store(store)
    return jsonify(ok=True)


# ------------------------------------------------------------ analyse

@app.get("/api/analyze")
def api_analyze():
    gap = int(request.args.get("gap", 2000))
    mode = request.args.get("mode", "pwm")
    store = load_store()

    rows, bitmap = [], {}
    for c in store["captures"]:
        pkt = decoder.decode_packet(c["b64"])
        frames = (decoder.decode_pwm(pkt["durations"], gap) if mode == "pwm"
                  else decoder.decode_manchester(pkt["durations"], gap))
        # trame majoritaire, pas frames[0] : la 1re répétition porte le bruit de
        # capture et fausserait l'alignement du diff (cf. decoder.pick_frame)
        bits = decoder.pick_frame(frames)
        agree = sum(1 for f in frames if f["bits"] == bits)
        bitmap[c["name"]] = bits
        rows.append({
            "id": c["id"], "name": c["name"], "meta": c["meta"],
            "bits": bits, "nframes": len(frames), "agree": agree,
            "ndur": len(pkt["durations"]),
            "durations": pkt["durations"][:80],
            "header": pkt["header"],
        })

    return jsonify(rows=rows,
                   analysis=decoder.analyze({k: v for k, v in bitmap.items() if v}),
                   fields=store.get("fields", []),
                   checksum=store.get("checksum", {"kind": "none"}),
                   meta_schema=store.get("meta_schema", DEFAULT_META_SCHEMA))


@app.get("/api/meta-schema")
def get_meta_schema():
    if request.args.get("defaults"):
        return jsonify(meta_schema=DEFAULT_META_SCHEMA)
    return jsonify(meta_schema=load_store().get("meta_schema", DEFAULT_META_SCHEMA))


@app.post("/api/meta-schema")
def set_meta_schema():
    """
    Persiste les paramètres d'état à saisir à chaque capture. Ne touche pas aux
    captures déjà enregistrées : leur `meta` garde les clés d'origine, et l'UI
    affiche simplement un trou pour un paramètre ajouté après coup.
    """
    body = request.json or {}
    schema = body.get("meta_schema")
    if not isinstance(schema, list) or not schema:
        return jsonify(error="meta_schema doit être une liste non vide"), 400
    for entry in schema:
        if not entry.get("key"):
            return jsonify(error="chaque paramètre a besoin d'une clé"), 400
        if entry.get("type") not in ("number", "bool", "enum"):
            return jsonify(error=f"type invalide pour {entry.get('key')}"), 400
        if entry.get("type") == "enum" and not entry.get("options"):
            return jsonify(error=f"{entry['key']} : enum sans options"), 400

    store = load_store()
    store["meta_schema"] = schema
    save_store(store)
    return jsonify(ok=True)


@app.get("/api/detect-checksum")
def api_detect_checksum():
    """
    Cherche le checksum qui explique toutes les captures, pour la tranche donnée.
    C'est le piège n°1 de §7 : sans ça, une trame générée avec la luminosité
    modifiée est simplement ignorée par le récepteur, sans le moindre indice.
    """
    start = int(request.args.get("start", 0))
    end = int(request.args.get("end", 0))
    gap = int(request.args.get("gap", 2000))
    if end <= start:
        return jsonify(error="tranche CRC invalide"), 400

    store = load_store()
    samples = []
    for c in store["captures"]:
        pkt = decoder.decode_packet(c["b64"])
        bits = decoder.pick_frame(decoder.decode_pwm(pkt["durations"], gap))
        if bits:
            samples.append(bits)

    res = decoder.detect_checksum(samples, start, end)
    res["samples"] = len(samples)
    return jsonify(res)


@app.post("/api/fields")
def set_fields():
    body = request.json or {}
    store = load_store()
    store["fields"] = body.get("fields", [])
    store["checksum"] = body.get("checksum", {"kind": "none"})
    save_store(store)
    return jsonify(ok=True)


# ------------------------------------------------------------ génération

def build_bits(ref_bits, fields, values, checksum):
    """
    Réécrit les champs demandés dans la trame de référence, puis recalcule le CRC.

    Un champ `const` n'est JAMAIS réécrit, même si l'appelant en donne une valeur :
    c'est là que vivent le préambule et l'ID appairé (§7 — y toucher et le ventilo
    ignore la trame), ainsi que les bits de contexte décoratifs.
    """
    bits = ref_bits
    for f in fields:
        if f["name"] in values and f.get("role") not in ("crc", "const"):
            val = int(values[f["name"]])
            if f.get("min") is not None:
                val = max(val, f["min"])
            if f.get("max") is not None:
                val = min(val, f["max"])
            bits = decoder.set_field(bits, f["start"], f["end"], val,
                                     f.get("msb_first", True))
    crc = next((f for f in fields if f.get("role") == "crc"), None)
    if crc and checksum.get("kind", "none") != "none":
        val = decoder.compute_checksum(bits, checksum["kind"], crc["start"],
                                       crc["end"], checksum.get("k", 0))
        bits = decoder.set_field(bits, crc["start"], crc["end"], val,
                                 crc.get("msb_first", True))
    return bits


@app.post("/api/generate")
def generate():
    body = request.json or {}
    gap = int(body.get("gap", 2000))
    store = load_store()

    ref = next((c for c in store["captures"] if c["id"] == body["ref_id"]), None)
    if not ref:
        return jsonify(error="référence introuvable"), 404

    pkt = decoder.decode_packet(ref["b64"])
    frames = decoder.decode_pwm(pkt["durations"], gap)
    if not frames:
        return jsonify(error="décodage PWM vide — ajuste le gap"), 400

    ref_bits = decoder.pick_frame(frames)
    new_bits = build_bits(ref_bits, store.get("fields", []),
                          body.get("values", {}),
                          store.get("checksum", {"kind": "none"}))

    durations = decoder.rebuild_frame(pkt["durations"], frames, new_bits, ref_bits)
    packet = decoder.encode_packet(durations, pkt["header"], pkt["repeats"],
                                   pkt.get("terminator", True))
    ok = decoder.verify_rebuild(durations, new_bits, gap)

    return jsonify(b64=decoder.to_b64(packet), bits=new_bits,
                   ref_bits=ref_bits, verified=ok)


def _describe(b64, gap=2000):
    """
    Décrit une trame par ses champs nommés. Un vrai RM4 n'accuse rien de ce qu'il
    émet : sans ça une session de reverse ne laisse aucune trace de ce qui est
    parti sur les ondes, et « ça a marché » devient invérifiable après coup.
    """
    try:
        store = load_store()
        pkt = decoder.decode_packet(b64)
        bits = decoder.pick_frame(decoder.decode_pwm(pkt["durations"], gap))
        if not bits:
            return "trame indécodable"
        parts = [f"{f['name']}="
                 f"{decoder.field_value(bits, f['start'], f['end'], f.get('msb_first', True))}"
                 for f in store.get("fields", [])
                 if f["name"] != "preambule" and f["end"] <= len(bits)]
        return " ".join(parts) or bits
    except Exception:                     # noqa: BLE001
        return "trame indécodable"


@app.post("/api/send")
def send():
    body = request.json or {}
    try:
        dev = get_device()
        import base64 as b64mod
        dev.send_data(b64mod.b64decode(body["b64"]))
        log.info("ÉMIS -> %s", _describe(body["b64"], int(body.get("gap", 2000))))
        return jsonify(ok=True)
    except Exception as exc:              # noqa: BLE001
        log.exception("send")
        return jsonify(error=str(exc)), 500


@app.post("/api/ref")
def set_ref():
    """Capture de référence par défaut pour /api/set."""
    body = request.json or {}
    store = load_store()
    if not any(c["id"] == body.get("ref_id") for c in store["captures"]):
        return jsonify(error="référence introuvable"), 404
    store["ref_id"] = body["ref_id"]
    save_store(store)
    return jsonify(ok=True)


@app.post("/api/set")
def api_set():
    """
    Générer + émettre en un appel : c'est le point d'entrée de Home Assistant.

    Le package HA ne peut pas embarquer les codes en dur — l'espace d'états
    dépasse 3,7 millions de combinaisons, parce que chaque trame porte TOUS les
    organes à la fois (§10). HA envoie donc l'état cible, l'addon fabrique la
    trame et l'émet.

    Corps : les valeurs BRUTES des champs, ex. {"light":1,"lum":5,"cct":4,
    "fan":1,"speed":3,"reverse":0,"mode":0,"timer":0}. Les champs omis gardent
    la valeur de la référence.
    """
    body = request.json or {}
    gap = int(body.get("gap", 2000))
    store = load_store()
    if not store["captures"]:
        return jsonify(error="aucune capture de référence enregistrée"), 400

    ref_id = body.get("ref_id") or store.get("ref_id")
    ref = next((c for c in store["captures"] if c["id"] == ref_id), None) \
        or store["captures"][0]

    values = {k: v for k, v in body.items()
              if k not in ("gap", "ref_id", "send")}
    fields = store.get("fields", [])
    known = {f["name"] for f in fields}
    unknown = set(values) - known
    if unknown:
        return jsonify(error=f"champs inconnus : {sorted(unknown)}",
                       known=sorted(known)), 400

    pkt = decoder.decode_packet(ref["b64"])
    frames = decoder.decode_pwm(pkt["durations"], gap)
    if not frames:
        return jsonify(error="décodage PWM vide — ajuste le gap"), 400

    ref_bits = decoder.pick_frame(frames)
    new_bits = build_bits(ref_bits, fields, values,
                          store.get("checksum", {"kind": "none"}))
    durations = decoder.rebuild_frame(pkt["durations"], frames, new_bits, ref_bits)
    if not decoder.verify_rebuild(durations, new_bits, gap):
        # ne JAMAIS émettre une trame qu'on n'a pas su re-décoder
        return jsonify(error="trame reconstruite non vérifiée — rien émis"), 500

    packet = decoder.encode_packet(durations, pkt["header"], pkt["repeats"],
                                   pkt.get("terminator", True))
    b64 = decoder.to_b64(packet)

    if body.get("send", True):
        try:
            dev = get_device()
            import base64 as b64mod
            dev.send_data(b64mod.b64decode(b64))
            log.info("ÉMIS -> %s", _describe(b64, gap))
        except Exception as exc:          # noqa: BLE001
            log.exception("set/send")
            return jsonify(error=str(exc)), 500

    return jsonify(ok=True, b64=b64, bits=new_bits, ref_bits=ref_bits,
                   state=_describe(b64, gap))


# ------------------------------------------------------------ profil d'appareil

PROFILE_DIR = os.environ.get("PROFILE_DIR", "/share/rf_lab")


@app.post("/api/profile")
def api_profile():
    """
    Fabrique le profil d'appareil — le livrable du labo.

    Il embarque la capture de référence : c'est elle qui porte le préambule et
    l'ID appairé de la télécommande. RF Bridge n'a donc besoin d'aucune capture,
    juste d'un RM4 pour émettre.

    `save=true` l'écrit dans /share/rf_lab/<id>.json, seul point de contact entre
    les deux addons.
    """
    body = request.json or {}
    store = load_store()

    ref_id = body.get("ref_id") or store.get("ref_id")
    ref = next((c for c in store["captures"] if c["id"] == ref_id), None) \
        or (store["captures"][0] if store["captures"] else None)
    if not ref:
        return jsonify(error="aucune capture — le profil a besoin d'une référence"), 400

    try:
        prof = profile_mod.build(
            device=body.get("device") or {},
            rf={"frequency": FREQUENCY,
                "gap": int(body.get("gap", 2000)),
                "reference_b64": ref["b64"],
                "reference_name": ref["name"]},
            fields=store.get("fields", []),
            checksum=store.get("checksum", {"kind": "none"}),
            entities=body.get("entities") or [],
        )
    except profile_mod.ProfileError as exc:
        return jsonify(error=str(exc)), 400

    saved = None
    if body.get("save"):
        try:
            os.makedirs(PROFILE_DIR, exist_ok=True)
            saved = os.path.join(PROFILE_DIR, f"{prof['device']['id']}.json")
            tmp = saved + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(prof, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, saved)
            log.info("profil écrit : %s", saved)
        except OSError as exc:
            return jsonify(error=f"écriture impossible dans {PROFILE_DIR} : {exc}"), 500

    return jsonify(ok=True, profile=prof, saved=saved)


@app.post("/api/profile/import")
def api_profile_import():
    """
    Charge un profil : sa carte de bits, son checksum, ses entités.

    Sert au backup, mais surtout au PARTAGE. Un profil mélange deux choses de
    nature différente :
      - la carte des bits, le checksum et les entités = le savoir sur le MODÈLE,
        identique pour tous les exemplaires ;
      - la capture de référence = l'ID appairé, propre à UNE télécommande.

    D'où `rebase` : on importe le savoir de quelqu'un d'autre, puis on capture UNE
    trame de sa propre télécommande pour l'ancrer. Zéro reverse à refaire.
    """
    body = request.json or {}
    prof = body.get("profile")
    if not isinstance(prof, dict):
        return jsonify(error="corps attendu : {\"profile\": {...}}"), 400
    errs = profile_mod.validate(prof)
    if errs:
        return jsonify(error="profil invalide : " + "; ".join(errs)), 400

    store = load_store()
    store["fields"] = prof["fields"]
    store["checksum"] = prof.get("checksum", {"kind": "none"})
    store["entities"] = prof.get("entities", [])
    store["device"] = prof.get("device", {})

    # La référence importée porte l'ID appairé de SON propriétaire. On ne la garde
    # que si l'appelant le demande explicitement — sinon il devra réancrer.
    imported_ref = None
    if body.get("keep_reference"):
        imported_ref = {
            "id": uuid.uuid4().hex[:8],
            "name": prof["rf"].get("reference_name", "référence importée"),
            "b64": prof["rf"]["reference_b64"],
            "meta": {},
        }
        store["captures"].append(imported_ref)
        store["ref_id"] = imported_ref["id"]

    save_store(store)
    return jsonify(ok=True, fields=len(prof["fields"]),
                   entities=len(prof.get("entities", [])),
                   device=prof.get("device", {}),
                   reference_kept=bool(imported_ref),
                   hint=None if imported_ref else
                   "Capture une trame de TA télécommande et choisis-la comme "
                   "référence : la carte des bits est bonne, seul l'ID appairé "
                   "diffère d'un exemplaire à l'autre.")


@app.get("/api/profiles")
def api_profiles():
    """Les profils déjà déposés dans /share, tels que RF Bridge les verra."""
    out = []
    try:
        for name in sorted(os.listdir(PROFILE_DIR)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(PROFILE_DIR, name)
            try:
                with open(path) as fh:
                    p = json.load(fh)
                out.append({"file": name, "device": p.get("device", {}),
                            "errors": profile_mod.validate(p)})
            except Exception as exc:      # noqa: BLE001
                out.append({"file": name, "device": {}, "errors": [str(exc)]})
    except FileNotFoundError:
        pass
    return jsonify(dir=PROFILE_DIR, profiles=out)


@app.post("/api/device")
def set_device():
    """
    Change l'IP du RM4 depuis l'UI, et la persiste.

    Un Broadlink est en DHCP : son adresse change. Sans ça il faudrait éditer les
    options de l'addon et le redémarrer à chaque bail renouvelé. Une IP vide
    revient à l'option de l'addon, puis au broadcast.
    """
    global _dev
    body = request.json or {}
    ip = (body.get("ip") or "").strip() or None

    store = load_store()
    store["device_ip"] = ip
    save_store(store)
    _dev = None                       # forcer la reconnexion sur la nouvelle IP
    _connect_cancel.clear()

    src = "ui" if ip else ("option" if DEVICE_IP_OPTION else "broadcast")
    try:
        dev = get_device(force=True)
        return jsonify(ok=True, connected=True, model=dev.model, host=dev.host[0],
                       frequency=FREQUENCY, ip=ip, ip_source=src)
    except Exception as exc:          # noqa: BLE001
        return jsonify(ok=True, connected=False, error=str(exc),
                       frequency=FREQUENCY, ip=ip, ip_source=src)


@app.post("/api/device/cancel")
def cancel_device():
    """
    Abandonne la connexion en cours.

    Un recv() bloqué ne s'interrompt pas de l'extérieur : le drapeau est testé
    entre les essais, donc l'abandon prend au pire le timeout d'un essai (6 s)
    au lieu des 18 s des trois.
    """
    _connect_cancel.set()
    return jsonify(ok=True)


@app.get("/api/discover")
def api_discover():
    """
    Cherche les Broadlink du réseau.

    D'abord en broadcast — ça marche depuis un addon HA (`host_network: true`).
    Si ça ne donne rien et qu'un `cidr` est fourni, on balaie la plage en unicast :
    c'est plus lent, mais c'est la seule méthode qui traverse un NAT (WSL2, §8).
    """
    cidr = request.args.get("cidr")
    found = discover.broadcast()
    method = "broadcast"
    if not found and cidr:
        log.info("broadcast vide — balayage unicast de %s", cidr)
        found = discover.sweep(cidr)
        method = "unicast"
    return jsonify(method=method, cidr=cidr, devices=found,
                   hint=None if found else
                   "Rien trouvé. Depuis WSL2 le broadcast ne traverse pas le NAT : "
                   "relance avec une plage (ex. 192.168.0.0/24) pour balayer en "
                   "unicast. Sinon, l'appareil dort peut-être — réessaie.")


@app.get("/api/status")
def status():
    ip = device_ip()
    src = ("ui" if load_store().get("device_ip")
           else "option" if DEVICE_IP_OPTION else "broadcast")
    try:
        dev = get_device()
        return jsonify(connected=True, model=dev.model, host=dev.host[0],
                       frequency=FREQUENCY, ip=ip, ip_source=src)
    except Exception as exc:              # noqa: BLE001
        return jsonify(connected=False, error=str(exc), frequency=FREQUENCY,
                       ip=ip, ip_source=src)


# ------------------------------------------------------------ static (ingress)

@app.get("/")
def index():
    return send_from_directory("www", "index.html")


@app.get("/<path:path>")
def static_files(path):
    return send_from_directory("www", path)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    # Home Assistant appelle /api/set en permanence (§10) : le serveur de dev de
    # Flask n'est pas fait pour ça — pas de file d'attente, pas de robustesse.
    # waitress est du Python pur, donc rien à compiler sur alpine.
    try:
        from waitress import serve
        log.info("waitress sur :%d", PORT)
        serve(app, host="0.0.0.0", port=PORT, threads=8, channel_timeout=30)
    except ImportError:
        log.warning("waitress absent — serveur de développement Flask")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
