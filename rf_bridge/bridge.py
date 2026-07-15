#!/usr/bin/env python3
"""
RF Bridge — les profils de RF Lab deviennent des appareils Home Assistant.

Générique par construction : le pont ne connaît aucune télécommande. Il lit un
profil (/share/rf_lab/*.json), publie les entités en MQTT discovery, et traduit
chaque commande HA en trame RF.

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
import json
import logging
import os
import sys
import threading
import time

import broadlink
import paho.mqtt.client as mqtt

import decoder
import profile as profile_mod

LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rf_bridge")

DEVICE_IP = os.environ.get("DEVICE_IP") or None
PROFILE_DIR = os.environ.get("PROFILE_DIR", "/share/rf_lab")
STATE_DIR = os.environ.get("STATE_DIR", "/data")
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant")
BASE = os.environ.get("TOPIC_BASE", "rf_bridge")

_dev = None
_dev_lock = threading.Lock()


# ------------------------------------------------------------ RM4

def get_device(force=False):
    global _dev
    with _dev_lock:
        if _dev is not None and not force:
            return _dev
        if DEVICE_IP:
            dev = broadlink.hello(DEVICE_IP)
        else:
            found = broadlink.discover(timeout=5)
            if not found:
                raise RuntimeError("aucun Broadlink trouvé — renseigne device_ip")
            dev = found[0]
        dev.auth()
        log.info("RM4 appairé : %s @ %s", dev.model, dev.host[0])
        _dev = dev
        return dev


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
        try:
            with open(self.state_path) as fh:
                saved = json.load(fh)
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
                json.dump(self.state, fh)
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
    def emit(self):
        """
        Fabrique la trame de l'état COURANT et l'émet.

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
            get_device().send_data(data)
            log.info("[%s] émis -> %s", self.id, self.state)
            return True
        except Exception as exc:              # noqa: BLE001
            log.exception("[%s] émission impossible : %s", self.id, exc)
            return False

    def apply(self, changes):
        """Applique des valeurs brutes, émet, republie l'état."""
        with self.lock:
            self.state.update(changes)
            self._save_state()
            self.emit()
        self.publish_state()

    # ---- discovery + état
    def publish_discovery(self):
        for i, e in enumerate(self.p["entities"]):
            cfg, comp, oid = self._discovery(e, i)
            topic = f"{DISCOVERY_PREFIX}/{comp}/{self.id}/{oid}/config"
            self.client.publish(topic, json.dumps(cfg), retain=True)
            log.info("[%s] discovery %s -> %s", self.id, comp, topic)

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
                cfg.update(percentage_command_topic=self.t(oid, "pct", "set"),
                           percentage_state_topic=self.t(oid, "pct", "state"),
                           speed_range_min=lo, speed_range_max=hi)
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
                pf, _, _ = _rng(e["power"], self.fields)
                self.client.publish(self.t(oid, "state"),
                                    "ON" if s.get(pf) else "OFF", retain=True)
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
            pf, _, _ = _rng(e["power"], self.fields)
            if sub is None:
                ch[pf] = 1 if payload == "ON" else 0
            elif sub == "pct":
                f, lo, hi = _rng(e["percentage"], self.fields)
                v = int(payload)
                # 0 % vaut extinction côté HA ; la vitesse, elle, garde sa valeur
                # (couper ne remet jamais un niveau à zéro dans ce protocole)
                if v <= 0:
                    ch[pf] = 0
                else:
                    ch[f] = max(lo, min(v, hi))
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


# ------------------------------------------------------------ chargement

def load_profiles():
    wanted = [p for p in (os.environ.get("PROFILES") or "").split(",") if p]
    out = []
    try:
        files = sorted(f for f in os.listdir(PROFILE_DIR) if f.endswith(".json"))
    except FileNotFoundError:
        log.error("%s n'existe pas — lance RF Lab et exporte un profil", PROFILE_DIR)
        return out
    if not files:
        log.error("aucun profil dans %s — lance RF Lab et exporte un profil", PROFILE_DIR)
    for name in files:
        path = os.path.join(PROFILE_DIR, name)
        try:
            with open(path) as fh:
                p = json.load(fh)
        except Exception as exc:              # noqa: BLE001
            log.error("%s illisible : %s", name, exc)
            continue
        errs = profile_mod.validate(p)
        if errs:
            # un profil bancal ne se verrait qu'au moment où le ventilo n'obéit
            # pas : on refuse de le charger et on dit pourquoi
            log.error("%s invalide, ignoré : %s", name, "; ".join(errs))
            continue
        if wanted and p["device"]["id"] not in wanted:
            log.info("%s ignoré (pas dans l'option `profiles`)", name)
            continue
        out.append(p)
        log.info("profil chargé : %s (%s)", p["device"]["id"], p["device"]["name"])
    return out


# ------------------------------------------------------------ main

def main():
    profs = load_profiles()
    if not profs:
        sys.exit("aucun profil exploitable — rien à publier")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="rf_bridge", clean_session=True)
    user = os.environ.get("MQTT_USER")
    if user:
        client.username_pw_set(user, os.environ.get("MQTT_PASSWORD"))
    client.will_set(f"{BASE}/status", "offline", retain=True)

    devices = {}

    def on_connect(c, u, flags, rc, props=None):
        log.info("connecté au broker (rc=%s)", rc)
        c.publish(f"{BASE}/status", "online", retain=True)
        for d in devices.values():
            d.publish_discovery()
            d.subscribe()
            d.publish_state()

    def on_message(c, u, msg):
        payload = msg.payload.decode(errors="replace")
        for d in devices.values():
            if msg.topic.startswith(f"{BASE}/{d.id}/"):
                d.on_message(msg.topic, payload)

    client.on_connect = on_connect
    client.on_message = on_message

    for p in profs:
        devices[p["device"]["id"]] = Device(p, client)

    host = os.environ.get("MQTT_HOST", "core-mosquitto")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    log.info("connexion à %s:%s …", host, port)
    while True:
        try:
            client.connect(host, port, keepalive=60)
            break
        except OSError as exc:
            log.warning("broker injoignable (%s) — nouvelle tentative dans 5 s", exc)
            time.sleep(5)

    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
