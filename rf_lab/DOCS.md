# RF Lab

L'atelier de rétro-ingénierie. Il sert à **décoder** une télécommande et à produire
un **profil d'appareil** — que [RF Bridge](https://github.com/powange/broadlink-lab/tree/main/rf_bridge)
transformera en appareil Home Assistant.

C'est un outil **temporaire** : une fois ta télécommande décodée et le profil
enregistré, désinstalle-le. Les profils restent dans `/share/broadlink_lab/`.

Si quelqu'un t'a déjà donné un profil pour ton appareil, **tu n'as pas besoin de cet
addon** — installe seulement RF Bridge et importe le fichier.

## Prérequis

Un Broadlink : **RM4 Pro** ou **RM Pro** pour la RF (433/315 MHz), n'importe quel RM
pour l'infrarouge. Un RM Mini ne fait que de l'IR.

## Configuration

| Option | Défaut | |
|---|---|---|
| `device_ip` | vide | IP du Broadlink. Vide = découverte broadcast. Modifiable à chaud depuis l'UI, ce qui est plus pratique — ces appareils sont en DHCP. |
| `frequency` | `433.92` | Fréquence d'écoute RF, en MHz. |
| `log_level` | `info` | |

## Le principe

Beaucoup de télécommandes RF n'envoient **pas de commandes incrémentales**. Elles
transmettent à chaque appui la trame de l'**état complet**. Apprendre « luminosité + »
capture donc un état figé — le rejouer force cet état au lieu d'incrémenter.

D'où l'idée : **décoder le protocole** pour fabriquer la trame de n'importe quel état.
C'est plus rapide qu'apprendre la combinatoire, et ça donne accès à des états que la
télécommande ne sait pas demander.

## Une session, en pratique

**La règle d'or : ne fais varier qu'un seul paramètre à la fois.** Le diff devient
alors d'une lisibilité immédiate. Tout le reste découle de ça.

### 1. Connecte le Broadlink

Barre d'état : saisis l'IP, ou clique **Chercher**. Depuis un addon HA la découverte
broadcast fonctionne directement.

### 2. Capture

Renseigne les **paramètres d'état** (luminosité, vitesse…) puis clique **Capturer** et
appuie sur la télécommande.

**Ces paramètres sont obligatoires, et c'est le cœur de la méthode** : sans eux, rien
ne permet de corréler un champ de bits à une grandeur physique. Le bouton « Paramètres
d'état… » te laisse déclarer ceux de *ta* télécommande.

Trois pièges :

- **Les toggles.** Un bouton « inverser le sens » ne t'annonce pas le résultat, il le
  bascule. Étiquette avec l'état **obtenu** — en regardant l'appareil — pas avec le
  bouton pressé.
- **Les paramètres non déclarés.** La trame les porte quand même. S'ils changent sans
  que tu les notes, leurs bits bougeront *sans corrélation logique* — et ça imite un
  checksum à s'y méprendre.
- **La valeur n'a pas besoin d'être vraie.** Elle doit être *reproductible et
  ordonnée*. Si ta télécommande n'affiche rien, compte les appuis : 1, 2, 3.

### 3. Lis la grille

Une ligne par capture, une colonne par bit. Les colonnes **surlignées** varient.
Trier par paramètre aide énormément à voir l'incrément.

Sélectionne une tranche au **clic-glisser** pour la nommer. L'UI affiche alors sa
valeur décodée par capture : `lum10 / lum20 / lum30 → 1 / 2 / 3`, et c'est gagné.

Pour chaque champ, renseigne ses **bornes réelles** (`min`/`max`) — pas la largeur en
bits. Un champ de 4 bits qui ne va que de 1 à 11 doit le dire.

### 4. Repère le checksum

C'est le champ qui bouge **beaucoup** alors que tu n'as changé qu'une valeur. Passe
son rôle à `crc` et clique **Détecter** : l'outil cherche l'algorithme qui explique
toutes tes captures. Il lui faut **au moins 3 états distincts** pour ne pas conclure
par hasard.

**C'est le piège n°1.** Sans checksum correct, une trame générée est ignorée par le
récepteur — sans le moindre message, sans rien pour comprendre.

### 5. Protège ce qui ne doit pas bouger

Le gros bloc invariant en tête de trame, c'est le **préambule + l'ID appairé** avec le
récepteur. Passe-le en rôle **`const`** : il ne sera jamais réécrit. Y toucher, et
l'appareil ignore tout.

### 6. Génère et émets

Un slider par champ, **Générer**, puis **Émettre**. Le bouton reste bloqué tant que la
trame n'est pas vérifiée — l'outil re-décode ce qu'il a fabriqué avant de l'envoyer.

Boucle en une seconde : change un slider, émets, regarde l'appareil.

### 7. Exporte le profil

Nomme l'appareil, puis **Construire le profil** (téléchargement, pour la sauvegarde
ou le partage) et **Enregistrer dans /share** — RF Bridge le détecte tout seul.

Le profil embarque la **capture de référence** : c'est elle qui porte l'ID appairé.
Le pont n'a donc besoin d'aucune capture, juste d'un Broadlink pour émettre.

## Partager un profil

Un profil mélange deux choses de nature différente :

- la **carte des bits**, le checksum et les entités — le savoir sur le **modèle**,
  identique pour tous les exemplaires, donc partageable ;
- la **capture de référence** — l'ID appairé de *ta* télécommande.

Quelqu'un avec le même appareil importe ton profil, capture **une** trame de sa propre
télécommande pour le réancrer, et récupère tout le reverse sans rien décoder. L'UI le
lui demande à l'import.

## Si ça ne marche pas

**Le Broadlink ne répond pas.** Ce sont des appareils WiFi qui **dorment** : le premier
paquet les réveille et se perd. L'addon réessaie trois fois pour ça. Devant un échec,
réessaie avant de conclure — un timeout court les déclare absents alors qu'ils sont là.

**La grille affiche du bruit.** Ajuste le `gap` dans la barre d'état, ou essaie
Manchester. Attention : le décodage Manchester fonctionne, mais **pas la génération** —
le rebuild exige les index PWM.

**Les trames n'ont pas la même longueur.** Le `gap` est mal réglé, les colonnes ne
s'alignent pas et le diff ne veut rien dire.

**La trame générée ne fait rien.** Neuf fois sur dix, c'est le checksum. Vérifie qu'il
est détecté et que le champ `crc` est bien nommé.

**Un champ bouge sans raison.** Soit c'est le checksum, soit c'est un paramètre que tu
n'as pas déclaré et qui a changé sans que tu le saches.
