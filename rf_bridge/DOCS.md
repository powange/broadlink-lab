# RF Bridge

Transforme des **profils d'appareil** en **vrais appareils Home Assistant**, par MQTT
discovery : fabricant, modèle, entités groupées sous une seule carte. Rien à coller
dans `configuration.yaml`.

Il est **générique** : il ne connaît aucune télécommande. Il applique le profil qu'on
lui donne. Une nouvelle télécommande, c'est un fichier de plus — zéro ligne de code.

**Il ne dépend pas de RF Lab.** Le labo sert à *fabriquer* un profil ; si tu en as
déjà un, cet addon suffit.

## Prérequis

- **L'addon Mosquitto** (ou un autre broker MQTT). C'est une dépendance dure : le pont
  refuse de démarrer sans (`services: mqtt:need`). Aucun identifiant à saisir, le
  superviseur les lui fournit.
- **Un Broadlink** pour émettre. RM4 Pro ou RM Pro pour la RF, n'importe quel RM pour
  l'IR.

## Configuration

| Option | Défaut | |
|---|---|---|
| `device_ip` | vide | IP du Broadlink. Vide = découverte broadcast. |
| `profiles` | vide | Vide = tous les profils de `/share/broadlink_lab/`. Sinon, la liste des identifiants voulus. |
| `log_level` | `info` | |

## Ajouter un appareil

**Depuis l'UI** — ouvre RF Bridge dans la sidebar, clique **Importer un profil…** et
choisis le fichier JSON. L'appareil apparaît dans Home Assistant. C'est tout.

**Ou en déposant le fichier** dans `/share/broadlink_lab/` (Samba, File Editor…). Les
profils sont **rechargés à chaud** : pas de redémarrage, il est détecté en quelques
secondes. Supprimer le fichier retire l'appareil de HA.

L'UI liste aussi les **profils refusés** avec leur raison — plutôt que de les ignorer
en silence.

## Ce que tu obtiens

Le profil décrit comment présenter les champs à HA. Quatre types d'entités :

| Type | Ce qu'il donne |
|---|---|
| `light` | allumage, luminosité, température de couleur (en mireds, convertis depuis les kelvins du profil) |
| `fan` | allumage, pourcentage, sens de rotation, modes prédéfinis |
| `number` | un champ numérique brut, avec échelle et unité |
| `switch` | un champ d'un bit |

## Comment ça marche

**Chaque trame porte l'état complet de tous les organes.** Il n'existe pas de commande
« lampe seule » : émettre une luminosité impose aussi un état au ventilateur. Le pont
tient donc l'état complet en mémoire et **réémet tout** à chaque commande.

Pour fabriquer une trame, il part de la **capture de référence du profil** — elle porte
le préambule et l'ID appairé — réécrit les champs, recalcule le checksum, et inverse
les paires d'impulsions des bits qui changent. Il **re-décode ce qu'il a fabriqué** et
refuse d'émettre si ça ne retombe pas juste.

L'état est **persisté** dans `/data`. Sans ça, un redémarrage repartirait des valeurs
de la référence et réémettrait un état arbitraire — rallumant ta lampe en pleine nuit.

## Limites, à connaître avant d'installer

**L'état est optimiste.** Ces appareils n'accusent **jamais** réception. Ce que HA
affiche est ce qu'il a *demandé*, pas ce que la machine fait. Un appui sur la
télécommande physique désynchronise, et **aucune architecture ne peut corriger ça** —
l'appareil n'émet rien.

En pratique : si tu utilises encore la télécommande d'origine, HA se trompera jusqu'à
la prochaine commande envoyée depuis HA (qui, elle, resynchronise tout puisqu'elle
impose l'état complet).

**Un profil est lié à UNE télécommande.** La référence porte son ID appairé. Le profil
d'un voisin ne pilotera pas ton appareil tel quel — il faut le réancrer sur une capture
de ta propre télécommande, ce que RF Lab sait faire à l'import.

**Le pont n'a pas d'ingress sécurisé pour l'émission** : toute personne ayant accès à
l'UI peut piloter les appareils. C'est le cas de tous les addons `panel_admin`.

## Si ça ne marche pas

**« aucun profil » au démarrage.** Normal à la première installation. L'UI reste
joignable — importe un profil, l'addon ne redémarre pas en boucle pour autant.

**MQTT déconnecté.** L'addon Mosquitto tourne-t-il ? Le pont réessaie toutes les 5 s.

**Le Broadlink ne répond pas.** Ce sont des appareils WiFi qui **dorment** : le premier
paquet les réveille et se perd. Réessaie avant de conclure qu'il est absent.

**L'appareil n'apparaît pas dans HA.** Regarde les logs : un profil invalide est refusé
avec sa raison, et l'UI les liste. Vérifie aussi que l'intégration MQTT est bien
configurée dans HA.

**Un appareil ne disparaît pas après suppression.** Le pont publie une config vide et
retenue, c'est ce qui l'efface. Si HA le garde, redémarre l'intégration MQTT.
