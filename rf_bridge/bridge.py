#!/usr/bin/env python3
"""
RF Bridge — des profils d'appareil deviennent des appareils Home Assistant.

Générique par construction : le pont ne connaît aucune télécommande. Il lit les
profils de /share/broadlink_lab/, publie les entités en MQTT discovery, et traduit
chaque commande HA en trame RF.

**Il ne dépend pas de RF Lab.** Le labo sert à FABRIQUER un profil ; s'il en
existe déjà un (partagé, sauvegardé), le pont suffit — son UI permet de l'importer.
Le dossier partagé est neutre : ni l'un ni l'autre ne le possède.

Les profils sont rechargés à chaud : déposer un fichier suffit, pas de
redémarrage. Sans ça, « ajouter un appareil » imposerait d'aller redémarrer
l'addon depuis le panneau HA, sans que rien ne le dise.

DEUX CONTRAINTES DICTENT TOUTE L'ARCHITECTURE :

1. La trame est ABSOLUE et porte TOUS les organes. Il n'existe pas de commande
   « lampe seule » : émettre une luminosité impose aussi un état au ventilateur.
   Le pont tient donc l'état complet en mémoire et réémet TOUT à chaque commande.
   C'est aussi pourquoi l'état est persisté : au redémarrage, réémettre un état
   par défaut rallumerait la lampe de quelqu'un à 3 h du matin.

2. L'appareil n'accuse JAMAIS réception. L'état est optimiste — c'est ce que HA
   a demandé, pas ce que la machine fait. Un appui sur la télécommande physique
   désynchronise, et rien ne peut le corriger : le ventilo n'émet rien.
"""
import base64
import json
import logging
import os
import sys
import threading
import time

import broadlink
import paho.mqtt.client as mqtt
from flask import Flask, jsonify, request, send_from_directory

import decoder
import discover
import profile as profile_mod

LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rf_bridge")

# IP de départ, venue des options de l'addon. L'UI peut la changer et la valeur
# est persistée : un Broadlink est en DHCP, éditer la config de l'addon et le
# redémarrer à chaque bail renouvelé n'a pas de sens.
DEVICE_IP_OPTION = os.environ.get("DEVICE_IP") or None
PROFILE_DIR = os.environ.get("PROFILE_DIR", "/share/broadlink_lab")
STATE_DIR = os.environ.get("STATE_DIR", "/data")
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant")
BASE = os.environ.get("TOPIC_BASE", "rf_bridge")
PORT = int(os.environ.get("PORT", "8098"))
WATCH_SECONDS = float(os.environ.get("WATCH_SECONDS", "3"))

_dev = None
_dev_lock = threading.Lock()

# Le RM4 est HALF-DUPLEX : un seul émetteur-récepteur, donc il écoute OU il
# émet, jamais les deux. Un verrou unique pour la radio, et toute émission
# désarme l'écoute — le firmware ne dit pas si elle survit, on ne parie pas.
_radio = threading.RLock()
_listener = None

app = Flask(__name__, static_folder=None)
bridge = None                     # instance de Bridge, posée par main()


# ------------------------------------------------------------ Broadlink

CONFIG_PATH = os.path.join(STATE_DIR, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


def device_ip():
    """L'UI l'emporte sur l'option de l'addon, qui l'emporte sur le broadcast."""
    return load_config().get("device_ip") or DEVICE_IP_OPTION


def get_device(force=False):
    global _dev
    with _dev_lock:
        if _dev is not None and not force:
            return _dev
        ip = device_ip()
        # Un Broadlink est un appareil WiFi qui dort : le premier paquet le
        # réveille et se perd. Un seul essai le déclare absent alors qu'il est là.
        last = None
        for attempt in (1, 2, 3):
            try:
                if ip:
                    dev = broadlink.hello(ip, timeout=discover.DEFAULT_TIMEOUT)
                else:
                    found = broadlink.discover(timeout=discover.DEFAULT_TIMEOUT)
                    if not found:
                        raise RuntimeError(
                            "aucun Broadlink trouvé en broadcast — renseigne l'IP")
                    dev = found[0]
                dev.auth()
                log.info("Broadlink appairé : %s @ %s", dev.model, dev.host[0])
                _dev = dev
                return dev
            except Exception as exc:          # noqa: BLE001
                last = exc
                log.info("essai %d échoué (%s) — l'appareil dort peut-être",
                         attempt, exc)
        raise RuntimeError(f"{last} (3 essais — l'appareil est-il alimenté ?)")


# ------------------------------------------------------------ conversions

def _scale(value, src, dst):
    """Convertit linéairement entre deux plages, en bornant."""
    (a0, a1), (b0, b1) = src, dst
    if a1 == a0:
        return b0
    v = b0 + (value - a0) * (b1 - b0) / (a1 - a0)
    return max(min(round(v), max(b0, b1)), min(b0, b1))


def _rng(ref, fields):
    """Plage brute d'un champ référencé, en respectant ses bornes réelles."""
    if isinstance(ref, str):
        ref = {"field": ref}
    f = next(x for x in fields if x["name"] == ref["field"])
    lo = ref.get("min", f.get("min", 0))
    hi = ref.get("max", f.get("max", 2 ** (f["end"] - f["start"]) - 1))
    return ref["field"], lo, hi


# ------------------------------------------------------------ un appareil

class Device:
    """Un profil + son état courant + ses entités MQTT."""

    def __init__(self, prof, client):
        self.p = prof
        self.client = client
        self.id = prof["device"]["id"]
        self.fields = prof["fields"]
        self.gap = prof["rf"].get("gap", 2000)
        self.lock = threading.Lock()
        self.state_path = os.path.join(STATE_DIR, f"state_{self.id}.json")
        self.state = self._load_state()

    # ---- persistance de l'état
    def _load_state(self):
        base = profile_mod.defaults(self.p)
        self.listen = False
        try:
            with open(self.state_path) as fh:
                saved = json.load(fh)
            # `_listen` n'est pas un champ de la trame : c'est une fonction du
            # pont. Le souligné le sort de l'espace de noms des champs.
            self.listen = bool(saved.get("_listen", False))
            # ne garder que les champs qui existent encore dans le profil
            base.update({k: v for k, v in saved.items() if k in base})
            log.info("[%s] état restauré : %s", self.id, base)
        except (FileNotFoundError, json.JSONDecodeError):
            log.info("[%s] pas d'état sauvegardé, valeurs de la référence : %s",
                     self.id, base)
        return base

    def _save_state(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({**self.state, "_listen": self.listen}, fh)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("[%s] état non persisté : %s", self.id, exc)

    # ---- topics
    def t(self, *parts):
        return "/".join((BASE, self.id) + parts)

    @property
    def ha_device(self):
        d = self.p["device"]
        return {"identifiers": [self.id], "name": d["name"],
                "manufacturer": d.get("manufacturer", "?"),
                "model": d.get("model", "?"),
                "via_device": "rf_bridge"}

    # ---- émission
    def emit(self, applying=None):
        """
        Émet le ou les trames qui appliquent l'état courant.

        UNE trame ne suffit pas toujours. Sur un récepteur à `requires` (Mantra
        R00143), elle porte tout l'état mais l'appareil n'en retient que les
        champs libres plus celui que l'octet de commande désigne : régler vitesse
        ET sens en demande deux. `applying` limite l'émission aux champs qui
        changent vraiment, pour ne pas émettre quatre trames là où une suffit.
        """
        ok = True
        for req in profile_mod.emit_groups(self.p, applying):
            ok = self._emit_one(req) and ok
        return ok

    def _emit_one(self, require=None):
        """
        Fabrique UNE trame de l'état courant, sous contrainte, et l'émet.

        On repart toujours de la capture de référence : elle porte le préambule
        et l'ID appairé, qu'on ne doit jamais reconstruire (§3.2 / §7).
        """
        pkt = decoder.decode_packet(self.p["rf"]["reference_b64"])
        frames = decoder.decode_pwm(pkt["durations"], self.gap)
        ref_bits = decoder.pick_frame(frames)

        bits = ref_bits
        for f in profile_mod.data_fields(self.p):
            if f["name"] in self.state:
                bits = decoder.set_field(bits, f["start"], f["end"],
                                         int(self.state[f["name"]]),
                                         f.get("msb_first", True))
        # La contrainte prime, et elle écrit MÊME un champ `const` : l'octet de
        # commande est constant du point de vue de l'état — il ne décrit rien de
        # l'appareil — mais c'est lui qui décide de ce que le récepteur applique.
        for name, val in (require or {}).items():
            t = next((x for x in self.fields if x["name"] == name), None)
            if t is None:
                log.error("[%s] requires -> champ « %s » absent de la carte",
                          self.id, name)
                return False
            bits = decoder.set_field(bits, t["start"], t["end"], int(val),
                                     t.get("msb_first", True))

        crc = next((f for f in self.fields if f.get("role") == "crc"), None)
        ck = self.p.get("checksum") or {"kind": "none"}
        if crc and ck.get("kind", "none") != "none":
            bits = decoder.set_field(
                bits, crc["start"], crc["end"],
                decoder.compute_checksum(bits, ck["kind"], crc["start"],
                                         crc["end"], ck.get("k", 0)),
                crc.get("msb_first", True))

        durations = decoder.rebuild_frame(pkt["durations"], frames, bits, ref_bits)
        if not decoder.verify_rebuild(durations, bits, self.gap):
            # ne jamais émettre une trame qu'on n'a pas su re-décoder
            log.error("[%s] trame non vérifiée — rien émis", self.id)
            return False
        data = decoder.encode_packet(durations, pkt["header"], pkt["repeats"],
                                     pkt.get("terminator", True))
        try:
            with _radio:
                if _listener is not None:
                    _listener.disarm()
                get_device().send_data(data)
            log.info("[%s] émis%s -> %s", self.id,
                     " " + repr(require) if require else "", self.state)
            return True
        except Exception as exc:              # noqa: BLE001
            log.exception("[%s] émission impossible : %s", self.id, exc)
            return False

    # ---- écoute : suivre la VRAIE télécommande
    def absorb(self, b64):
        """
        Cette trame vient-elle de MA télécommande ? Si oui, en adopter l'état.

        Le discriminateur, ce sont les champs marqués `identity` : le préambule
        et l'ID appairé. Trois filtres indépendants — longueur, identité
        identique à la référence, checksum juste — ne laissent aucune chance de
        confondre deux appareils.

        PAS « tous les champs const ». `const` veut dire « ne réécris pas », et
        l'octet de commande est const tout en changeant à chaque bouton pressé :
        matcher dessus faisait rejeter au pont ses PROPRES trames.

        Retourne True si la trame était pour nous.

        LES TOGGLES SONT EXCLUS, et ce n'est pas un détail : le bit d'un champ
        toggle dit ce que la TÉLÉCOMMANDE croit. Dès que le pont émet, l'appareil
        bascule sans qu'elle le sache, et sa croyance devient fausse. L'adopter
        propagerait son erreur dans HA.
        """
        try:
            pkt = decoder.decode_packet(b64)
            bits = decoder.pick_frame(decoder.decode_pwm(pkt["durations"], self.gap))
            ref = decoder.pick_frame(decoder.decode_pwm(
                decoder.decode_packet(self.p["rf"]["reference_b64"])["durations"],
                self.gap))
        except Exception:                     # noqa: BLE001
            return False
        if not bits or len(bits) != len(ref):
            return False
        ident = profile_mod.identity(self.p)
        if not ident:
            return False                      # cf. Listener : dit une fois pourquoi
        for f in ident:
            if bits[f["start"]:f["end"]] != ref[f["start"]:f["end"]]:
                return False

        crc = next((f for f in self.fields if f.get("role") == "crc"), None)
        ck = self.p.get("checksum") or {"kind": "none"}
        if crc and ck.get("kind", "none") != "none":
            want = decoder.compute_checksum(bits, ck["kind"], crc["start"],
                                            crc["end"], ck.get("k", 0))
            if want != decoder.field_value(bits, crc["start"], crc["end"],
                                           crc.get("msb_first", True)):
                return False

        tog = set(profile_mod.toggles(self.p))
        heard = {f["name"]: decoder.field_value(bits, f["start"], f["end"],
                                                f.get("msb_first", True))
                 for f in profile_mod.data_fields(self.p) if f["name"] not in tog}
        with self.lock:
            changed = {k: v for k, v in heard.items() if self.state.get(k) != v}
            if not changed:
                return True                   # déjà à jour : rien à republier
            self.state.update(changed)
            self._save_state()
        log.info("[%s] télécommande entendue -> %s", self.id, changed)
        self.publish_state()                  # et surtout : PAS de réémission
        return True

    def set_listen(self, on):
        with self.lock:
            self.listen = bool(on)
            self._save_state()
        log.info("[%s] écoute continue %s", self.id, "activée" if on else "coupée")
        self.publish_state()

    def apply(self, changes):
        """Applique des valeurs brutes, émet, republie l'état."""
        with self.lock:
            # Un champ toggle ne se RÈGLE pas, il se BASCULE : le récepteur
            # ignore sa valeur et inverse à chaque trame. En émettre une alors
            # que HA redemande le sens courant inverserait le ventilateur pour
            # rien. On ne l'applique donc que s'il change vraiment.
            tog = set(profile_mod.toggles(self.p))
            applying = [k for k, v in changes.items()
                        if k not in tog or self.state.get(k) != v]
            self.state.update(changes)
            self._save_state()
            self.emit(applying)
        self.publish_state()

    # ---- discovery + état
    def publish_discovery(self):
        for i, e in enumerate(self.p["entities"]):
            cfg, comp, oid = self._discovery(e, i)
            topic = f"{DISCOVERY_PREFIX}/{comp}/{self.id}/{oid}/config"
            self.client.publish(topic, json.dumps(cfg), retain=True)
            log.info("[%s] discovery %s -> %s", self.id, comp, topic)
        # Ce switch ne décrit aucun champ de la trame : c'est un réglage du pont.
        # Il est par appareil parce que le coût l'est aussi — écouter monopolise
        # le RM4, et personne ne doit le payer sans l'avoir demandé.
        self.client.publish(
            f"{DISCOVERY_PREFIX}/switch/{self.id}/listen/config",
            json.dumps({"unique_id": f"{self.id}_listen", "device": self.ha_device,
                        "availability_topic": f"{BASE}/status",
                        "name": "Suivre la télécommande",
                        "icon": "mdi:remote",
                        "entity_category": "config",
                        "command_topic": self.t("listen", "set"),
                        "state_topic": self.t("listen", "state"),
                        "payload_on": "ON", "payload_off": "OFF"}), retain=True)

    def _discovery(self, e, i):
        t = e["type"]
        oid = e.get("id") or f"{t}{i or ''}"
        base = {"unique_id": f"{self.id}_{oid}", "device": self.ha_device,
                "availability_topic": f"{BASE}/status",
                "name": e.get("name")}

        if t == "light":
            cfg = {**base, "schema": "json",
                   "command_topic": self.t(oid, "set"),
                   "state_topic": self.t(oid, "state")}
            modes = []
            if e.get("brightness"):
                cfg["brightness"] = True
            if e.get("color_temp"):
                ct = e["color_temp"]
                kmin, kmax = ct.get("kelvin", [3000, 5000])
                cfg["supported_color_modes"] = ["color_temp"]
                cfg["min_mireds"] = round(1000000 / kmax)
                cfg["max_mireds"] = round(1000000 / kmin)
                modes = ["color_temp"]
            if e.get("brightness") and not modes:
                cfg["supported_color_modes"] = ["brightness"]
            return cfg, "light", oid

        if t == "fan":
            cfg = {**base,
                   "command_topic": self.t(oid, "set"),
                   "state_topic": self.t(oid, "state")}
            if e.get("percentage"):
                _, lo, hi = _rng(e["percentage"], self.fields)
                # HA réserve la valeur SOUS speed_range_min pour « éteint » et
                # exige donc un minimum >= 1. Sur un ventilateur sans bit
                # d'alimentation, la vitesse 0 EST l'arrêt : la plage HA commence
                # à 1, et le 0 lui sert justement d'« éteint ». Publier 0 ici
                # faisait rejeter TOUTE l'entité par HA, en silence.
                cfg.update(percentage_command_topic=self.t(oid, "pct", "set"),
                           percentage_state_topic=self.t(oid, "pct", "state"),
                           speed_range_min=max(1, lo),
                           speed_range_max=max(hi, max(1, lo)))
            if e.get("direction"):
                cfg.update(direction_command_topic=self.t(oid, "dir", "set"),
                           direction_state_topic=self.t(oid, "dir", "state"))
            if e.get("preset"):
                cfg.update(preset_mode_command_topic=self.t(oid, "preset", "set"),
                           preset_mode_state_topic=self.t(oid, "preset", "state"),
                           preset_modes=list(e["preset"]["options"]))
            return cfg, "fan", oid

        if t == "number":
            _, lo, hi = _rng(e["field"], self.fields)
            sc = e.get("scale", 1)
            cfg = {**base, "command_topic": self.t(oid, "set"),
                   "state_topic": self.t(oid, "state"),
                   "min": lo * sc, "max": hi * sc, "step": sc,
                   "mode": "box"}
            if e.get("unit"):
                cfg["unit_of_measurement"] = e["unit"]
            return cfg, "number", oid

        if t == "switch":
            return ({**base, "command_topic": self.t(oid, "set"),
                     "state_topic": self.t(oid, "state"),
                     "payload_on": "ON", "payload_off": "OFF"}, "switch", oid)

        raise profile_mod.ProfileError(f"type d'entité inconnu : {t}")

    def publish_state(self):
        self.client.publish(self.t("listen", "state"),
                            "ON" if self.listen else "OFF", retain=True)
        for i, e in enumerate(self.p["entities"]):
            t, oid = e["type"], (e.get("id") or f"{e['type']}{i or ''}")
            s = self.state

            if t == "light":
                pf, _, _ = _rng(e["power"], self.fields)
                pl = {"state": "ON" if s.get(pf) else "OFF"}
                if e.get("brightness"):
                    f, lo, hi = _rng(e["brightness"], self.fields)
                    pl["brightness"] = _scale(s.get(f, lo), (lo, hi), (1, 255))
                if e.get("color_temp"):
                    f, lo, hi = _rng(e["color_temp"], self.fields)
                    kmin, kmax = e["color_temp"].get("kelvin", [3000, 5000])
                    kelvin = _scale(s.get(f, lo), (lo, hi), (kmin, kmax))
                    pl["color_temp"] = round(1000000 / max(kelvin, 1))
                    pl["color_mode"] = "color_temp"
                self.client.publish(self.t(oid, "state"), json.dumps(pl), retain=True)

            elif t == "fan":
                # sans bit d'alimentation, l'allumage se lit sur la vitesse
                if e.get("power"):
                    on = bool(s.get(_rng(e["power"], self.fields)[0]))
                else:
                    on = bool(s.get(_rng(e["percentage"], self.fields)[0], 0))
                self.client.publish(self.t(oid, "state"),
                                    "ON" if on else "OFF", retain=True)
                if e.get("percentage"):
                    f, lo, hi = _rng(e["percentage"], self.fields)
                    self.client.publish(self.t(oid, "pct", "state"),
                                        str(s.get(f, lo)), retain=True)
                if e.get("direction"):
                    f, _, _ = _rng(e["direction"], self.fields)
                    self.client.publish(self.t(oid, "dir", "state"),
                                        "reverse" if s.get(f) else "forward", retain=True)
                if e.get("preset"):
                    f, _, _ = _rng(e["preset"], self.fields)
                    opts = e["preset"]["options"]
                    idx = int(s.get(f, 0))
                    self.client.publish(self.t(oid, "preset", "state"),
                                        opts[idx] if idx < len(opts) else opts[0],
                                        retain=True)

            elif t == "number":
                f, _, _ = _rng(e["field"], self.fields)
                self.client.publish(self.t(oid, "state"),
                                    str(s.get(f, 0) * e.get("scale", 1)), retain=True)

            elif t == "switch":
                pf, _, _ = _rng(e["power"], self.fields)
                self.client.publish(self.t(oid, "state"),
                                    "ON" if s.get(pf) else "OFF", retain=True)

    # ---- commandes
    def subscribe(self):
        self.client.subscribe(self.t("#"))
        log.info("[%s] écoute %s", self.id, self.t("#"))

    def on_message(self, topic, payload):
        parts = topic.split("/")
        if len(parts) < 4 or parts[-1] != "set":
            return
        oid = parts[2]
        if oid == "listen":               # réglage du pont, pas une entité du profil
            self.set_listen(payload == "ON")
            return
        e = next((x for i, x in enumerate(self.p["entities"])
                  if (x.get("id") or f"{x['type']}{i or ''}") == oid), None)
        if e is None:
            return
        sub = parts[3] if len(parts) > 4 else None
        try:
            changes = self._decode_cmd(e, sub, payload)
        except Exception as exc:              # noqa: BLE001
            log.warning("[%s] commande illisible sur %s : %s", self.id, topic, exc)
            return
        if changes:
            self.apply(changes)

    def _decode_cmd(self, e, sub, payload):
        t = e["type"]
        ch = {}

        if t == "light":
            pl = json.loads(payload)
            pf, _, _ = _rng(e["power"], self.fields)
            if "state" in pl:
                ch[pf] = 1 if pl["state"] == "ON" else 0
            if "brightness" in pl and e.get("brightness"):
                f, lo, hi = _rng(e["brightness"], self.fields)
                ch[f] = _scale(pl["brightness"], (1, 255), (lo, hi))
                ch[pf] = 1
            if "color_temp" in pl and e.get("color_temp"):
                f, lo, hi = _rng(e["color_temp"], self.fields)
                kmin, kmax = e["color_temp"].get("kelvin", [3000, 5000])
                kelvin = 1000000 / max(int(pl["color_temp"]), 1)
                ch[f] = _scale(kelvin, (kmin, kmax), (lo, hi))
            return ch

        if t == "fan":
            # Sans champ `power`, « éteint » s'écrit « vitesse 0 » : c'est la
            # Mantra R00143, qui n'a pas de bit d'alimentation ventilateur. Le
            # niveau y est alors PERDU en s'éteignant, contrairement à la RF00234.
            pf = _rng(e["power"], self.fields)[0] if e.get("power") else None
            sf, slo, shi = (_rng(e["percentage"], self.fields)
                            if e.get("percentage") else (None, 0, 0))
            if sub is None:
                on = payload == "ON"
                if pf:
                    ch[pf] = 1 if on else 0
                elif sf:
                    # rallumer sans bit d'alimentation : il faut choisir une
                    # vitesse, l'appareil n'a pas gardé la précédente
                    ch[sf] = max(slo, 1) if on else 0
            elif sub == "pct":
                f, lo, hi = _rng(e["percentage"], self.fields)
                v = int(payload)
                # 0 % vaut extinction côté HA
                if v <= 0:
                    ch[pf if pf else f] = 0
                else:
                    ch[f] = max(max(lo, 1), min(v, hi))
                    if pf:
                        ch[pf] = 1
            elif sub == "dir":
                f, _, _ = _rng(e["direction"], self.fields)
                ch[f] = 1 if payload == "reverse" else 0
            elif sub == "preset":
                f, _, _ = _rng(e["preset"], self.fields)
                opts = e["preset"]["options"]
                if payload in opts:
                    ch[f] = opts.index(payload)
            return ch

        if t == "number":
            f, lo, hi = _rng(e["field"], self.fields)
            sc = e.get("scale", 1)
            ch[f] = max(lo, min(round(float(payload) / sc), hi))
            return ch

        if t == "switch":
            pf, _, _ = _rng(e["power"], self.fields)
            ch[pf] = 1 if payload == "ON" else 0
            return ch

        return ch


# ------------------------------------------------------------ le pont

def read_profile(path):
    """Lit et valide un fichier. Retourne (profil|None, [erreurs])."""
    try:
        with open(path) as fh:
            p = json.load(fh)
    except json.JSONDecodeError as exc:
        return None, [f"JSON invalide : {exc}"]
    except OSError as exc:
        return None, [f"illisible : {exc}"]
    errs = profile_mod.validate(p)
    return (None, errs) if errs else (p, [])


class Listener:
    """
    Écoute continue : suivre la VRAIE télécommande.

    C'est ce qui enlève le mot « optimiste » du README. Sans elle, un appui sur
    la télécommande physique désynchronise Home Assistant pour toujours — le
    ventilateur n'accuse jamais réception, donc rien ne peut le corriger.

    TROIS CHOIX, ET ILS SE TIENNENT :

    1. **Un switch par appareil, éteint par défaut.** Écouter monopolise le RM4 :
       ni le labo ni l'intégration Broadlink de HA ne pourront s'en servir
       pendant ce temps. Personne ne doit payer ça sans l'avoir demandé. Si aucun
       appareil n'est suivi, ce fil ne touche PAS la radio.

    2. **Le RM4 est half-duplex.** Émettre coupe l'écoute — on ne parie pas sur
       sa survie, on réarme systématiquement après. La fenêtre d'aveuglement fait
       quelques dizaines de ms, et le pont n'émet que sur commande.

    3. **Mono-fréquence.** `find_rf_packet` accorde le récepteur sur UNE
       fréquence. Deux appareils suivis sur des fréquences différentes sont
       impossibles à écouter ensemble : on le dit plutôt que d'en ignorer un en
       silence.
    """

    POLL = 0.4          # comme capture_worker : le RM4 ne rend rien plus vite

    def __init__(self, bridge):
        self.bridge = bridge
        self.stop = threading.Event()
        self._armed = False
        self._freq = None

    def disarm(self):
        """
        Appelé par toute émission, sous le verrou radio : le RM4 ne fait pas les
        deux. On le sort proprement du mode écoute — laisser une session ouverte
        empêcherait le prochain réarmement (cf. le cancel après capture).
        """
        if self._armed:
            try:
                get_device().cancel_sweep_frequency()
            except Exception:                 # noqa: BLE001
                pass
        self._armed = False

    def watched(self):
        return [d for d in list(self.bridge.devices.values()) if d.listen]

    def frequency(self, devs):
        """
        La fréquence à écouter, et la liste des appareils qu'on laisse de côté.

        Un récepteur ne s'accorde que sur une fréquence : la majorité l'emporte,
        et on nomme les exclus. Les taire serait pire — ils seraient « suivis »
        dans l'UI sans que rien n'arrive jamais.
        """
        freqs = {}
        for d in devs:
            freqs.setdefault(d.p["rf"].get("frequency", 433.92), []).append(d)
        best = max(freqs, key=lambda f: len(freqs[f]))
        left = [d.id for f, ds in freqs.items() if f != best for d in ds]
        return best, left

    def run(self):
        left_warned = mute_warned = None
        while not self.stop.wait(self.POLL):
            devs = self.watched()
            mute = [d.id for d in devs if not profile_mod.identity(d.p)]
            if mute != mute_warned:
                mute_warned = mute
                if mute:
                    log.warning("%s est suivi mais son profil ne marque aucun champ "
                                "`identity` : impossible de reconnaître ses trames",
                                ", ".join(mute))
            if not devs:
                self._armed = False           # radio libre : on n'y touche pas
                continue
            freq, left = self.frequency(devs)
            if left != left_warned:
                left_warned = left
                if left:
                    log.warning("écoute sur %s MHz : %s est sur une autre "
                                "fréquence et ne sera PAS suivi", freq, ", ".join(left))
            try:
                with _radio:
                    dev = get_device()
                    if not self._armed or freq != self._freq:
                        dev.find_rf_packet(frequency=freq)
                        self._armed, self._freq = True, freq
                        continue              # rien à lire au premier tour
                    data = dev.check_data()
            except broadlink.exceptions.StorageError:
                continue                      # rien reçu encore : le cas normal
            except Exception as exc:          # noqa: BLE001
                log.info("écoute : %s", exc)
                self._armed = False
                continue

            # Une capture consomme l'armement. Et le RM4 Pro ne se laisse PAS
            # réarmer tant que la session d'écoute n'est pas close : sans ce
            # cancel, la première trame passe puis plus rien (le bug « marche une
            # fois »). Le labo et find_frequency.py cancellent déjà ; l'écoute le
            # devait aussi.
            try:
                with _radio:
                    dev.cancel_sweep_frequency()
            except Exception:                 # noqa: BLE001
                pass
            self._armed = False
            b64 = base64.b64encode(data).decode()
            for d in devs:
                if d.absorb(b64):
                    break
            else:
                log.info("trame entendue, aucun profil suivi ne la reconnaît "
                         "(%d octets)", len(data))


class Bridge:
    """
    Tient les appareils, le broker, et surveille le dossier des profils.

    Le rechargement à chaud n'est pas un luxe : sans lui, ajouter un appareil
    imposerait de redémarrer l'addon depuis le panneau HA — et rien dans l'UI
    ne le dirait.
    """

    def __init__(self, client):
        self.client = client
        self.devices = {}         # id -> Device
        self.errors = {}          # fichier -> [erreurs] (pour l'UI)
        self.seen = {}            # fichier -> mtime
        self.lock = threading.Lock()
        self.connected = False

    def wanted(self):
        w = [p for p in (os.environ.get("PROFILES") or "").split(",") if p]
        return set(w)

    def scan(self):
        """Ce qui est sur disque, maintenant."""
        out, errors = {}, {}
        try:
            files = sorted(f for f in os.listdir(PROFILE_DIR) if f.endswith(".json"))
        except FileNotFoundError:
            return out, errors
        for name in files:
            path = os.path.join(PROFILE_DIR, name)
            prof, errs = read_profile(path)
            if errs:
                # un profil bancal ne se verrait qu'au moment où l'appareil
                # n'obéit pas : on le refuse et on dit pourquoi, dans l'UI
                errors[name] = errs
                continue
            w = self.wanted()
            if w and prof["device"]["id"] not in w:
                errors[name] = ["ignoré : absent de l'option `profiles`"]
                continue
            out[prof["device"]["id"]] = (path, prof)
        return out, errors

    def reload(self):
        """Applique le disque : ajoute, met à jour, retire."""
        with self.lock:
            found, errors = self.scan()
            self.errors = errors
            changed = []

            for did in list(self.devices):
                if did not in found:
                    self.remove_device(did)
                    changed.append(f"-{did}")

            for did, (path, prof) in found.items():
                mtime = os.path.getmtime(path)
                if did in self.devices and self.seen.get(path) == mtime:
                    continue
                self.seen[path] = mtime
                d = Device(prof, self.client)
                # garder l'état courant si l'appareil existait déjà : recharger
                # un profil ne doit pas réémettre un état arbitraire
                if did in self.devices:
                    old = self.devices[did].state
                    d.state.update({k: v for k, v in old.items() if k in d.state})
                self.devices[did] = d
                if self.connected:
                    # s'abonner AVANT d'annoncer : HA peut envoyer une
                    # commande dès qu'il voit la discovery, et sans abonnement
                    # elle tombe dans le vide, en silence.
                    d.subscribe()
                    d.publish_discovery()
                    d.publish_state()
                changed.append(f"+{did}")

            if changed:
                log.info("profils rechargés : %s", " ".join(changed))
            return changed

    def remove_device(self, did):
        """Retire l'appareil de HA : une config vide et retenue l'efface."""
        d = self.devices.pop(did, None)
        if not d:
            return
        # ne pas laisser traîner le chemin dans `seen` : la signature comparée
        # par watch() finirait par mentir
        self.seen = {p: m for p, m in self.seen.items()
                     if os.path.basename(p) != f"{did}.json"}
        for i, e in enumerate(d.p["entities"]):
            _, comp, oid = d._discovery(e, i)
            self.client.publish(f"{DISCOVERY_PREFIX}/{comp}/{did}/{oid}/config",
                                "", retain=True)
        self.client.unsubscribe(d.t("#"))
        log.info("[%s] retiré de HA", did)

    def watch(self):
        """Surveille le dossier. Un simple sondage : pas de dépendance en plus."""
        while True:
            time.sleep(WATCH_SECONDS)
            try:
                found, _ = self.scan()
                sig = {p: os.path.getmtime(p) for p, _ in found.values()}
                if sig != {p: m for p, m in self.seen.items() if p in sig} \
                        or set(found) != set(self.devices):
                    self.reload()
            except Exception:                 # noqa: BLE001
                log.exception("surveillance des profils")


# ------------------------------------------------------------ UI (ingress)

@app.get("/api/status")
def api_status():
    b = bridge
    ip = device_ip()
    src = ("ui" if load_config().get("device_ip")
           else "option" if DEVICE_IP_OPTION else "broadcast")
    rm4 = {"connected": False, "error": None, "ip": ip, "ip_source": src}
    try:
        dev = get_device()
        rm4.update(connected=True, model=dev.model, host=dev.host[0])
    except Exception as exc:                  # noqa: BLE001
        rm4["error"] = str(exc)
    return jsonify(
        mqtt=b.connected, rm4=rm4, dir=PROFILE_DIR,
        devices=[{"id": d.id, "name": d.p["device"]["name"],
                  "manufacturer": d.p["device"].get("manufacturer"),
                  "model": d.p["device"].get("model"),
                  "entities": [e["type"] for e in d.p["entities"]],
                  "state": d.state}
                 for d in b.devices.values()],
        errors=b.errors)


@app.post("/api/device")
def api_device():
    """Change l'IP du Broadlink depuis l'UI, et la persiste."""
    global _dev
    body = request.json or {}
    ip = (body.get("ip") or "").strip() or None
    cfg = load_config()
    cfg["device_ip"] = ip
    save_config(cfg)
    _dev = None
    src = "ui" if ip else ("option" if DEVICE_IP_OPTION else "broadcast")
    try:
        dev = get_device(force=True)
        return jsonify(ok=True, connected=True, model=dev.model, host=dev.host[0],
                       ip=ip, ip_source=src)
    except Exception as exc:                  # noqa: BLE001
        return jsonify(ok=True, connected=False, error=str(exc), ip=ip, ip_source=src)


@app.get("/api/discover")
def api_discover():
    """Cherche les Broadlink. Broadcast d'abord ; unicast si un cidr est fourni."""
    cidr = request.args.get("cidr")
    found = discover.broadcast()
    method = "broadcast"
    if not found and cidr:
        found = discover.sweep(cidr)
        method = "unicast"
    return jsonify(method=method, devices=found)


@app.post("/api/profiles")
def api_import():
    """
    Importe un profil. C'est ce qui rend le pont autonome : sans ça, déposer un
    profil imposerait d'installer RF Lab juste pour écrire un fichier.
    """
    body = request.json or {}
    prof = body.get("profile")
    if not isinstance(prof, dict):
        return jsonify(error='corps attendu : {"profile": {...}}'), 400
    errs = profile_mod.validate(prof)
    if errs:
        return jsonify(error="profil invalide : " + "; ".join(errs)), 400
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        path = os.path.join(PROFILE_DIR, f"{prof['device']['id']}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(prof, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        return jsonify(error=f"écriture impossible dans {PROFILE_DIR} : {exc}"), 500
    changed = bridge.reload()
    # Ne PAS prétendre que l'appareil est dans HA : sans MQTT, la discovery n'a
    # pas été publiée. Le message doit dire ce qui s'est réellement passé.
    return jsonify(ok=True, saved=path, device=prof["device"], changed=changed,
                   published=bridge.connected,
                   warning=None if bridge.connected else
                   "profil enregistré, mais le pont n'est PAS connecté au broker "
                   "MQTT : rien n'a été publié. L'appareil apparaîtra dans Home "
                   "Assistant dès que la connexion sera rétablie.")


@app.delete("/api/profiles/<did>")
def api_delete(did):
    path = os.path.join(PROFILE_DIR, f"{did}.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        return jsonify(error="profil introuvable"), 404
    except OSError as exc:
        return jsonify(error=str(exc)), 500
    bridge.reload()
    return jsonify(ok=True)


@app.post("/api/reload")
def api_reload():
    return jsonify(ok=True, changed=bridge.reload())


@app.get("/")
def index():
    return send_from_directory("www", "index.html")


@app.get("/<path:path>")
def static_files(path):
    return send_from_directory("www", path)


# ------------------------------------------------------------ main

def main():
    global bridge

    global _listener
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="rf_bridge", clean_session=True)
    user = os.environ.get("MQTT_USER")
    if user:
        client.username_pw_set(user, os.environ.get("MQTT_PASSWORD"))
    client.will_set(f"{BASE}/status", "offline", retain=True)

    bridge = Bridge(client)

    def on_connect(c, u, flags, rc, props=None):
        log.info("connecté au broker (rc=%s)", rc)
        bridge.connected = True
        c.publish(f"{BASE}/status", "online", retain=True)
        for d in bridge.devices.values():
            d.subscribe()          # avant la discovery : cf. Bridge.reload
            d.publish_discovery()
            d.publish_state()

    def on_disconnect(c, u, flags, rc, props=None):
        bridge.connected = False
        log.warning("déconnecté du broker (rc=%s)", rc)

    def on_message(c, u, msg):
        payload = msg.payload.decode(errors="replace")
        for d in list(bridge.devices.values()):
            if msg.topic.startswith(f"{BASE}/{d.id}/"):
                d.on_message(msg.topic, payload)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    bridge.reload()
    if not bridge.devices:
        # ne PAS sortir : l'UI doit rester joignable pour importer un profil.
        # Sortir ferait boucler l'addon en redémarrage, sans rien expliquer.
        log.warning("aucun profil dans %s — importe-en un depuis l'UI", PROFILE_DIR)

    host = os.environ.get("MQTT_HOST", "core-mosquitto")
    port = int(os.environ.get("MQTT_PORT", "1883"))

    def mqtt_forever():
        """
        Le broker en tâche de fond, et il réessaie sans fin.

        Surtout PAS dans le thread principal : une boucle de connexion bloquante
        empêchait l'UI de démarrer tant que le broker ne répondait pas. Or c'est
        exactement quand rien ne marche qu'on a besoin de l'interface pour
        comprendre pourquoi — elle ne doit dépendre de rien.
        """
        while True:
            try:
                log.info("connexion au broker %s:%s …", host, port)
                client.connect(host, port, keepalive=60)
                client.loop_forever(retry_first_connection=True)
            except Exception as exc:          # noqa: BLE001
                bridge.connected = False
                log.warning("broker injoignable (%s) — nouvelle tentative dans 5 s", exc)
                time.sleep(5)

    _listener = Listener(bridge)
    threading.Thread(target=mqtt_forever, daemon=True).start()
    threading.Thread(target=bridge.watch, daemon=True).start()
    # Ce fil ne touche la radio que si un appareil est effectivement suivi :
    # tant qu'aucun switch n'est activé, le RM4 reste libre pour le labo et pour
    # l'intégration Broadlink de HA.
    threading.Thread(target=_listener.run, daemon=True).start()

    # L'UI en premier, toujours : c'est l'outil de diagnostic.
    try:
        from waitress import serve
        log.info("UI sur :%d", PORT)
        serve(app, host="0.0.0.0", port=PORT, threads=4, channel_timeout=30)
    except ImportError:
        log.warning("waitress absent — serveur de développement Flask")
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
