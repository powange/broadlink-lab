# Journal des modifications

## 0.5.3

**« Suivre la télécommande » ne se synchronisait qu'une fois.** Le premier appui
sur la vraie télécommande mettait bien HA à jour, puis plus rien.

Le RM4 sort du mode écoute dès qu'il rend une trame, et il refuse de s'y remettre
tant que la session n'a pas été close par `cancel_sweep_frequency`. La boucle
d'écoute rappelait `find_rf_packet` sans ce cancel : le réarmement échouait en
silence, et seule la première trame était captée. Le pont cancelle maintenant
après chaque trame reçue, et en sortie d'écoute quand une émission l'interrompt.

## 0.5.2

**Le ventilateur d'une télécommande sans bit d'alimentation apparaît enfin dans
HA.** L'appareil se créait, mais sans ses commandes de ventilateur — seuls la
lumière et le timer étaient là.

Home Assistant réserve la valeur *sous* `speed_range_min` pour « éteint », et
exige donc un minimum ≥ 1. Sur la Mantra R00143, la vitesse 0 **est** l'arrêt, et
le pont publiait `speed_range_min: 0` — ce qui faisait rejeter toute l'entité
ventilateur par HA, sans le moindre message. La plage commence maintenant à 1, et
le 0 sert d'« éteint » comme HA l'attend.

## 0.5.1

**Un ventilateur sans bit d'alimentation est enfin importable.** La validation
exigeait un champ `power` sur toute entité `fan`. Or certaines télécommandes n'en
ont pas — la Mantra R00143 écrit « éteint » en « vitesse 0 ». Leur profil était
refusé à l'import avec « fan.power : il faut au moins {field} », alors qu'il était
correct. `power` est désormais optionnel : sans lui, la vitesse 0 vaut extinction.

## 0.5.0

**« Suivre la télécommande » — l'état de Home Assistant peut cesser de mentir.**

Jusqu'ici un appui sur la télécommande physique désynchronisait HA **pour
toujours** : l'appareil n'accuse jamais réception, donc rien ne pouvait le
corriger. Le pont sait maintenant écouter la vraie télécommande et adopter son
état.

Un **switch par appareil, éteint par défaut**, parce que le coût est réel :
écouter monopolise le Broadlink, qui n'est plus disponible pour le labo ni pour
l'intégration Broadlink de HA. Tant qu'aucun appareil n'est suivi, le pont ne
touche pas la radio.

Trois choses qui se déduisent du matériel :

- **Le Broadlink est half-duplex** : il écoute ou il émet. Toute émission coupe
  l'écoute, qui est réarmée juste après — quelques dizaines de millisecondes
  d'aveuglement, et seulement quand HA commande.
- **Une seule fréquence à la fois.** Deux appareils suivis sur des fréquences
  différentes sont impossibles à écouter ensemble : le pont le dit dans son
  journal plutôt que d'en ignorer un en silence.
- **Les toggles sont exclus.** Le bit d'un champ « bascule » dit ce que la
  télécommande croit ; dès que le pont émet, l'appareil bascule sans qu'elle le
  sache. Adopter sa croyance propagerait son erreur.

Nouveau champ de profil : **`identity`** — le préambule et l'ID appairé, ce qui
permet de reconnaître une trame entendue. À ne pas confondre avec `const`, qui
dit seulement « ne réécris pas » : l'octet de commande est const *et* change à
chaque bouton pressé.

## 0.4.0

**Profils v2 : `requires` et `semantics`.** Le pont supposait qu'une trame porte
l'état et que l'appareil l'applique en entier. C'est vrai de la Mantra RF00234 —
et faux de la Mantra R00143, dont le récepteur n'applique que les champs libres
plus **un** champ désigné par l'octet de commande.

Deux notions, que les bits ne disent pas et que seul le matériel révèle :

- **`requires`** — « ce champ ne s'applique que si `cmd` vaut 10 ». Régler
  vitesse *et* sens émet donc deux trames. Les champs libres (le bloc lampe)
  voyagent gratuitement avec chacune : inutile de leur en dédier une.
- **`semantics: toggle`** — le récepteur **ignore** la valeur du champ et
  bascule. Redemander le sens courant n'émet donc rien : une trame « pour rien »
  inverserait le ventilateur.

Les profils v1 restent lisibles et se comportent comme avant. Un profil v2 est
**refusé** par un pont trop ancien, plutôt que d'être appliqué de travers en
silence.

**Correction : le pont s'abonne avant d'annoncer.** Il publiait la discovery puis
s'abonnait aux topics de commande. Home Assistant, qui envoie volontiers une
commande dès qu'il découvre une entité, tombait dans cette fenêtre — et la
commande était perdue sans le moindre message.


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
