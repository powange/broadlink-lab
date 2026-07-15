# dev/ — lancer et tester l'addon en local

Rien ici n'est embarqué dans l'image : le `Dockerfile` ne copie que `app.py`,
`decoder.py`, `www/` et `run.sh`. Ce dossier sert à boucler sans HAOS.

## Tout tester d'un coup

```bash
./dev/test.sh
```

Décodeur, protocole, API, UI de bout en bout, export YAML, validation du YAML.
Aucun matériel requis. La première exécution installe `jsdom` via npm.

## Lancer l'UI et cliquer dedans

```bash
python3 -m venv dev/venv && dev/venv/bin/pip install -r dev/requirements-dev.txt
dev/venv/bin/python dev/serve.py
# → http://127.0.0.1:8099/
```

Le store est pré-semé avec les 7 captures de la séquence §9, donc la grille de
bits est immédiatement peuplée et le pattern `lum10/lum20/lum30 → 0001/0010/0011`
est lisible tout de suite. `--no-seed` pour partir d'un store vide.

## Faut-il l'IP du Broadlink ?

**En mode par défaut, non** — le faux RM4 ne touche ni réseau ni matériel.

Pour viser le vrai, deux modes :

```bash
dev/venv/bin/python dev/serve.py --device 192.168.0.42   # unicast, IP explicite
dev/venv/bin/python dev/serve.py --real                  # découverte broadcast
```

**Sous WSL2, `--real` ne marchera pas** : WSL2 est derrière un NAT et le broadcast
UDP de découverte ne traverse pas jusqu'au LAN. L'unicast, lui, passe — donc
`--device <ip>` est obligatoire. `serve.py` détecte WSL2 et le rappelle.

L'IP du RM4 se trouve dans l'intégration Broadlink de HA, dans le bail DHCP du
routeur, ou avec le scanner intégré :

```bash
dev/venv/bin/python dev/find_rm4.py            # balaie 192.168.0.0/24 en unicast
```

Il unicaste le paquet hello à chaque IP de la plage au lieu de le broadcaster,
ce qui est la seule façon de découvrir un Broadlink depuis WSL2.

**Le RM4 est un appareil WiFi qui dort**, et c'est un piège sérieux : le premier
paquet le réveille et se perd. Un timeout court le déclare absent alors qu'il est
bien là — le routeur va jusqu'à répondre `Destination Host Unreachable`. C'est
pour ça que `find_rm4.py` attend 6 s par adresse. Si un test réseau échoue,
réessayer avant de conclure que l'appareil n'est pas sur le LAN.

Tout le reverse peut se faire ainsi depuis la machine de dev, sans jamais
installer l'addon sur HAOS. Si tu prends des timeouts, désactive l'intégration
Broadlink de HA le temps des captures (cf. §7 de CLAUDE.md).

Si le RM4 ne répond pas, l'app démarre quand même : la barre d'état affiche
l'erreur en rouge, elle ne plante pas.

## Le faux RM4

`fake_broadlink/` implémente juste ce que `app.py` consomme de python-broadlink.
`serve.py` le substitue explicitement dans `sys.modules` — pas de shadowing de
`sys.path`, on voit ce qui remplace quoi.

Deux comportements qui servent vraiment :

- **`check_data()` rejoue la séquence de §9**, une trame par appui, et lève
  `ReadError` sur les premiers appels comme le vrai appareil. La boucle de
  polling de `/api/capture/poll` est donc réellement exercée, pas court-circuitée.
- **`send_data()` décode ce qu'on lui envoie** et logge l'état correspondant :

  ```
  fake-rm4 émission #1 -> lum=7 cct=0 speed=0 (id=178 fixe=77)
  ```

  C'est le retour qu'on n'a pas avec un vrai ventilo. On voit immédiatement si
  la trame générée porte l'état visé — et si l'ID appairé a bougé, ce qui est le
  piège de §7.

## Le protocole synthétique

`protocol.py` — **ce n'est pas le protocole du Mantra**, qui est justement
l'inconnue du projet. C'est un protocole plausible qui permet de valider la
chaîne complète :

```
0        8      12    14       17           24
| id     | lum  | cct | speed  | fixe       |
  constant  4b     2b    3b      constant
```

`id` et `fixe` ne bougent jamais : ils tiennent le rôle du préambule + ID
appairé. Si un test les voit varier, c'est un bug de l'outil.

```bash
python3 dev/protocol.py   # affiche la séquence §9 et ses bits
```

## Limites

Le faux RM4 valide la logique, pas le dialogue radio. Il ne dit rien sur le
comportement réel de `find_rf_packet(frequency=)`, ni sur l'existence d'un
checksum ou d'un compteur anti-rejeu dans le vrai protocole — ça, seul le
matériel le dira.
