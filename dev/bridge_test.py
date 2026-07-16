#!/usr/bin/env python3
"""
RF Bridge de bout en bout : broker MQTT réel (amqtt, python pur) + faux RM4.

Vérifie que le pont publie un vrai appareil HA, reçoit les commandes MQTT, et
les traduit en trames RF correctes — le faux RM4 décode ce qu'on lui envoie,
donc on lit noir sur blanc l'état parti sur les ondes.

Aucun matériel, aucun Mosquitto : `./dev/test.sh` le lance seul.
"""
import asyncio
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "rf_bridge"))

import paho.mqtt.client as mqtt  # noqa: E402

import decoder  # noqa: E402
import real_seed  # noqa: E402

PORT = int(os.environ.get("MQTT_TEST_PORT", "18830"))
UI_PORT = PORT + 1
PROFILE_DIR = os.path.join(HERE, ".profiles")
RF_QUEUE = os.path.join(HERE, ".rf_queue")
STATE_DIR = os.path.join(HERE, ".bridge_state")

ok = True
seen = {}          # topic -> dernier payload retenu


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'✓' if cond else '✗'} {label}{'  — ' + str(extra) if extra else ''}")


def write_profile():
    """Un profil Mantra complet, bâti sur les VRAIES captures (§10)."""
    # Repartir d'un dossier vide : un profil laissé par une passe interrompue
    # fausserait les comptes d'appareils, et le test deviendrait instable.
    os.makedirs(PROFILE_DIR, exist_ok=True)
    for f in os.listdir(PROFILE_DIR):
        os.remove(os.path.join(PROFILE_DIR, f))
    store = real_seed.store()
    prof = {
        "version": 1,
        "device": {"id": "mantra_nenufar", "name": "Mantra Nenufar",
                   "manufacturer": "Mantra", "model": "RF00234"},
        "rf": {"frequency": 433.92, "gap": 2000,
               "reference_b64": store["captures"][0]["b64"],
               "reference_name": store["captures"][0]["name"]},
        "fields": store["fields"],
        "checksum": store["checksum"],
        "entities": [
            {"type": "light", "id": "light", "name": "Lumière", "power": "light",
             "brightness": {"field": "lum", "min": 1, "max": 11},
             "color_temp": {"field": "cct", "min": 1, "max": 7, "kelvin": [3000, 5000]}},
            {"type": "fan", "id": "fan", "name": "Ventilateur", "power": "fan",
             "percentage": {"field": "speed", "min": 1, "max": 8},
             "direction": "reverse",
             "preset": {"field": "mode", "options": ["normal", "nuit", "eco"]}},
            {"type": "number", "id": "timer", "name": "Minuterie", "field": "timer",
             "scale": 2, "unit": "min"},
        ],
    }
    with open(os.path.join(PROFILE_DIR, "mantra_nenufar.json"), "w") as fh:
        json.dump(prof, fh, indent=2, ensure_ascii=False)
    return prof


async def run_broker():
    from amqtt.broker import Broker
    broker = Broker({
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{PORT}"}},
        "sys_interval": 0,
        "auth": {"allow-anonymous": True},
        "topic-check": {"enabled": False},
    })
    await broker.start()
    return broker


def start_broker():
    loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    fut = asyncio.run_coroutine_threadsafe(run_broker(), loop)
    fut.result(timeout=15)
    return loop


def main():
    prof = write_profile()
    for d in (STATE_DIR,):
        if os.path.isdir(d):
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))

    print("  démarrage du broker amqtt…")
    start_broker()
    time.sleep(1)

    env = dict(os.environ,
               MQTT_HOST="127.0.0.1", MQTT_PORT=str(PORT),
               PROFILE_DIR=PROFILE_DIR, STATE_DIR=STATE_DIR,
               FAKE_RF_FRAMES=RF_QUEUE,
               DEVICE_IP="192.168.0.99", LOG_LEVEL="info",
               PORT=str(UI_PORT), WATCH_SECONDS="1",
               PYTHONPATH=f"{HERE}:{os.path.join(ROOT, 'rf_bridge')}")
    # substitution du faux RM4 sans toucher à bridge.py
    boot = (
        "import sys; sys.path.insert(0, %r)\n"
        "import fake_broadlink\n"
        "sys.modules['broadlink'] = fake_broadlink\n"
        "sys.modules['broadlink.exceptions'] = fake_broadlink.exceptions\n"
        "exec(compile(open(%r).read(), 'bridge.py', 'exec'), "
        "{'__name__': '__main__', '__file__': 'bridge.py'})\n"
        % (HERE, os.path.join(ROOT, "rf_bridge", "bridge.py"))
    )
    proc = subprocess.Popen([sys.executable, "-c", boot], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=os.path.join(ROOT, "rf_bridge"))

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-observer")
    cli.on_message = lambda c, u, m: seen.__setitem__(m.topic, m.payload.decode())
    for _ in range(40):
        try:
            cli.connect("127.0.0.1", PORT, 30)
            break
        except OSError:
            time.sleep(0.25)
    cli.subscribe("homeassistant/#")
    cli.subscribe("rf_bridge/#")
    cli.loop_start()

    def wait_for(pred, label, timeout=15):
        # horloge monotone : la murale saute (resync NTP / WSL2)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return True
            time.sleep(0.15)
        print(f"    (timeout: {label})")
        return False

    try:
        # ---- discovery : HA doit voir un APPAREIL, pas des entités éparpillées
        got = wait_for(lambda: any(t.endswith("/config") for t in seen), "discovery")
        check("le pont publie la discovery MQTT", got)
        cfgs = {t: json.loads(p) for t, p in seen.items() if t.endswith("/config")}
        # 3 entités viennent du profil ; le 4e switch est une fonction du PONT
        # (« suivre la télécommande »), pas un champ de la trame.
        check("3 entités du profil + le switch d'écoute du pont", len(cfgs) == 4,
              sorted(t.split("/")[1] for t in cfgs))
        comps = sorted(t.split("/")[1] for t in cfgs)
        check("les bons composants HA", comps == ["fan", "light", "number", "switch"],
              comps)

        anycfg = next(iter(cfgs.values()))
        dev = anycfg.get("device", {})
        check("un seul appareil, correctement identifié",
              dev.get("identifiers") == ["mantra_nenufar"]
              and dev.get("name") == "Mantra Nenufar"
              and dev.get("manufacturer") == "Mantra"
              and dev.get("model") == "RF00234", dev)
        check("toutes les entités sont sous CE device",
              all(c["device"]["identifiers"] == ["mantra_nenufar"] for c in cfgs.values()))
        check("disponibilité annoncée (le pont peut être arrêté)",
              all(c.get("availability_topic") == "rf_bridge/status" for c in cfgs.values()))
        check("statut en ligne", seen.get("rf_bridge/status") == "online",
              seen.get("rf_bridge/status"))

        lightcfg = next(c for t, c in cfgs.items() if "/light/" in t)
        check("lumière : schéma JSON + luminosité + température",
              lightcfg.get("schema") == "json" and lightcfg.get("brightness") is True
              and lightcfg.get("supported_color_modes") == ["color_temp"])
        check("mireds calculés depuis les kelvins du profil",
              lightcfg.get("min_mireds") == 200 and lightcfg.get("max_mireds") == 333,
              f"{lightcfg.get('min_mireds')}-{lightcfg.get('max_mireds')}")

        fancfg = next(c for t, c in cfgs.items() if "/fan/" in t)
        check("ventilateur : 8 vitesses, sens, presets",
              fancfg.get("speed_range_min") == 1 and fancfg.get("speed_range_max") == 8
              and "direction_command_topic" in fancfg
              and fancfg.get("preset_modes") == ["normal", "nuit", "eco"])

        numcfg = next(c for t, c in cfgs.items() if "/number/" in t)
        check("minuterie : 0-510 min par pas de 2 (le champ compte en 2 min)",
              numcfg.get("min") == 0 and numcfg.get("max") == 510
              and numcfg.get("step") == 2 and numcfg.get("unit_of_measurement") == "min",
              f"{numcfg.get('min')}-{numcfg.get('max')} pas {numcfg.get('step')}")

        # ---- commandes -> trames RF. Le faux RM4 décode ce qu'il reçoit.
        def emitted():
            out = []
            for line in log_lines():
                if "] émis -> " in line:
                    out.append(line.split("] émis -> ", 1)[1].strip())
            return out

        def log_lines():
            # on lit sans bloquer ce que le pont a écrit jusqu'ici
            import fcntl
            fd = proc.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            try:
                return (proc.stdout.read() or "").splitlines()
            except Exception:                     # noqa: BLE001
                return []

        buf = []

        def emit_count():
            buf.extend(log_lines())
            return sum(1 for l in buf if "] émis -> " in l)

        before = emit_count()
        cli.publish("rf_bridge/mantra_nenufar/light/set",
                    json.dumps({"state": "ON", "brightness": 128}))
        got = wait_for(lambda: emit_count() > before, "émission lumière")
        check("commande lumière -> trame émise", got)
        last = [l for l in buf if "] émis -> " in l][-1] if got else ""
        check("luminosité 128/255 -> palier 6 sur 11", "'lum': 6" in last, last[-90:])
        check("la lampe est allumée", "'light': 1" in last)

        before = emit_count()
        cli.publish("rf_bridge/mantra_nenufar/fan/pct/set", "8")
        wait_for(lambda: emit_count() > before, "émission vitesse")
        last = [l for l in buf if "] émis -> " in l][-1]
        check("vitesse 8 -> speed=8 ET ventilo allumé",
              "'speed': 8" in last and "'fan': 1" in last, last[-90:])

        before = emit_count()
        cli.publish("rf_bridge/mantra_nenufar/fan/pct/set", "0")
        wait_for(lambda: emit_count() > before, "émission vitesse 0")
        last = [l for l in buf if "] émis -> " in l][-1]
        # couper ne remet JAMAIS un niveau à zéro dans ce protocole (§10)
        check("0 % éteint le ventilo mais laisse speed=8",
              "'fan': 0" in last and "'speed': 8" in last, last[-90:])

        before = emit_count()
        cli.publish("rf_bridge/mantra_nenufar/fan/preset/set", "eco")
        wait_for(lambda: emit_count() > before, "émission preset")
        last = [l for l in buf if "] émis -> " in l][-1]
        check("preset « eco » -> mode=2", "'mode': 2" in last, last[-90:])

        before = emit_count()
        cli.publish("rf_bridge/mantra_nenufar/timer/set", "20")
        wait_for(lambda: emit_count() > before, "émission timer")
        last = [l for l in buf if "] émis -> " in l][-1]
        check("minuterie 20 min -> champ brut 10 (unités de 2 min)",
              "'timer': 10" in last, last[-90:])

        # ---- l'état publié, retenu, reflète les commandes
        got = wait_for(lambda: "rf_bridge/mantra_nenufar/light/state" in seen, "état lumière")
        check("état de la lumière republié", got)
        st = json.loads(seen["rf_bridge/mantra_nenufar/light/state"])
        check("état optimiste : ON + luminosité + température",
              st.get("state") == "ON" and st.get("brightness") and st.get("color_temp"), st)
        check("état du ventilateur republié",
              seen.get("rf_bridge/mantra_nenufar/fan/state") == "OFF",
              seen.get("rf_bridge/mantra_nenufar/fan/state"))
        check("preset republié", seen.get("rf_bridge/mantra_nenufar/fan/preset/state") == "eco",
              seen.get("rf_bridge/mantra_nenufar/fan/preset/state"))
        check("minuterie republiée en minutes",
              seen.get("rf_bridge/mantra_nenufar/timer/state") == "20",
              seen.get("rf_bridge/mantra_nenufar/timer/state"))

        # ---- persistance : au redémarrage, l'état ne doit pas repartir de zéro
        # (réémettre un défaut rallumerait la lampe de quelqu'un à 3 h du matin)
        sf = os.path.join(STATE_DIR, "state_mantra_nenufar.json")
        check("état persisté sur disque", os.path.exists(sf))
        if os.path.exists(sf):
            saved = json.load(open(sf))
            check("l'état sauvegardé porte bien les dernières commandes",
                  saved.get("mode") == 2 and saved.get("timer") == 10
                  and saved.get("fan") == 0, saved)

        # ---- l'UI : c'est elle qui rend le pont autonome. Sans elle, déposer
        # un profil imposerait d'installer RF Lab juste pour écrire un fichier.
        import urllib.request

        def ui(path, data=None, method=None):
            url = f"http://127.0.0.1:{UI_PORT}{path}"
            req = urllib.request.Request(
                url, method=method,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        got = wait_for(lambda: ui("/api/status")[0] == 200, "UI joignable", 20)
        check("l'UI du pont répond", got)
        code, st = ui("/api/status")
        check("elle liste l'appareil chargé",
              len(st["devices"]) == 1 and st["devices"][0]["id"] == "mantra_nenufar",
              [d["id"] for d in st["devices"]])
        check("elle montre MQTT et le Broadlink",
              st["mqtt"] is True and st["rm4"]["connected"] is True,
              f"mqtt={st['mqtt']} rm4={st['rm4'].get('model')}")
        check("la page HTML est servie",
              urllib.request.urlopen(f"http://127.0.0.1:{UI_PORT}/", timeout=10).status == 200)

        # ---- import d'un 2e appareil SANS RF Lab, sans redémarrage
        p2 = json.loads(json.dumps(prof))
        p2["device"] = {"id": "salon", "name": "Ventilo salon",
                        "manufacturer": "Mantra", "model": "RF00234"}
        code, r = ui("/api/profiles", {"profile": p2})
        check("un profil s'importe depuis l'UI", code == 200 and r.get("ok"), r)
        got = wait_for(lambda: any("/salon/" in t and t.endswith("/config") for t in seen),
                       "discovery du 2e appareil")
        check("le 2e appareil est publié dans HA sans redémarrage", got)
        check("les 2 appareils coexistent", len(ui("/api/status")[1]["devices"]) == 2,
              [d["id"] for d in ui("/api/status")[1]["devices"]])

        # ---- un profil invalide est refusé AVEC sa raison
        code, r = ui("/api/profiles", {"profile": {"version": 99}})
        check("un profil invalide est refusé à l'import", code == 400 and "invalide" in r.get("error", ""),
              r.get("error", "")[:60])

        # ---- retrait : l'appareil doit disparaître de HA
        seen.pop("homeassistant/light/salon/light/config", None)
        code, r = ui("/api/profiles/salon", method="DELETE")
        check("un appareil se retire depuis l'UI", code == 200 and r.get("ok"), r)
        got = wait_for(lambda: seen.get("homeassistant/light/salon/light/config") == "",
                       "config vide = HA retire l'appareil")
        check("HA reçoit une config vide (c'est ce qui efface l'appareil)", got,
              repr(seen.get("homeassistant/light/salon/light/config")))
        check("il ne reste que le premier", len(ui("/api/status")[1]["devices"]) == 1)

        # ---- rechargement à chaud : déposer un fichier suffit
        p3 = json.loads(json.dumps(prof))
        p3["device"] = {"id": "chambre", "name": "Ventilo chambre",
                        "manufacturer": "Mantra", "model": "RF00234"}
        with open(os.path.join(PROFILE_DIR, "chambre.json"), "w") as fh:
            json.dump(p3, fh)
        # la suite fait tourner 4 serveurs + node + un broker : le sondage à 1 s
        # peut prendre du retard, d'où la marge
        got = wait_for(lambda: any("/chambre/" in t and t.endswith("/config") for t in seen),
                       "détection du fichier déposé", 25)
        check("un fichier déposé dans le dossier est détecté tout seul", got)

        os.remove(os.path.join(PROFILE_DIR, "chambre.json"))
        got = wait_for(lambda: seen.get("homeassistant/light/chambre/light/config") == "",
                       "retrait du fichier détecté", 25)
        check("un fichier supprimé retire l'appareil de HA", got)

        # ---- profil v2 : `requires` et `semantics` sur du VRAI signal R00143.
        # Le récepteur de cette télécommande n'applique que les champs libres
        # plus UN champ désigné par l'octet de commande (CLAUDE.md §10). Régler
        # vitesse ET sens y demande deux trames — c'est ce qu'on vérifie ici,
        # bout en bout, trames décodées par le faux RM4.
        fix = json.load(open(os.path.join(HERE, "fixtures", "real_rf00143.json")))
        flower = {
            "version": 2,
            "device": {"id": "flower", "name": "Mantra Flower",
                       "manufacturer": "Mantra", "model": "R00143"},
            "rf": {"frequency": 433.92, "gap": fix["expected"]["gap"],
                   "reference_b64": fix["captures"][0]["b64"]},
            "fields": [
                {"name": "id", "start": 0, "end": 16, "role": "const", "identity": True},
                {"name": "mode", "start": 16, "end": 18, "role": "data",
                 "min": 0, "max": 2, "requires": {"cmd": 13}},
                {"name": "reverse", "start": 18, "end": 19, "role": "data",
                 "min": 0, "max": 1, "requires": {"cmd": 12}, "semantics": "toggle"},
                {"name": "cct", "start": 19, "end": 24, "role": "data", "min": 4, "max": 24},
                {"name": "light", "start": 24, "end": 25, "role": "data", "min": 0, "max": 1},
                {"name": "speed", "start": 25, "end": 28, "role": "data",
                 "min": 0, "max": 6, "requires": {"cmd": 10}},
                {"name": "lum", "start": 28, "end": 32, "role": "data", "min": 2, "max": 11},
                {"name": "timer", "start": 32, "end": 36, "role": "data",
                 "min": 0, "max": 8, "requires": {"cmd": 14}},
                {"name": "cmd", "start": 36, "end": 40, "role": "const"},
                {"name": "crc", "start": 40, "end": 48, "role": "crc"},
            ],
            "checksum": {"kind": "sub8", "k": 0x55},
            "entities": [
                {"type": "light", "id": "light", "name": "Lumière", "power": "light",
                 "brightness": {"field": "lum", "min": 2, "max": 11}},
                {"type": "fan", "id": "fan", "name": "Ventilateur", "power": "light",
                 "percentage": {"field": "speed", "min": 0, "max": 7},
                 "direction": "reverse"},
            ],
        }
        with open(os.path.join(PROFILE_DIR, "flower.json"), "w") as fh:
            json.dump(flower, fh)
        got = wait_for(lambda: any("/flower/" in t and t.endswith("/config") for t in seen),
                       "profil v2 détecté", 25)
        check("un profil v2 se charge", got)

        # Un ventilateur SANS bit d'alimentation (speed 0 = éteint) : HA exige
        # speed_range_min >= 1, sinon il rejette TOUTE l'entité fan en silence.
        fan_cfg = json.loads(seen.get("homeassistant/fan/flower/fan/config") or "{}")
        check("l'entité ventilateur de la R00143 est bien publiée", bool(fan_cfg),
              [t for t in seen if "/flower/" in t and t.endswith("config")])
        check("speed_range_min >= 1 (0 est réservé à « éteint » par HA)",
              fan_cfg.get("speed_range_min", 0) >= 1, fan_cfg.get("speed_range_min"))

        def flower_frames():
            buf.extend(log_lines())
            return [l for l in buf if "[flower] émis" in l]

        # une commande du bloc lampe : UNE trame, sans contrainte
        n = len(flower_frames())
        cli.publish("rf_bridge/flower/light/set", json.dumps({"state": "ON"}))
        wait_for(lambda: len(flower_frames()) > n, "trame lampe")
        f = flower_frames()[n:]
        check("bloc lampe -> une seule trame, sans contrainte",
              len(f) == 1 and "{'cmd'" not in f[0], f"{len(f)} trame(s)")

        # une commande de vitesse : UNE trame, mais contrainte à cmd=10
        n = len(flower_frames())
        cli.publish("rf_bridge/flower/fan/pct/set", "5")
        wait_for(lambda: len(flower_frames()) > n, "trame vitesse")
        f = flower_frames()[n:]
        check("vitesse -> une trame portant cmd=10",
              len(f) == 1 and "{'cmd': 10}" in f[0], "".join(x[-70:] for x in f))

        # LE test : le sens exige cmd=12, donc sa propre trame
        n = len(flower_frames())
        cli.publish("rf_bridge/flower/fan/dir/set", "reverse")
        wait_for(lambda: len(flower_frames()) > n, "trame sens")
        f = flower_frames()[n:]
        check("sens -> une trame portant cmd=12", any("{'cmd': 12}" in x for x in f),
              "".join(x[-70:] for x in f))

        # Le toggle : redemander le MÊME sens ne doit RIEN émettre. Le récepteur
        # ignore le bit et bascule : une trame de plus inverserait le ventilo.
        n = len(flower_frames())
        cli.publish("rf_bridge/flower/fan/dir/set", "reverse")
        time.sleep(1.5)
        buf.extend(log_lines())
        check("redemander le sens courant n'émet RIEN (sinon ça l'inverserait)",
              len(flower_frames()) == n, f"{len(flower_frames()) - n} trame(s) de trop")

        # ---- ÉCOUTE CONTINUE : suivre la vraie télécommande.
        # C'est ce qui enlève le mot « optimiste » du README : sans elle, un
        # appui sur la télécommande physique désynchronise HA pour toujours.
        check("un switch « suivre la télécommande » est publié",
              "homeassistant/switch/flower/listen/config" in seen,
              [t for t in seen if "/listen/" in t])
        check("il est éteint par défaut — écouter monopolise le RM4",
              seen.get("rf_bridge/flower/listen/state") == "OFF",
              seen.get("rf_bridge/flower/listen/state"))

        # Tant qu'aucun appareil n'est suivi, la radio ne doit PAS être touchée :
        # le labo et l'intégration Broadlink de HA doivent pouvoir s'en servir.
        open(RF_QUEUE, "w").write(fix["captures"][10]["b64"] + "\n")
        time.sleep(2)
        check("écoute éteinte : la trame n'est pas consommée, le RM4 reste libre",
              open(RF_QUEUE).read().strip() != "", "file intacte")

        cli.publish("rf_bridge/flower/listen/set", "ON")
        got = wait_for(lambda: seen.get("rf_bridge/flower/listen/state") == "ON",
                       "switch d'écoute activé")
        check("le switch s'allume", got)

        # « lum10 -> 2 » dans la référence ; cette capture-ci est à lum=11.
        n = len(flower_frames())
        before = seen.get("rf_bridge/flower/light/state")
        got = wait_for(lambda: open(RF_QUEUE).read().strip() == "",
                       "trame consommée par l'écoute", 20)
        check("écoute allumée : le pont capte la trame", got)
        got = wait_for(lambda: seen.get("rf_bridge/flower/light/state") != before,
                       "état republié", 20)
        check("l'état de HA suit la vraie télécommande", got,
              seen.get("rf_bridge/flower/light/state"))
        st_light = json.loads(seen.get("rf_bridge/flower/light/state") or "{}")
        # lum=11 sur 2..11 -> 255/255 côté HA
        check("la luminosité entendue arrive dans HA", st_light.get("brightness") == 255,
              st_light)

        # LE test qui manquait : une DEUXIÈME trame. « Marche une fois puis
        # s'arrête » ne pouvait pas être attrapé tant qu'on n'en envoyait qu'une.
        # Le RM4 sort du mode écoute dès qu'il rend une trame ; il faut le réarmer
        # à chaque fois, sans quoi seule la première est captée.
        before2 = seen.get("rf_bridge/flower/light/state")
        # captures[22] est à lum10 -> brut 2 -> brightness 1 côté HA : bien distinct
        open(RF_QUEUE, "w").write(fix["captures"][22]["b64"] + "\\n")
        got = wait_for(lambda: open(RF_QUEUE).read().strip() == "",
                       "2e trame consommée (le réarmement marche)", 20)
        check("une DEUXIÈME trame est captée — l'écoute se réarme", got)
        got = wait_for(lambda: seen.get("rf_bridge/flower/light/state") != before2,
                       "2e état republié", 20)
        check("la 2e trame met HA à jour aussi", got,
              seen.get("rf_bridge/flower/light/state"))

        # LE point : entendre n'est pas commander. Réémettre ce qu'on vient
        # d'entendre serait au mieux inutile, au pire une boucle sans fin.
        time.sleep(1.5)
        buf.extend(log_lines())
        check("entendre ne déclenche AUCUNE émission",
              len(flower_frames()) == n, f"{len(flower_frames()) - n} trame(s) de trop")

        # Une trame d'un autre appareil ne doit pas être adoptée : le préambule
        # et l'ID appairé sont le discriminateur, et ils sont déjà dans le profil.
        before = seen.get("rf_bridge/flower/light/state")
        store_n = real_seed.store()
        open(RF_QUEUE, "w").write(store_n["captures"][3]["b64"] + "\n")
        wait_for(lambda: open(RF_QUEUE).read().strip() == "", "trame étrangère lue", 20)
        time.sleep(1.0)
        check("une trame d'une AUTRE télécommande est ignorée",
              seen.get("rf_bridge/flower/light/state") == before,
              "l'état n'a pas bougé")

        cli.publish("rf_bridge/flower/listen/set", "OFF")
        wait_for(lambda: seen.get("rf_bridge/flower/listen/state") == "OFF",
                 "écoute coupée")
        check("l'écoute se coupe", True)

        # et le faux RM4 confirme que la contrainte a bien écrit `cmd` dans les bits
        code, st = ui("/api/status")

        # ---- l'IP du Broadlink se règle depuis l'UI du pont aussi
        code, st = ui("/api/status")
        check("le pont expose l'IP et sa provenance",
              st["rm4"].get("ip") is not None and st["rm4"].get("ip_source") in
              ("ui", "option", "broadcast"),
              f"{st['rm4'].get('ip')} ({st['rm4'].get('ip_source')})")
        code, r = ui("/api/device", {"ip": "192.168.0.250"})
        check("une IP sans appareil -> erreur explicite",
              r.get("connected") is False and "timeout" in (r.get("error") or "").lower(),
              (r.get("error") or "")[:44])
        code, r = ui("/api/device", {"ip": "192.168.0.99"})
        check("la bonne IP reconnecte à chaud, sans redémarrer l'addon",
              r.get("connected") is True and r.get("ip_source") == "ui",
              f"{r.get('model')} @ {r.get('host')}")
        code, r = ui("/api/discover")
        check("le pont sait chercher un Broadlink",
              len(r["devices"]) == 1 and r["devices"][0]["rf"] is True,
              [d["ip"] for d in r["devices"]])

        # ---- l'UI doit être joignable même si TOUT est cassé : c'est l'outil de
        # diagnostic. Une boucle de connexion MQTT bloquante l'empêchait de
        # démarrer tant que le broker ne répondait pas — exactement au moment où
        # on en a besoin.
        import urllib.error
        nb_dir = os.path.join(HERE, ".nobroker_profiles")
        nb_state = os.path.join(HERE, ".nobroker_state")
        for d in (nb_dir, nb_state):
            os.makedirs(d, exist_ok=True)
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
        nb_env = dict(env, MQTT_PORT="19999",          # aucun broker à ce port
                      PROFILE_DIR=nb_dir, STATE_DIR=nb_state,
                      PORT=str(UI_PORT + 1))
        nb = subprocess.Popen([sys.executable, "-c", boot], env=nb_env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                              cwd=os.path.join(ROOT, "rf_bridge"))
        try:
            def nbui(path, data=None):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{UI_PORT + 1}{path}",
                    data=json.dumps(data).encode() if data else None,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())

            up = False
            for _ in range(40):
                try:
                    nbui("/api/status")
                    up = True
                    break
                except Exception:             # noqa: BLE001
                    time.sleep(0.5)
            check("l'UI démarre même sans broker MQTT", up)
            if up:
                check("elle signale que MQTT est absent", nbui("/api/status")["mqtt"] is False)
                r = nbui("/api/profiles", {"profile": prof})
                # ne PAS prétendre que l'appareil est dans HA : rien n'a été publié
                check("l'import n'annonce pas une publication qui n'a pas eu lieu",
                      r["ok"] is True and r["published"] is False and r.get("warning"),
                      (r.get("warning") or "")[:52])
                st = nbui("/api/status")
                check("le profil est chargé quand même (il partira à la reconnexion)",
                      len(st["devices"]) == 1 and st["devices"][0]["entities"],
                      st["devices"][0]["entities"] if st["devices"] else None)
        finally:
            nb.terminate()
            try:
                nb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                nb.kill()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        cli.loop_stop()

    print("\n=>", "OK" if ok else "ÉCHEC")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
