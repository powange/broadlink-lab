# Broadlink Lab

**Deux addons Home Assistant pour rétro-ingénierer une télécommande RF ou IR via
un Broadlink, et en faire un vrai appareil HA.**

[![Ajouter à Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fpowange%2Fbroadlink-lab)

---

## Le problème

Beaucoup de télécommandes RF n'envoient **pas de commandes incrémentales**. Elles
maintiennent leur état en interne et transmettent, à chaque appui, la trame de
l'**état complet** — luminosité *et* température de couleur *et* vitesse *et* mode.

Apprendre « luminosité + » avec `remote.learn_command` capture donc un état figé.
Le rejouer force l'appareil à cet état précis au lieu d'incrémenter. Et couvrir la
combinatoire demanderait des centaines de codes appris à la main.

## La réponse

**Décoder le protocole plutôt qu'apprendre des codes.** Une fois la carte des bits
connue, on fabrique la trame de n'importe quel état — y compris ceux que la
télécommande ne sait pas demander.

Sur le ventilateur qui a servi de cobaye, la minuterie s'est révélée être un champ
de 8 bits comptant par pas de 2 minutes. La télécommande n'a que quatre boutons
(1 h, 2 h, 4 h, 8 h) ; le récepteur, lui, accepte **n'importe quelle durée de 2 min
à 8 h 30**. C'est le genre de chose qu'on ne trouve qu'en décodant.

## Les deux addons

### `rf_lab` — le labo

L'atelier de rétro-ingénierie, avec une UI dans la sidebar. Il parle au Broadlink
**en direct** : pas de `learn_command` asynchrone, pas de balayage de fréquence.
Une capture = un clic.

- **Grille de bits** — une ligne par capture, une colonne par bit. Les colonnes qui
  varient sont surlignées. On sélectionne une tranche au clic-glisser et on la nomme.
- **Détection de checksum** — cherche l'algorithme qui explique toutes les captures.
  C'est le piège n°1 : sans lui, une trame générée est ignorée sans le moindre message.
- **Générateur** — un slider par champ, et on émet. La boucle
  *générer → émettre → observer* prend une seconde.
- **Profil d'appareil** — le livrable, téléchargeable et partageable.

C'est un outil **temporaire** : le reverse fini, désinstalle-le.

### `rf_bridge` — le pont

Lit les profils et publie de **vrais appareils Home Assistant** par MQTT discovery :
fabricant, modèle, entités groupées sous une seule carte. Rien à coller dans
`configuration.yaml`.

Il est **générique** — il ne connaît aucune télécommande, il applique le profil
qu'on lui donne. Une nouvelle télécommande, c'est une session de labo et un fichier
de plus. **Zéro ligne de code.**

Son UI liste les appareils publiés, permet d'**importer un profil**, et montre
pourquoi un profil serait refusé. Les profils sont **rechargés à chaud** : déposer
un fichier suffit, pas de redémarrage.

**Il ne dépend pas de RF Lab.** Le labo sert à *fabriquer* un profil. Si tu en as
déjà un — partagé, sauvegardé — le pont seul suffit.

### Ils ne se parlent jamais

Ils s'échangent un fichier dans `/share/broadlink_lab/`. Le dossier n'appartient à
aucun des deux : chacun démarre, s'arrête et se désinstalle sans l'autre.

## Prérequis

- **Un Broadlink.** RM4 Pro ou RM Pro pour la RF (433/315 MHz) ; n'importe quel RM
  pour l'infrarouge. C'est une dépendance dure : la capture et l'émission passent
  par [python-broadlink](https://github.com/mjg59/python-broadlink).
- **Mosquitto** (addon officiel), pour `rf_bridge` uniquement. Le labo s'en passe.

## Installation

1. **Ajoute le dépôt** : bouton ci-dessus, ou *Paramètres → Modules complémentaires
   → Boutique → ⋮ → Dépôts* et colle `https://github.com/powange/broadlink-lab`.

**Tu as déjà un profil** (partagé, ou sauvegardé d'une installation précédente) :

2. **Installe `RF Bridge`**, démarre-le, ouvre-le dans la sidebar.
3. **« Importer un profil… »**. L'appareil apparaît dans HA. C'est tout — le labo
   est inutile.

**Tu pars de zéro** et dois décoder ta télécommande :

2. **Installe `RF Lab`**, renseigne l'IP de ton Broadlink (ou cherche-le depuis
   l'UI), démarre, ouvre-le dans la sidebar.
3. **Fais ton reverse** (voir ci-dessous), puis « Enregistrer dans /share ».
4. **Installe `RF Bridge`** : il détecte le profil tout seul.
5. Le reverse fini, **désinstalle le labo**. Les profils restent.

## Une session de reverse, en pratique

L'idée directrice : **ne faire varier qu'un seul paramètre à la fois.** Le diff
devient alors d'une lisibilité totale.

1. Capture un état de référence, puis le même en changeant **une** chose.
2. Regarde les colonnes surlignées. Un champ contigu qui incrémente proprement,
   c'est gagné.
3. Nomme la tranche, borne-la (`min`/`max` réels, pas la largeur en bits).
4. Repère le checksum : c'est le champ qui bouge **beaucoup** alors que tu n'as
   changé qu'une valeur. Clique « Détecter ».
5. Passe en `const` tout ce qui ne doit jamais être réécrit — préambule et ID
   appairé en tête de trame. Y toucher, et le récepteur ignore tout.
6. Génère un état inédit, émets, regarde l'appareil.
7. **Émets deux fois la même valeur.** Si l'appareil change à la seconde, ce
   champ est une *action*, pas un état : le récepteur ignore la valeur et
   bascule. Déclare-le, sinon Home Assistant l'inversera à chaque commande.

**Les métadonnées de capture sont obligatoires, et c'est le cœur de la méthode** :
sans elles, on ne peut corréler aucun champ de bits à une grandeur physique.

**La carte des bits ne dit pas tout.** Elle décrit ce que la télécommande
*pense* ; elle ne dit rien de ce que le récepteur *fait*. Sur un vrai
ventilateur, le sens de rotation s'inverse à chaque trame quoi que porte son bit,
et la vitesse n'est appliquée que si l'octet de commande vaut la bonne valeur —
si bien que régler vitesse *et* sens demande deux trames. Aucun diff ne le
montre : il faut émettre et regarder. Le dialogue de nommage permet de le
déclarer, et le pont s'y conforme.

**Attention aux toggles.** Un bouton « inverser le sens » ne t'annonce pas le
résultat, il le bascule. Étiquette avec l'état **obtenu**, pas avec le bouton pressé.

## Ce qu'on apprend en décodant

Deux télécommandes Mantra de modèles différents (RF00234 et RF00143) partagent le
**même checksum** — une somme complémentée avec la constante 0x55 — et la **même
correspondance de luminosité**, alors que leurs timings, la longueur de leurs
trames et la disposition de leurs bits n'ont rien à voir.

C'est la signature d'une famille de puces. Sur la troisième télécommande de la
marque, le piège du checksum sera résolu avant la première capture.

## Ce que ça ne fait pas

- **Broadlink uniquement.** Le décodage PWM, la génération et le pont MQTT sont
  indépendants du matériel, mais la couche paquet et la couche appareil sont
  Broadlink. Supporter un RTL-SDR demanderait de les réécrire.
- **L'état est optimiste.** Ces appareils n'accusent jamais réception : ce que HA
  affiche est ce qu'il a demandé, pas ce que la machine fait. Un appui sur la
  télécommande physique désynchronise, et rien ne peut le corriger.
- **Le décodage Manchester existe, mais pas la génération** : le rebuild exige les
  index PWM.

## Partager un profil

Un profil mélange deux choses :

- la **carte des bits**, le checksum et les entités — le savoir sur le **modèle**,
  identique pour tous les exemplaires, donc partageable ;
- la **capture de référence** — elle porte l'**ID appairé** de *ta* télécommande.

Quelqu'un avec le même appareil importe ton profil, capture **une** trame de sa
propre télécommande pour le réancrer, et récupère tout le reverse sans rien décoder.
L'UI le lui demande à l'import.

## Développement

Tout se teste **sans matériel** :

```bash
./dev/test.sh
```

Un faux Broadlink qui rejoue une séquence de capture *et décode ce qu'on lui émet*,
un vrai broker MQTT en Python pur, l'UI pilotée dans un DOM headless, et une
non-régression sur des captures de **vrai signal**.

Pour cliquer dans l'UI sans matériel, ou contre un vrai Broadlink :

```bash
python3 -m venv dev/venv && dev/venv/bin/pip install -r dev/requirements-dev.txt
dev/venv/bin/python dev/serve.py                    # faux Broadlink
dev/venv/bin/python dev/serve.py --device <ip>      # le vrai
```

Détails dans [dev/README.md](dev/README.md). Le protocole décodé, les décisions
d'architecture et les pièges rencontrés sont dans [CLAUDE.md](CLAUDE.md).

## Licence

[MIT](LICENSE) — fais-en ce que tu veux, garde la mention de copyright.

Le code de `shared/decoder.py` s'appuie sur le format de paquet de
[python-broadlink](https://github.com/mjg59/python-broadlink) (MIT), utilisé comme
dépendance.
