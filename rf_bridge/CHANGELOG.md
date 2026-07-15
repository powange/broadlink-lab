# Journal des modifications

## 0.3.1

**L'UI démarre même sans broker MQTT.** La boucle de connexion tournait dans le
thread principal, avant `serve()` : si Mosquitto ne répondait pas, l'interface ne
démarrait **jamais** — exactement au moment où on en a besoin pour comprendre
pourquoi. MQTT passe en tâche de fond, l'UI d'abord.

**L'import ne promet plus ce qu'il n'a pas fait.** Il annonçait « Il apparaît dans
Home Assistant » sans vérifier que la discovery avait été publiée. Sans MQTT, elle
ne l'est pas. Le message dit maintenant ce qui s'est réellement passé.

**Une alerte rouge quand MQTT est déconnecté** — c'est la cause n°1 d'un appareil
qui n'apparaît pas dans HA, et rien ne le signalait.

**L'IP du Broadlink se règle depuis l'UI du pont**, et elle est persistée. Le labo
l'avait, le pont l'avait oublié. Avec la recherche sur le réseau et les 3 essais
qu'imposent ces appareils qui dorment.

## 0.3.0

**Le pont devient autonome.** Il ne dépend plus de RF Lab : son UI permet d'importer
un profil. Avant, récupérer le profil d'un ami imposait d'installer le labo juste
pour déposer un fichier.

**UI d'administration** (sidebar) : les appareils publiés avec leur état, l'import, le
retrait, l'état MQTT et Broadlink — et surtout **les profils refusés avec leur
raison**. Avant, un profil invalide n'était visible que dans les logs.

**Rechargement à chaud.** Déposer un fichier dans `/share/broadlink_lab/` suffit :
détecté en quelques secondes, pas de redémarrage. Un fichier supprimé retire
l'appareil de HA (config vide et retenue). Avant, ajouter un appareil imposait de
redémarrer l'addon depuis le panneau HA, sans que rien ne le dise.

**Le dossier partagé devient `/share/broadlink_lab/`** — neutre. Il s'appelait
`/share/rf_lab/`, au nom du labo, alors que c'est le pont qui le consomme.

**Le pont ne sort plus si aucun profil n'est chargé.** Il bouclait en redémarrage sans
rien expliquer ; l'UI reste maintenant joignable pour en importer un.

## 0.2.0

MQTT discovery : HA voit un vrai appareil, avec fabricant, modèle et entités groupées.
Remplace le package YAML — l'espace d'états dépasse 3,7 millions de combinaisons
(chaque trame porte tous les organes), soit ~80 Mo de codes en dur. Le pont génère à
la volée.

État persisté dans `/data` : sans ça, un redémarrage réémettrait un état arbitraire.

## 0.1.0

Première version.
