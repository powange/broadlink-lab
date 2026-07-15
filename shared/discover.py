#!/usr/bin/env python3
"""
Trouver un Broadlink sur le LAN.

Deux méthodes, parce qu'aucune ne suffit seule :

- **broadcast** : la découverte normale de python-broadlink. Marche depuis un
  addon HA (`host_network: true`), mais PAS depuis WSL2 — le paquet part en
  255.255.255.255 et ne traverse pas le NAT.
- **balayage unicast** : on envoie le même paquet hello à chaque IP de la plage.
  Plus lent, mais l'unicast passe le NAT. C'est la seule option sous WSL2.

Le balayage fonctionne parce que le paquet de découverte embarque `0.0.0.0:0`
comme adresse de retour quand on ne passe pas de `local_ip_address` (cf.
`broadlink.scan`) : l'appareil répond à l'adresse source UDP, correctement
dé-NATée au retour.

TIMEOUT : ne pas descendre sous ~6 s. Les Broadlink sont des appareils WiFi qui
dorment — le premier paquet les réveille et se perd. Un timeout court les déclare
absents alors qu'ils sont là (§8).
"""
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

import broadlink
from broadlink.const import DEFAULT_PORT

DEFAULT_TIMEOUT = 6.0

# devtypes capables de RF (RM Pro / RM4 Pro). Les autres ne font que de l'IR.
RF_CAPABLE = {0x2712, 0x272A, 0x2737, 0x273D, 0x2783, 0x277C, 0x2797, 0x27A1,
              0x27A6, 0x27A9, 0x27C2, 0x27C3, 0x27C7, 0x27CC, 0x27CD, 0x27D0,
              0x27D1, 0x27D3, 0x27DC, 0x27DE, 0x520B, 0x6026, 0x6070, 0x610E,
              0x610F, 0x62BC, 0x62BE, 0x6364, 0x648D, 0x649B, 0x653A}


def _describe(devtype, host, mac, name, locked):
    try:
        model = broadlink.gendevice(devtype, (host[0], DEFAULT_PORT), mac).model or "?"
    except Exception:                          # noqa: BLE001
        model = "?"
    return {"ip": host[0], "devtype": devtype, "model": model, "name": name,
            "mac": ":".join(f"{b:02x}" for b in mac),
            "rf": devtype in RF_CAPABLE, "locked": bool(locked)}


def broadcast(timeout=DEFAULT_TIMEOUT):
    """Découverte normale. Ne traverse pas le NAT de WSL2."""
    out = []
    try:
        for dev in broadlink.discover(timeout=timeout):
            out.append({"ip": dev.host[0], "devtype": dev.devtype,
                        "model": getattr(dev, "model", "?") or "?",
                        "name": getattr(dev, "name", "") or "",
                        "mac": ":".join(f"{b:02x}" for b in dev.mac),
                        "rf": dev.devtype in RF_CAPABLE,
                        "locked": bool(getattr(dev, "is_locked", False))})
    except OSError:
        pass
    return out


def probe(ip, timeout=DEFAULT_TIMEOUT, port=DEFAULT_PORT):
    """Unicast un hello à UNE adresse."""
    try:
        for devtype, host, mac, name, locked in broadlink.scan(
                timeout=timeout, discover_ip_address=str(ip), discover_ip_port=port):
            return _describe(devtype, host, mac, name, locked)
    except (socket.timeout, OSError):
        pass
    return None


def sweep(cidr, timeout=DEFAULT_TIMEOUT, workers=64, port=DEFAULT_PORT):
    """Balaie une plage en unicast. Lent mais traverse le NAT."""
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = list(net.hosts()) or [net.network_address]
    found = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(lambda ip: probe(ip, timeout, port), hosts):
            if r:
                found.append(r)
    return found
