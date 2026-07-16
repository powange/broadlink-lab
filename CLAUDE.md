# Broadlink RF Lab — deux addons Home Assistant

**`rf_lab`** — rétro-ingénierie de télécommandes via un Broadlink RM4 Pro. UI ingress :
capturer, diffé, nommer les champs, générer des trames inédites, émettre. Produit un
**profil d'appareil**.

**`rf_bridge`** — lit les profils et en fait de vrais appareils Home Assistant, via
MQTT discovery. **Générique** : il ne connaît aucune télécommande, il applique le profil.

Les deux ne se parlent jamais : ils s'échangent un fichier dans `/share/broadlink_lab/`. Une
fois le reverse fini, le labo peut être désinstallé.

**Dépendance assumée : Broadlink.** La capture et l'émission passent par
`python-broadlink`, et `decode_packet`/`encode_packet` parsent son format de paquet.
Le reste — décodage PWM, rebuild par swap de paires, détection de checksum, format de
profil, pont MQTT — est indépendant du matériel. Pas limité au 433 MHz : `decoder.py`
connaît l'en-tête IR (`0x26`), un RM4 Pro fait IR + RF 433 + RF 315.

---

## 1. Le problème à résoudre

**Matériel :**
- Ventilateur de plafond **Mantra Nenufar** : LED 18W dimmable, CCT 3000–5000K, moteur DC
  8 vitesses, mode été/hiver.
- Télécommande RF **RF00234**, **433 MHz** (vérifié — attention, Mantra a aussi une gamme
  2,4 GHz type R00142/R00233 que le RM4 Pro ne peut PAS voir ; ce n'est pas notre cas).
- Broadlink **RM4 Pro** (433/315 MHz), déjà intégré dans HA. Coordonnées en §8.

**Ce que la RF00234 sait régler** — donc ce que la trame absolue porte :
on/off lumière, luminosité, CCT, on/off ventilo, vitesse, **reverse** (toggle),
**mode nuit**, **mode éco**, et un **timer 1 h / 2 h / 4 h / 8 h**.
C'est le `meta_schema` par défaut de `app.py`.

Attention aux **toggles** (reverse, nuit, éco) : le bouton ne dit pas l'état
résultant, il le bascule. Une capture s'étiquette avec l'état **obtenu** — en
regardant le ventilo — pas avec le bouton pressé.

**Tranché (§10) : « éteint » n'est PAS « niveau 0 ».** La lampe et le ventilo ont
chacun un bit d'alimentation dédié (32 et 40), et couper l'un laisse son champ de
niveau intact. Avoir modélisé `light`/`lum` et `fan`/`speed` séparément plutôt que
de présumer était le bon choix.

**Le blocage :** la télécommande n'envoie pas de commandes incrémentales (« luminosité + »,
« CCT + »). Elle maintient son état en interne et **transmet à chaque appui la trame d'état
complet** (luminosité + CCT + vitesse + mode). Apprendre « lum+ » via `remote.learn_command`
capture donc un état figé : le rejouer force le ventilo à cet état précis au lieu d'incrémenter.

**Conséquence, et c'est contre-intuitif :** puisque la trame est **absolue**, on n'a pas besoin
de connaître l'état courant pour émettre — on envoie directement la trame de l'état cible.
Home Assistant n'a besoin de modéliser l'état que pour l'UI (mode optimiste), jamais pour
construire la commande. Le vrai travail est donc : **décoder le protocole pour pouvoir
fabriquer la trame de n'importe quel état**, plutôt que d'apprendre des centaines de codes.

L'apprentissage brut est ingérable en combinatoire : 8 vitesses × N luminosités × 3 CCT ×
été/hiver = des centaines de trames.

---

## 2. But du projet

Un addon HA avec UI web (ingress) qui boucle rapidement sur :

**capturer → décoder → diffé → nommer les champs → générer → émettre → observer le ventilo**

En CLI c'est 5 étapes par hypothèse. L'objectif est d'en faire un slider.

Livrable final : une table de correspondance champs → bits, et un package YAML HA
(template light + template fan, optimistes) exporté depuis l'outil.

**Révision (§10)** : l'espace d'états réel dépasse 3,7 millions de combinaisons,
parce que chaque trame porte les deux organes à la fois. Le package n'embarque donc
pas les codes — il appelle `/api/set` en `rest_command`, et l'addon génère à la
volée. Le YAML fait 11 Ko au lieu de 80 Mo.

**Conséquence : l'addon devient une dépendance permanente de HA.** D'où le passage
à waitress (WSGI de production, Python pur) plutôt que le serveur de dev Flask.

---

## 3. Décisions d'architecture (et pourquoi)

### 3.1. Parler au RM4 en direct via `python-broadlink`, PAS via l'API HA

```python
dev = broadlink.hello(ip)
dev.auth()
dev.find_rf_packet(frequency=433.92)  # fréquence connue -> zéro sweep
data = dev.check_data()               # octets bruts immédiatement
dev.send_data(data)
```

Ça shunte le `learn_command` de HA (asynchrone, passe par une notification, écrit dans
`.storage`) et ça shunte le sweep de fréquence. Une capture = un clic, pas 30 s de danse.

### 3.2. Le rebuild par swap de paires — **le point le plus important du projet**

Pour générer une trame, on ne la reconstruit **jamais** à partir de zéro (fragile : préambule,
gaps, répétitions, calibration du tick). On part d'une **capture réelle de référence** et on
inverse les paires `(mark, space)` des seuls bits à changer.

En PWM, `bit=1` → `(long, court)` et `bit=0` → `(court, long)`. **Swapper mark et space inverse
le bit** en préservant à l'identique le préambule, les gaps, les répétitions et le timing exact
de la télécommande. Zéro dérive.

C'est implémenté dans `decoder.rebuild_frame()`. Ne pas remplacer par une reconstruction
naïve à partir de constantes de timing.

**Validé sur le matériel** (§10) : les trames ainsi fabriquées sont identiques bit
pour bit à celles de la vraie télécommande, et le ventilo leur obéit.

#### 3.3. Addon HA (pas un simple container)

Ingress → UI dans la sidebar, pas de port exposé. `host_network: true` pour la découverte
broadcast Broadlink (supprimable si `device_ip` est renseigné en dur).
Persistance dans `/data` (volume addon).

### 3.4. Deux addons, un fichier entre eux

Le labo est un outil **temporaire** : une fois le protocole décodé, il n'a plus lieu
d'être installé. Le pont, lui, doit tourner en permanence. Les séparer permet de
désinstaller le premier — et rend le second **réutilisable** : une nouvelle télécommande,
c'est une session de labo et un profil de plus, zéro ligne de code.

Ils communiquent par `/share/broadlink_lab/<id>.json` (le labo en `rw`, le pont en `ro`).
Jamais directement : chacun démarre, s'arrête et se met à jour sans l'autre.

`shared/` est la source de vérité de `decoder.py` et `profile.py`. Chaque addon en
embarque une **copie**, parce que le contexte de build Docker d'un addon HA se limite à
son propre dossier — un `COPY ../shared/` est impossible. `./dev/sync_shared.sh` synchronise,
`dev/shared_test.py` échoue si les copies dérivent.

### 3.5. MQTT discovery plutôt qu'un package YAML

Le pont publie ses entités sur le broker : HA crée un **vrai appareil**, avec fabricant,
modèle et entités groupées sous une seule carte. Rien à coller dans `configuration.yaml`.

L'alternative — un package YAML avec les codes en dur — est morte à l'analyse : chaque
trame portant les deux organes, l'espace d'états dépasse 3,7 millions de combinaisons,
soit ~80 Mo de YAML (§10). Le pont génère à la volée.

Prix à payer : une dépendance au broker MQTT (addon Mosquitto). `services: - mqtt:need`
dans `config.yaml`, et le superviseur fournit les identifiants — rien à saisir.

---

## 4. Format Broadlink (référence)

Un code Broadlink n'est pas un identifiant opaque : **c'est la liste des durées d'impulsions**.

| Offset | Contenu |
|---|---|
| 0 | Header : `0x26` = IR, `0xb2` = RF 433 MHz, `0xd7` = RF 315 MHz |
| 1 | Nombre de répétitions |
| 2–3 | Longueur des données suivantes (little-endian) |
| 4… | Durées : 1 octet, ou `0x00` + 2 octets big-endian si ≥ 256 |
| fin | Terminateur `0x0d 0x05` — **pas toujours présent** |

Deux écarts entre cette référence et le vrai matériel, constatés sur les captures
de §10. Les deux sont des pièges silencieux :

**L'en-tête peut valoir `0xb1`.** C'est ce que rend le RM4 Pro sur ses captures RF,
là où la doc communautaire annonce `0xb2`. Ne pas le normaliser : `app.py` réémet
l'en-tête d'origine.

**Le terminateur est absent des captures brutes.** `check_data()` rend des paquets
qui s'arrêtent sur leur dernier gap. `decode_packet` renvoie donc un flag
`terminator` et `encode_packet` le prend en paramètre. En ajouter un absent
collerait une impulsion parasite (~427 puis ~164 µs) en fin de trame émise, et
casserait le round-trip — qui est justement la calibration.

**Unité de temps : `269/8.192` ≈ 32,84 µs.** Cette constante flotte dans la communauté
(certaines sources disent 30,52 µs = 2⁻¹⁵ s). Elle n'a **aucune importance pour le diff de
bits** ; elle ne compte que pour la régénération — et le swap de paires (§3.2) évite justement
d'en dépendre. Un test de round-trip (`decode` puis `encode` doit ressortir le b64 identique)
sert de calibration.

---

## 5. État actuel du repo

**La boucle complète est validée sur le matériel (15/07/2026).** Une trame de
luminosité que la télécommande n'a jamais émise a été fabriquée par l'outil,
envoyée via le RM4, et **le ventilo a obéi**. Capturer → décoder → diffé → nommer
→ générer → émettre : tout le pari du projet tient, §3.2 compris.

**Le protocole est décodé à 53 bits sur 64** (§10) : lumière, CCT, luminosité,
ventilo, reverse, mode moteur, vitesse, et le checksum. Ne manque que le timer,
seul locataire possible de l'octet 6.

Tout est testé : 95 contrôles via `./dev/test.sh` sans matériel, dont une
non-régression sur 14 captures de **vrai signal** (`dev/fixtures/`).

```
repository.yaml              # manifeste du dépôt d'addons
shared/                      # source de vérité, copiée dans les 2 addons
├── decoder.py               # decode/encode packet, PWM, checksums, rebuild_frame
└── profile.py               # format du profil d'appareil + validation
rf_lab/                      # ADDON 1 — le labo (ingress, temporaire)
├── config.yaml              # slug rf_lab, ingress, host_network, map share:rw
├── app.py                   # Flask + python-broadlink, API, /api/profile
├── decoder.py profile.py    # copies de shared/ (cf. §3.4)
├── test_decoder.py          # calibration §9, sans dépendances
└── www/index.html           # UI complète, vanilla, fichier unique
rf_bridge/                   # ADDON 2 — le runtime (MQTT, permanent)
├── config.yaml              # slug rf_bridge, services mqtt:need, map share:ro
├── bridge.py                # profils -> MQTT discovery -> trames RF
└── decoder.py profile.py    # copies de shared/
dev/                         # jamais embarqué — cf. dev/README.md
├── serve.py                 # lance rf_lab en local : faux RM4, --device, --seed-real
├── fake_broadlink/          # faux RM4 : rejoue §9, et DÉCODE ce qu'on lui émet
├── protocol.py              # protocole synthétique de test (pas celui du Mantra)
├── real_seed.py             # store peuplé des vraies captures + leur carte
├── find_rm4.py              # retrouve l'IP du RM4 (unicast, marche sous WSL2)
├── sync_shared.sh           # shared/ -> les 2 addons
├── test.sh                  # suite complète, aucun matériel
├── shared_test.py           # les copies de shared/ n'ont pas dérivé
├── real_test.py             # 387 contrôles sur le vrai signal
├── bridge_test.py           # RF Bridge : broker amqtt réel + faux RM4
├── ui_test.mjs              # pilote la vraie index.html dans jsdom
├── cancel_test.mjs          # annulation de capture + verrou
├── meta_test.mjs            # paramètres d'état configurables
├── profile_test.mjs         # profil d'appareil : build, import, réancrage
└── fixtures/                # 42 captures RÉELLES — le seul vrai signal du projet
```

### API déjà exposée par `app.py`

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/status` | connexion, modèle, fréquence |
| POST | `/api/capture/start` | lance une capture (thread) ; 409 si déjà en cours |
| POST | `/api/capture/cancel` | annule la capture en cours (sort le RM4 du mode écoute) |
| GET | `/api/capture/poll` | `{state: idle\|listening\|done\|timeout\|cancelled\|error, message, result(b64)}` |
| POST | `/api/captures` | `{name, b64, meta:{...}}` → enregistre ; `meta` suit `meta_schema` |
| DELETE | `/api/captures/<id>` | supprime |
| GET | `/api/analyze?gap=2000&mode=pwm` | `{rows[], analysis:{length,varying[],truncated}, fields[], checksum}` |
| POST | `/api/fields` | `{fields[], checksum:{kind,k}}` → persiste le nommage |
| GET | `/api/detect-checksum?start=&end=&gap=` | cherche le checksum qui explique toutes les captures |
| GET | `/api/meta-schema` | paramètres d'état à saisir ; `?defaults=1` → ceux de la RF00234 |
| POST | `/api/meta-schema` | `{meta_schema[]}` → persiste ; valide clé/type/options |
| POST | `/api/generate` | `{ref_id, values:{}, gap}` → `{b64, bits, ref_bits, verified}` |
| POST | `/api/send` | `{b64}` → émet ; logge l'état décodé |
| POST | `/api/set` | `{light,cct,lum,fan,reverse,mode,speed,timer}` → **génère ET émet**. Le point d'entrée de HA. Refuse d'émettre une trame non vérifiée. |
| POST | `/api/ref` | `{ref_id}` → capture de référence par défaut de `/api/set` |

Modèle d'un champ : `{name, start, end, msb_first, role, min?, max?}` où
`role ∈ {data, const, crc}`. Un **`const`** n'est jamais réécrit ni énuméré —
c'est là que vivent le préambule, l'ID appairé et l'octet de commande décoratif.
`min`/`max` bornent les sliders et l'export aux valeurs réelles : sans eux, `lum`
irait de 0 à 15 alors que la télécommande ne fait que 1 à 11.

Modèle d'un paramètre d'état : `{key, label, type, short, always?, min?, max?, options?}`
où `type ∈ {number, bool, enum}`. `short` = préfixe du nommage auto, `always` = présent
dans le nom même à 0 — et, pour un bool, affiché `on`/`off` dans la grille au lieu
d'être masqué quand il est faux. Défaut = les 9 paramètres de la RF00234 (§1).

---

## 6. L'UI : `www/index.html`

Fichier **unique, vanilla JS, sans build step** (préférence assumée).
**Toutes les URL de fetch sont relatives** (`fetch('api/status')`, sans `/` initial) :
l'ingress sert la page sous `/api/hassio_ingress/<token>/` et strippe le préfixe côté HA.
Un `/` initial casserait tout. `dev/ui_test.mjs` résout les URL contre
`window.location`, ce qui vérifie cette contrainte à chaque test.

Deux logiques sont dupliquées côté JS faute d'endpoint : `fieldValue()` recopie
`decoder.field_value` (l'API ne renvoie que les bits bruts), et l'export est
construit dans le navigateur. Si `decoder.field_value` change, changer les deux.

Les cinq sections :

1. **Barre d'état** — connexion RM4, modèle, fréquence, sélecteur de `gap` et du mode de
   décodage (PWM / Manchester).

2. **Panneau de capture** — nom + **paramètres d'état obligatoires**. Sans eux le diff ne
   veut rien dire : c'est ce qui corrèle un champ de bits à une grandeur physique.
   Bouton « Capturer » → poll jusqu'à `done`, annulable (bouton ou Échap).

   Les paramètres sont **générés depuis `meta_schema`** (§8), pas codés en dur, et
   éditables depuis l'UI (« Paramètres d'état… »). Trois types : `number`, `bool`,
   `enum`. Une capture antérieure à l'ajout d'un paramètre affiche un chip jaune
   `clé ?` — le trou doit se voir, sinon il se lit comme un champ inexpliqué.

   Le nommage automatique : les champs `always` sont toujours présents, les autres
   n'apparaissent que s'ils sont actifs. D'où `light1_lum10_cct3000_fan1_v0` au repos,
   et `light1_lum10_cct3000_fan1_v0_rev1_t4` avec reverse et timer 4 h. Un on/off à
   l'arrêt reste lisible (`light0`) : éteint est un état, pas une absence.

3. **Grille de bits — l'écran qui fait tout le boulot.**
   Une ligne par capture, une colonne par bit, en police mono. Les colonnes qui varient
   (`analysis.varying`) sont surlignées. Sélection de colonnes au clic-glisser → on nomme la
   tranche (`lum`, `cct`, `speed`, `crc`). L'UI affiche alors la valeur décodée de ce champ
   pour chaque capture, ce qui rend le pattern lisible d'un coup d'œil :
   `lum10/lum20/lum30 → 0001/0010/0011` = gagné.
   Trier les lignes par métadonnée aide énormément à voir l'incrément.

4. **Générateur** — sliders par champ nommé + choix de la capture de référence →
   `POST /api/generate` → affiche `bits` vs `ref_bits` (diff visuel) et le flag `verified` →
   bouton « Émettre ». Boucle en ~1 s. « Émettre 2× » teste le compteur anti-rejeu (§7).
   Le bouton d'émission reste bloqué tant que `verified` est faux.

5. **Export** — table de correspondance (JSON) + package YAML HA prêt à coller.
   Le YAML indexe les codes par **valeur brute** des champs, pas par grandeur physique :
   la correspondance observée (`lum10 → lum=1`) est en commentaire en tête de fichier,
   le mapping physique reste à reporter dans les templates. Plafonné à 512 combinaisons.

---

## 7. Pièges connus à anticiper

- **Checksum.** Beaucoup de ces protocoles ont un XOR ou une somme sur les derniers octets.
  Symptôme : la trame apprise fonctionne, mais une trame générée avec la luminosité modifiée
  ne fait rien. Le diff le révèle : un champ qui bouge **sans corrélation logique** avec la
  grandeur changée = checksum. `decoder.compute_checksum()` gère `sum8`, `xor8` et `sub8`
  (`(k - somme) & 0xFF`). **C'est `sub8` k=0x55 sur la RF00234** (§10).
  `decoder.detect_checksum()` le retrouve automatiquement à partir des captures —
  bouton « Détecter » dans le dialogue de nommage. Il faut ≥ 3 états distincts
  pour que ce ne soit pas un hasard.
- **Compteur anti-rejeu.** ~~Vérifier qu'un même code envoyé 2× de suite passe.~~
  **Écarté sur la RF00234** : le même état capturé deux fois donne des trames
  identiques (§10). Le replay marche.
- **Encodage non-PWM.** `decode_pwm` est générique. Si la sortie est du bruit, essayer
  Manchester. Noter que `rebuild_frame` **exige les index PWM** et lèvera une exception en
  Manchester — il faudra alors une autre stratégie de génération.
- **Bits fixes = ID télécommande.** Le gros bloc invariant est le préambule + l'ID appairé
  avec le récepteur. Ne jamais le toucher, sinon le ventilo ignore la trame.
- **Cohabitation avec l'intégration Broadlink de HA.** Elle garde une session ouverte. En
  pratique ça passe (UDP), mais en cas de timeouts pendant les captures, désactiver
  l'intégration le temps du reverse.
- **`rtl_433 -A`** peut reconnaître le protocole directement (il a des décodeurs pour plusieurs
  télécommandes de ventilateurs de plafond). À tenter en parallèle : ça éviterait tout ce travail.

---

## 8. Le matériel — ce qu'on a appris de lui

L'IP du RM4 **n'est pas documentée ici** : c'est de la configuration, pas de la
connaissance. Elle vit dans l'option `device_ip` des deux addons, et dans
`--device` de `dev/serve.py`. `dev/find_rm4.py` la retrouve au besoin. Ce qui suit
est vrai de tout RM4 Pro, pas d'un exemplaire.

**Vérifié sur le matériel (RM4 Pro, devtype `0x520b` → classe `rm4pro`) :**
`find_rf_packet(frequency=433.92)` est **accepté sans sweep préalable** — la
décision §3.1 tient. `auth()` passe, l'appareil entre en écoute RF. La
cohabitation avec l'intégration Broadlink de HA n'a posé aucun problème (elle
était active pendant les tests), contrairement à ce que craint §7.

**C'est un appareil WiFi qui dort — le piège le plus coûteux du projet.** Le
premier paquet le réveille et se perd : 10 % de perte et 267 ms de pic sur un
appareil endormi, 11 ms une fois réveillé. Conséquence : **tout timeout court
produit un faux négatif crédible.** Un `ping -W 2` et un scan à 2 s le déclarent
absent alors qu'il est là — le routeur va jusqu'à répondre
`Destination Host Unreachable`, son ARP ayant échoué sur un appareil qui dort.
`dev/find_rm4.py` attend 6 s par adresse pour cette raison : **ne pas baisser
cette valeur.** Devant un échec réseau, réessayer avant de conclure.

**Un récepteur muet n'est pas un problème logiciel.** Sur la Mantra Flower
(R00143), toutes les émissions ont été ignorées : trames justes, checksum bon,
paquets acceptés par le RM4, et rien. Ce n'était ni la carte, ni la fréquence, ni
la portée — **l'antenne du récepteur était coincée sous le bloc d'alimentation**
du ventilateur. La faire passer par-dessus a tout réglé.

Deux leçons, payées cher :

- **Une télécommande à écran affiche son état qu'elle soit écoutée ou non.** On
  peut donc étiqueter 51 captures en regardant l'écran, décoder le protocole
  entier, et n'avoir aucune preuve que l'appareil ait jamais reçu quoi que ce
  soit. **Avant de chercher pourquoi une émission échoue, vérifier que la VRAIE
  télécommande pilote l'appareil.** C'est dix secondes, et ça évite d'accuser la
  bande ISM.
- **« 433 MHz » sur une fiche produit est une bande** (433,05–434,79), pas une
  fréquence. Ça n'infirme ni ne confirme le 433,92 câblé en dur ici.
  `dev/find_frequency.py` demande la vraie valeur au RM4 en balayant — à lancer
  devant toute nouvelle télécommande, mais **après** avoir vérifié le lien.

Au passage : la R00143 est bien en 433 MHz (revendeurs indépendants). Le piège
`R00142`/`R00233` en 2,4 GHz ne s'y applique pas — le suffixe qui compte chez
Mantra est le « RF » des fiches (« Nemo RF »), pas le préfixe de la référence.

**Depuis WSL2, l'unicast passe, le broadcast non.** `--device <ip>` fonctionne
parce que `broadlink.hello()` n'embarque pas l'IP locale dans son paquet (il met
`0.0.0.0:0`, cf. `broadlink.scan`) : l'appareil répond à l'adresse source UDP,
correctement dé-NATée au retour. Seule la **découverte broadcast** ne traverse pas
le NAT — d'où `dev/find_rm4.py`, qui balaie la plage en unicast.

### Préférences du projet

- Serveur HAOS dédié + serveur Docker/Portainer. Pas de Proxmox.
- Dépôt d'addons perso existant (cf. `powange/Wyoming-STT-VBAN` pour le pattern).
- Stack habituelle : Nuxt 3 / Vue 3 / Laravel / TypeScript / Docker.
  Ici : **vanilla, fichier unique, pas de build step**.

### Ce qui est publiable, et ce qui ne l'est pas

Le dépôt est public. Règle posée maintenant, parce qu'elle servira :

- **Les captures de `dev/fixtures/` restent en clair.** Ce sont les codes d'un
  ventilateur de plafond : le risque est borné à la portée radio (~30 m), et
  n'importe qui à cette distance peut les capturer lui-même avec un RM4. En face,
  elles sont le seul vrai signal du projet — sans elles, `real_test.py`,
  `bridge_test.py`, `profile_test.mjs` et `--seed-real` ne démarrent même pas.
  Et git en est **la seule sauvegarde** : les mettre en `.gitignore` perdrait les
  tests *et* le backup.
- **Le jour où ce labo servira sur un portail ou une porte de garage, ce
  raisonnement s'inverse.** Là les captures sont de vrais secrets. Deux options :
  `.gitignore` (et on perd les tests), ou **scrubber l'ID appairé** (bits du
  préambule) en réencodant — les tests sont auto-cohérents, ils survivent. Préférer
  le scrub.
- **Jamais d'IP, de MAC ni de topologie réseau dans le dépôt.** C'est de la
  configuration, ça n'apprend rien au lecteur suivant, et ça ne s'efface pas d'un
  historique git.

## 9. Workflow de test

**Tout tester sans matériel** (décodeur, API, UI, export) :

```bash
./dev/test.sh
```

**Cliquer dans l'UI sans matériel** — store pré-semé avec la séquence ci-dessous,
donc la grille est peuplée immédiatement :

```bash
python3 -m venv dev/venv && dev/venv/bin/pip install -r dev/requirements-dev.txt
dev/venv/bin/python dev/serve.py          # -> http://127.0.0.1:8099/
```

**Contre le vrai RM4, depuis la machine de dev** (pas besoin d'installer l'addon) :

```bash
dev/venv/bin/python dev/serve.py --device 192.168.0.42
```

Sous WSL2, `--device <ip>` est **obligatoire** : le broadcast de découverte
(`--real`) ne traverse pas le NAT. Détails dans `dev/README.md`.

La calibration de §4 (`decode` puis `encode` ressort le b64 identique) est
automatisée dans `rf_lab/test_decoder.py`, sans dépendances :

```bash
python3 rf_lab/test_decoder.py
```

Séquence de capture recommandée (8–10 trames suffisent pour le diff) :

```
lum10_cct3000_v0, lum20_cct3000_v0, lum30_cct3000_v0   -> isole le champ luminosité
lum10_cct4000_v0, lum10_cct5000_v0                     -> isole le champ CCT
lum10_cct3000_v1, lum10_cct3000_v2                     -> isole le champ vitesse
off, ete, hiver
```

Puis : installer l'addon en local (`/addons/rf_lab` sur HAOS via Samba/SSH),
« Rechercher des mises à jour » dans le store, installer, ouvrir l'ingress.

---

## 10. Le protocole RF00234 — ce qu'on sait

Établi sur 2 captures réelles (lumière on/off, tout le reste constant), figées en
fixture dans `dev/fixtures/real_rf00234.json` et vérifiées par `dev/real_test.py`.

**Acquis :**

- **PWM confirmé**, timings nets : `bit 0 = (263 µs, 788 µs)`, `bit 1 = (788, 263)`.
- **Trame de 64 bits**, émise **6 fois**, répétitions séparées par ~2758 µs.
- En-tête Broadlink **`0xb1`** (pas `0xb2` — cf. §4), `repeats=192`.
- **Les 6 répétitions sont identiques** → pas de compteur qui s'incrémenterait
  *au sein* d'une émission.
- La capture est encadrée d'un silence de ~1,34 s et d'un gap final de ~49 ms.

```
              0         1         2         3         4         5         6
              0123456789012345678901234567890123456789012345678901234567890123
  lumière OFF 0000000000000011011101100001000000010010000010000000000010110010
  lumière ON  0000000000000011011101100001001110010010000010000000000000101111
  diff                                    ^^^                       ^  ^^^ ^
```

- **Le protocole est DÉTERMINISTE.** Le même état capturé deux fois donne des
  trames strictement identiques → **pas de compteur anti-rejeu** (§7). Le replay
  marchera, et le dernier octet est donc calculé, pas tiré au sort.

### La Mantra RF00143 — second protocole, décodé à 48/48

Autre modèle Mantra, **51 captures réelles** figées dans
`dev/fixtures/real_rf00143.json`, vérifiées par `dev/rf00143_test.py`. Une seule
fonction (`model()` dans le test) explique **les 7 champs des 51 captures, sans
exception**. Timings `296/985 µs` contre `263/788`, trame de **48 bits** contre
64, disposition sans rapport — et zéro ligne de code spécifique. C'est le
garde-fou du projet : ce test échoue si l'outil redevient spécifique à un appareil.

| bits | champ | valeurs |
|---|---|---|
| 0-15 | préambule + ID | `0xAB 0xB2` — constant |
| 16-17 | mode moteur | `00` normal, `01` nuit, `10` éco |
| 18 | reverse | |
| 19-23 | **CCT** | 4→24 (3000→5000 K par pas de **100** — 21 teintes) |
| 24 | lumière on/off | |
| 25-27 | **vitesse** | **0 = éteint**, 1-6, **7 = éco** |
| 28-31 | luminosité | 2-11 |
| 32-35 | timer | 1h→1, 2h→2, 4h→4, 8h→8 |
| 36-39 | **code de commande** | **PAS décoratif** — commande le bloc ventilo |
| 40-47 | checksum | `sub8` k=0x55 |

### Ce qui se transfère d'une Mantra à l'autre — et ce qui ne se transfère pas

| | RF00234 | RF00143 |
|---|---|---|
| checksum | `sub8` k=0x55 | **identique** |
| luminosité | `lum10 → 2` … `lum100 → 11` | **identique** |
| mode moteur | `00` normal, `01` nuit, `10` éco | **identique** |
| octet de commande | oui, décoratif | oui, décoratif |
| lampe éteinte | garde son niveau, bit 32 tombe | garde son niveau, bit 24 tombe |
| **ventilo éteint** | **garde sa vitesse, bit 40 tombe** | **PAS de bit d'alim : vitesse = 0** |

**Sur la prochaine Mantra, essayer `sub8` k=0x55 en premier** : le piège n°1 se
résout avant d'avoir capturé.

**Mais la symétrie « un bit d'alimentation par organe » de §10 n'est PAS une
loi.** Sur la RF00143, le ventilateur n'a pas de bit d'alimentation : « éteint »
s'écrit « vitesse 0 », et la vitesse est **perdue** — vérifié à v3 et à v6. La
lampe, sur la **même** télécommande, respecte pourtant la règle. Deux organes,
deux conventions. La symétrie reste une bonne hypothèse de départ ; ce n'est
qu'une hypothèse.

L'éco est encodé **deux fois** : bits 16-17 à `10`, *et* le champ vitesse écrasé
à 7 quelle que soit la vitesse réelle (vérifié à v1 et à v4). Qui génère une
trame éco doit poser les deux.

### L'octet de commande N'EST PAS décoratif — contrairement à la RF00234

C'est la différence la plus importante entre les deux modèles, et elle vaut
contre-exemple : ce qui est vrai d'une Mantra ne l'est pas de l'autre.

Établi sur le matériel (16/07/2026), un paramètre à la fois :

| champ | appliqué quand | sémantique |
|---|---|---|
| `light` | **toujours** | **absolue** — vérifiée |
| `lum` | **toujours**, quel que soit `cmd` | **absolue** — vérifiée, répétable |
| `cct` | toujours (présumé) | absolue (présumée) |
| `speed` | `cmd=10` | **absolue** — vérifiée (0 arrête, 2 et 6 tournent) |
| `reverse` | `cmd=12` | **TOGGLE — le bit 18 est IGNORÉ** |
| `mode` | `cmd=13` | **absolue** — vérifiée, répétable (à `cmd=8`, rien ne bouge) |
| `timer` | présumé `cmd=14` | inconnue |

`cmd` ne sélectionne pas un *bloc*, il sélectionne **le champ ventilateur** à
appliquer : à `cmd=10`, `reverse` ne bouge pas ; à `cmd=12`, il bouge. Le bloc
lampe, lui, voyage gratuitement avec n'importe quelle trame.

### Ce que la trame ENCODE ≠ ce que le récepteur EN FAIT

`reverse` le prouve : émettre avec `cmd=12` **inverse le sens à chaque fois**, que
le bit 18 vaille 0 ou 1. Le récepteur ne lit pas ce bit, il bascule. C'est une
**action**, pas un état.

Le bit 18 n'est pas une erreur de décodage pour autant : il existe, il porte
l'état que la télécommande **affiche**, et `infer` l'a trouvé correctement. C'est
la télécommande qui suit son état interne — le récepteur s'en moque.

**Aucun diff de bits ne peut révéler ça.** La carte des champs dit ce que la
télécommande pense ; seul le matériel dit ce que le récepteur fait. Les deux se
décrivent séparément, et §1 ne prévenait que du premier (« le bouton bascule »).

**`reverse` est le SEUL toggle** — ce n'est pas une règle du protocole, c'est une
propriété de ce champ. `mode` est pourtant lui aussi un bouton qui *cycle* sur la
télécommande, et le récepteur, lui, **lit sa valeur** : émettre dix fois `mode=1`
laisse le régime sur nuit. Le caractère « bouton bascule » de la télécommande
n'implique donc **rien** sur le récepteur, dans un sens comme dans l'autre. Il
faut tester chaque champ, en émettant **deux fois de suite la même valeur** : si
l'appareil change à la seconde, c'est un toggle.

**Conséquence sur les entités HA :** un champ toggle ne se *règle* pas, il se
*bascule*. Sans retour d'état, HA ne peut pas garantir un sens de rotation — au
mieux un `button` « inverser le sens », honnête, ou un `switch` optimiste qui
dérive dès qu'on touche la vraie télécommande. À vérifier pour `mode` : si
`cmd=13` fait défiler normal → nuit → éco à chaque émission, même verdict.

Les codes observés à la capture, cohérents avec ça : `1` lumière off, `3`
luminosité, `4` lumière on, `5` CCT ; `2` ventilo off, `10` vitesse, `12`
reverse, `13` mode, `14` timer.

**Conséquence lourde sur le livrable.** La trame n'est PAS absolue du point de vue
du récepteur : elle porte l'état complet, mais il n'en applique que la lampe plus
un champ ventilateur. Régler vitesse + sens + mode + timer demande donc **une
trame par champ ventilateur modifié** (`cmd=2` reste inutile : `cmd=10` avec
`speed=0` arrête le ventilateur). La lampe est gratuite : elle arrive avec la
première.

Le format de profil (`shared/profile.py`) ne sait pas exprimer ça — il suppose
qu'une trame suffit, ce qui est vrai de la RF00234 et faux ici. Il lui faudra un
`cmd` par champ, et au pont d'émettre autant de trames que de champs ventilateur
changés.

**Je me suis trompé trois fois de suite sur ce champ, de la même façon :**

1. « décoratif » par analogie avec la RF00234, sans aucun test ;
2. « décoratif, vérifié sur le matériel » — après un test qui ne portait que sur
   la LAMPE, c'est-à-dire précisément le seul bloc qui s'applique toujours ;
3. le test qui a tranché ne fait varier qu'**un** paramètre à la fois (`cmd`
   seul, `speed` constant). C'est la méthode du projet, écrite en §9, et je ne
   l'ai appliquée qu'en troisième.

**Reste ouvert :** le timer donne `1/2/4/8`, ce qui colle aussi bien à un
compteur d'heures sur 4 bits (3 h, 5 h, 15 h seraient alors possibles) qu'à
quatre bits indépendants. Aucune capture ne peut trancher — la télécommande n'a
pas le bouton. **L'outil, si : générer `timer=3` et regarder.** C'est exactement
comme ça que le timer de la RF00234 a livré ses pas de 2 minutes.

### Le piège que ce protocole a révélé dans l'outil

`fan` et `speed` s'y partagent le champ [25,28), donc **aucun des deux ne
l'explique seul**. « Déduire les champs » en concluait « une capture est mal
étiquetée » — à tort, l'étiquetage était juste.

Ce symptôme a **deux causes de signature formelle identique** : un champ partagé,
ou un vrai étiquetage fautif (ce que la RF00234 a par ailleurs). **On ne peut pas
les départager par corrélation** — testé, ça se trompe dans les deux sens (§
docstring de `shared/infer.py`). D'où `infer.isolated_pairs` : deux captures qui
ne diffèrent QUE par le paramètre en cause. Aucune heuristique, et c'est « ne
fais varier qu'un seul paramètre à la fois » appliqué par la machine.

### Le checksum — trouvé

```
checksum (bits 56-63) = (0x55 - somme des octets 0..6) & 0xFF
```

Autrement dit `(somme + checksum) & 0xFF` vaut toujours `0x55`. Vérifié sur les
3 états distincts. Implémenté sous le nom **`sub8`** (`k=0x55`) dans
`decoder.compute_checksum`, et `decoder.detect_checksum` le retrouve seul — le
bouton « Détecter » de l'UI s'appuie dessus.

Leçon au passage : la force brute initiale l'avait manqué parce qu'elle testait
`sum8`, `xor8`, `~sum8`, `-sum8` et les CRC-8 usuels, mais pas `k - somme` avec
une constante libre. Le motif qui a mis sur la piste : entre deux états, un octet
de données perd 2 et le checksum gagne 2.

### La carte des champs

Établie sur 22 captures réelles : les 11 paliers de luminosité et les 8 vitesses,
chaque série ne faisant varier qu'un seul paramètre.

**La trame s'organise par octet, un octet par organe :**

| octet | bits | contenu |
|---|---|---|
| 0-2 | 0-23 | préambule + ID appairé — constant, ne jamais toucher |
| 3 | 24-31 | code de la dernière commande — **décoratif** |
| **4** | **32-39** | **BLOC LUMIÈRE** |
| **5** | **40-47** | **BLOC VENTILO** |
| 6 | 48-55 | **inconnu**, constant à `0x00` sur 35 états — le timer doit être là |
| 7 | 56-63 | **checksum** `sub8` k=0x55 |

**Octet 4 — la lumière :**

| bits | champ | valeurs |
|---|---|---|
| 32 | lumière on/off | |
| 33-35 | **CCT**, MSB-first | **1 à 7** (3000 / 3300 / 3700 / 4000 / 4300 / 4700 / 5000 K) |
| 36-39 | **luminosité**, MSB-first | **1 à 11** |

**Octet 5 — le ventilateur :**

| bits | champ | valeurs |
|---|---|---|
| 40 | ventilo on/off | |
| 41 | **reverse** | |
| 42-43 | **mode moteur** | `00` normal, `01` nuit, `10` éco — `11` jamais observé |
| 44-47 | **vitesse**, MSB-first | **1 à 8** |

Chaque organe a son bit d'alimentation en tête d'octet, puis ses réglages. Cette
symétrie a valeur prédictive : elle a fait trouver le reverse (bit 41) et le CCT
(33-35) du premier coup, avant même de capturer.

**Nuit et éco appartiennent au VENTILATEUR, pas à la lampe**, et ils **s'excluent** :
c'est un champ à 3 valeurs, pas deux booléens. Le `meta_schema` par défaut les
modélise encore en booléens séparés — à corriger en un `enum` mode moteur.

**Éteindre ne remet jamais le niveau à zéro.** Lumière éteinte : `lum` et `cct`
gardent leur valeur, seul le bit 32 tombe. Ventilo éteint : `speed` garde la
sienne, seul le bit 40 tombe. C'est la réponse définitive à la question de §1.

**Éteindre ne remet jamais le niveau à zéro.** Lumière éteinte : `lum` garde sa
valeur, seul le bit 32 tombe. Ventilo éteint : `speed` garde sa valeur (8 observé),
seul le bit 40 tombe. C'est la réponse définitive à la question de §1.

### Les bits de contexte sont décoratifs

Les bits 26-27 annoncent quel sous-système l'appui visait, et les bits 30-31
décrivent son état (allumé / au-dessus du minimum). Un modèle
`bit30 = visé allumé`, `bit31 = visé au-dessus du minimum` explique 21 des 22
captures. L'exception : deux captures aux champs d'état rigoureusement identiques
diffèrent au seul bit 30 — il dépend donc du **bouton pressé**, pas de l'état.

**Le récepteur les ignore, c'est vérifié** : une trame générée depuis une
référence `cible = ventilo`, avec `lum` modifié, a bien fait changer la lampe
(15/07/2026). D'où leur rôle `const` dans le store — on les recopie tels quels
sans chercher à les calculer.

C'est aussi ce qui explique rétrospectivement le seul écart de la série
luminosité : à `lum=1`, le bit 31 tombe à 0 parce que le niveau visé est au
minimum. Le même phénomène se reproduit à l'identique sur `v1`.

**Réponse à la question de §1 : « lumière éteinte » n'est PAS `lum=0`.** La capture
lumière éteinte garde `lum=2` dans son champ ; c'est le **bit 32** qui tombe à 0.
Il y a donc bien un bit d'alimentation dédié.

**Les deux niveaux sont linéaires et sans surprise :**

| lum saisi | 1 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| brut [36:40] | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |

La vitesse suit exactement la même règle : `v1..v8` → brut `1..8` sur [44:48].

### La preuve que la génération est correcte

En partant d'une capture à `lum=2` et en ne changeant que le champ luminosité
(+ recalcul du checksum), **10 des 11 paliers générés sont identiques bit pour bit
à la vraie capture** de la télécommande. Pas « plausibles » : identiques. Figé dans
`dev/real_test.py`.

Le 11ᵉ, `lum=1`, diffère **exactement au bit 31** — c'est ce qui a révélé ce bit.

### Émission validée sur le ventilo (15/07/2026)

Une trame de luminosité générée depuis une référence à un autre palier, jamais
émise par la télécommande, a été envoyée via le RM4 : **le ventilo a obéi**.
4 émissions, 0 erreur.

C'est la validation de bout en bout de tout le projet. Corollaire utile : le
checksum est forcément correct — une somme fausse et le récepteur aurait ignoré
la trame sans le moindre signe (c'est exactement le piège de §7).

**Reste à cartographier :** le timer (1h/2h/4h/8h), seul locataire possible de
l'octet 6. Et la 4e valeur du mode moteur (`11`), jamais observée.

### L'espace d'états, et ce qu'il impose

```
light 2 × cct 7 × lum 11 × fan 2 × reverse 2 × speed 8 × mode 3  =  14 784
                                                    × timer 5  =  73 920
```

**Chaque trame porte les DEUX organes.** Il n'existe pas de commande « lampe
seule » : émettre une luminosité impose aussi un état complet au ventilateur.
L'espace à couvrir est donc le produit cartésien, pas la somme.

Conséquence directe sur le livrable de §2 : **des codes en dur dans le YAML sont
hors de question** (73 920 × 1084 caractères ≈ 80 Mo). Le package HA devra appeler
l'addon en `rest_command`, et l'addon générera la trame à la volée. Ça implique
que l'addon tourne en permanence — donc de remplacer le serveur de dev Flask par
un vrai WSGI avant de s'en servir en production.

**Points ouverts côté outil :**

- **`compute_checksum` suppose un CRC aligné sur l'octet.** Il calcule sur
  `bits[:crc_start] + bits[crc_end:]` puis repadde à l'octet : si la tranche CRC
  n'est pas alignée, la retirer décale tout ce qui suit. Sain pour la RF00234
  (CRC en [56,64), rien après), à revoir pour tout autre protocole.
- **`/api/generate` ignore le paramètre `mode`** et appelle `decode_pwm` en dur.
  L'UI avertit en Manchester au lieu de laisser générer — garde-fou, pas correctif.
- **Pas d'endpoint d'export** : l'UI construit JSON et YAML côté navigateur.
- **`field_value` n'est pas exposée** par l'API et est recopiée en JS (cf. §6).
- **`get_device()` met `_dev` en cache indéfiniment** et son paramètre `force` n'est
  jamais utilisé : pas de ré-auth si la session RM4 expire.
- **`/api/status` déclenche une découverte broadcast de 5 s à chaque appel** tant que
  `device_ip` est vide. L'UI n'appelle donc `status` qu'au chargement et sur le bouton.
- **Le YAML d'export s'appuie sur les valeurs brutes**, pas sur les grandeurs
  physiques (cf. §6.5) : le mapping reste à reporter à la main dans les templates.

- **`compute_checksum` est probablement faux sur un cas réel.** Il calcule sur
  `bits[:crc_start] + bits[crc_end:]` puis repadde à l'octet : si le champ CRC n'est
  pas aligné sur un octet, retirer la tranche décale tout ce qui suit et produit un
  flux qui ne correspond à rien de ce que calcule la télécommande. Il inclut aussi les
  bits *après* le CRC, alors que la plupart de ces protocoles ne somment que ce qui le
  précède. À reprendre le jour où les captures montrent qu'il y a bien un checksum.
- **`/api/generate` ignore le paramètre `mode`** et appelle `decode_pwm` en dur.
  L'UI affiche un avertissement en Manchester au lieu de laisser générer — c'est un
  garde-fou, pas une correction.
- **Pas d'endpoint d'export** : l'UI construit JSON et YAML côté navigateur.
- **`field_value` n'est pas exposée** par l'API et est recopiée en JS (cf. §6).
- **`get_device()` met `_dev` en cache indéfiniment** et son paramètre `force` n'est
  jamais utilisé : pas de ré-auth si la session RM4 expire.
- **`/api/status` déclenche une découverte broadcast de 5 s à chaque appel** tant que
  `device_ip` est vide. L'UI n'appelle donc `status` qu'au chargement et sur le bouton.
- **Le YAML d'export s'appuie sur les valeurs brutes**, pas sur les grandeurs
  physiques (cf. §6.5) : le mapping reste à reporter à la main dans les templates.