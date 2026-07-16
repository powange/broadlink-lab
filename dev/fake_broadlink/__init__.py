"""
Faux RM4 Pro — implémente juste ce que app.py consomme de python-broadlink :
hello / discover / auth / find_rf_packet / check_data / send_data.

Deux comportements utiles pour le dev :

- check_data() rejoue la séquence de capture de §9, une trame par appui. Les
  premiers appels lèvent ReadError comme le vrai appareil tant que rien n'est
  reçu, ce qui exerce réellement la boucle de polling de /api/capture/poll.
- send_data() décode ce qu'on lui envoie et logge l'état correspondant. C'est
  le retour qu'on n'a pas avec un vrai ventilo : on voit tout de suite si la
  trame générée porte bien l'état visé.
"""
import base64
import itertools
import logging
import os
import time

from . import const
from . import exceptions

import protocol

log = logging.getLogger("fake-rm4")

# tout ce qui a été émis, dans l'ordre — les tests s'appuient dessus
SENT = []

# Nombre de check_data() qui lèvent ReadError avant qu'une trame « arrive ».
# FAKE_LATENCY_POLLS=9999 simule « personne n'appuie sur la télécommande » :
# indispensable pour tester l'annulation et le timeout.
LATENCY_POLLS = int(os.environ.get("FAKE_LATENCY_POLLS", "2"))

# Fichier de trames b64 à rejouer en écoute, une par ligne, consommées dans
# l'ordre. Sans lui, le faux RM4 rejoue le protocole SYNTHÉTIQUE — inutile pour
# tester l'écoute du pont, qui doit reconnaître de VRAIES trames contre un vrai
# profil (le préambule et l'ID appairé sont le discriminateur).
RF_FRAMES = os.environ.get("FAKE_RF_FRAMES")


class FakeRM4:
    devtype = 0x520B          # RM4 Pro, comme le vrai (§8)
    model = "RM4 Pro (simulé)"
    manufacturer = "Broadlink"

    def __init__(self, host=None):
        self.host = (host or KNOWN_IP, 80)
        self.mac = MAC
        self.is_locked = False
        self._armed = False
        self._polls = 0
        self._seq = itertools.cycle(protocol.SEQ)

    # --- protocole device -------------------------------------------------
    def auth(self):
        log.info("faux RM4 appairé @ %s", self.host[0])
        return True

    def find_rf_packet(self, frequency=None):
        log.info("écoute RF @ %s MHz", frequency)
        self._armed = True
        self._polls = 0
        return True

    def cancel_sweep_frequency(self):
        """Commande 0x1e du vrai appareil : sortie du mode apprentissage."""
        log.info("sortie du mode écoute")
        self._armed = False
        return True

    def check_data(self):
        if not self._armed:
            raise exceptions.StorageError("pas en écoute")

        if RF_FRAMES:
            # Une file pilotée par le test : il y dépose la trame que « la vraie
            # télécommande » vient d'émettre. StorageError et pas ReadError —
            # c'est ce que rend le vrai RM4 tant que rien n'est arrivé, et c'est
            # sur lui que la boucle d'écoute du pont distingue « rien » d'une
            # panne.
            try:
                with open(RF_FRAMES) as fh:
                    lines = [x.strip() for x in fh if x.strip()]
            except OSError:
                lines = []
            if not lines:
                raise exceptions.StorageError("rien reçu")
            with open(RF_FRAMES, "w") as fh:
                fh.writelines(x + "\n" for x in lines[1:])
            self._armed = False
            log.info("trame rejouée depuis la file (%d en attente)", len(lines) - 1)
            return base64.b64decode(lines[0])

        self._polls += 1
        if self._polls <= LATENCY_POLLS:
            raise exceptions.ReadError("rien reçu")

        name, lum, cct, speed, _meta = next(self._seq)
        self._armed = False
        log.info("trame simulée : %s (lum=%d cct=%d speed=%d)", name, lum, cct, speed)
        return protocol.packet_for(lum, cct, speed)

    def send_data(self, data):
        SENT.append(data)
        state = protocol.decode_state(data)
        if state is None:
            log.warning("émission #%d : trame INDÉCODABLE (%d octets)",
                        len(SENT), len(data))
        else:
            log.info("émission #%d -> lum=%d cct=%d speed=%d (id=%d fixe=%d)",
                     len(SENT), state["lum"], state["cct"], state["speed"],
                     state["id"], state["fixe"])
        time.sleep(0.02)
        return True


# IP à laquelle le faux RM4 "répond". Toute autre adresse simule un appareil
# absent : c'est ce qui permet de tester la saisie d'IP et ses erreurs.
KNOWN_IP = os.environ.get("FAKE_RM4_IP", "192.168.0.99")


# Une mauvaise IP doit COÛTER du temps, comme dans la vraie vie : sans délai, la
# requête se termine avant qu'on ait pu cliquer « Annuler », et l'annulation
# devient intestable. 0 par défaut (tests rapides) ; FAKE_HELLO_DELAY=2 pour les
# scénarios qui ont besoin d'une tentative qui dure.
HELLO_DELAY = float(os.environ.get("FAKE_HELLO_DELAY", "0"))


def hello(host, port=80, timeout=10):
    if host != KNOWN_IP:
        time.sleep(min(timeout, HELLO_DELAY))
        raise exceptions.NetworkTimeoutError(
            f"[Errno -4000] Network timeout: No response received within {timeout}s")
    return FakeRM4(host)


def discover(timeout=5, **kwargs):
    time.sleep(0.1)
    return [FakeRM4(KNOWN_IP)]


def scan(timeout=5, local_ip_address=None, discover_ip_address="255.255.255.255",
         discover_ip_port=80):
    """Miroir de broadlink.scan : un générateur (devtype, host, mac, name, locked)."""
    if discover_ip_address in (KNOWN_IP, "255.255.255.255"):
        d = FakeRM4(KNOWN_IP)
        yield (d.devtype, d.host, MAC, "RM4 simulé", False)


MAC = bytes.fromhex("aabbccddeeff")


def gendevice(devtype, host, mac, **kwargs):
    return FakeRM4(host[0] if isinstance(host, tuple) else host)
