# Journal des modifications

## 0.5.3

**On peut modifier un champ existant** : cliquer son nom dans la liste rouvre le
dialogue de nommage, pré-rempli. Avant, il fallait resélectionner ses colonnes
dans la grille — et sur un champ déjà nommé, ce n'était pas évident.

**La correspondance physique de la luminosité est déduite et exportée.** Le labo
lit dans tes métadonnées que le brut 2 vaut 10 % et le porte dans le profil
(`brightness.percent`). Sans ça, RF Bridge affichait la télécommande à 10 % comme
si elle était à 0 %. C'est le même principe que le `kelvin` déjà porté pour le CCT.

## 0.5.2

**Le labo produit une entité ventilateur même sans bit d'alimentation.** Il
n'ajoutait un ventilateur que s'il trouvait un champ `fan`. Une télécommande dont
« éteint » s'écrit « vitesse 0 » (Mantra R00143) n'a pas ce champ : son
ventilateur était absent du profil. Un champ `speed` suffit maintenant.

## 0.5.1

**Marquer ce qui identifie la télécommande.** Une case « identifie » sur les
champs `const` : c'est le préambule et l'ID appairé. RF Bridge s'en sert pour
reconnaître une trame entendue et suivre la vraie télécommande.

`const` ne suffisait pas : il veut dire « ne réécris pas », pas « ne varie
jamais ». L'octet de commande est const et change à chaque bouton — le confondre
avec l'identité faisait rejeter au pont ses propres trames.

## 0.5.0

**Déclarer ce que le récepteur fait d'un champ** — le labo produit des profils v2.

La carte des bits décrit ce que la télécommande *pense*. Elle ne dit rien de ce
que le récepteur *fait*, et aucun diff ne peut le révéler : il faut émettre et
regarder l'appareil. Le dialogue de nommage a donc deux nouvelles questions :

- **il lit la valeur, ou il bascule ?** Un champ « bascule » s'inverse à chaque
  trame quoi que porte son bit. Le sens de rotation d'une Mantra R00143 est comme
  ça. Sans le déclarer, HA l'inverserait à chaque commande.
- **seulement si ... vaut ...** Certains récepteurs n'appliquent qu'un champ à la
  fois, désigné par un octet de commande. Régler vitesse *et* sens demande alors
  deux trames — le pont les émet.

Les chips le montrent : `⇄` pour une bascule, `si cmd=10` pour une condition.

Ces deux réglages n'ont de sens qu'après avoir **émis pour voir**. Laissés tels
quels, le comportement est celui d'avant.

## 0.4.4

**Le générateur montre les valeurs plutôt que de les faire viser.** Un champ dont
les valeurs tiennent en 4 bits rend un bouton par valeur — `mode` devient
`[0][1][2]`. Au-delà, c'est encore un slider : `timer` sur 8 bits en ferait 256.

Trois widgets, et le choix se fait sur ce que le champ **accepte** :

| bornes | widget |
|---|---|
| 0-1 | interrupteur |
| max ≤ 15 | un bouton par valeur |
| au-delà | slider |

Les bornes réelles comptent doublement ici : `lum` affiche 11 boutons (1 à 11) et
pas 16, parce que la télécommande ne fait que ça.

## 0.4.3

**Les champs qui ne valent que 0 ou 1 ont un interrupteur**, plus un slider à deux
crans dont il fallait lire le chiffre à côté pour savoir s'il était mis.

Le critère est ce que le champ **accepte** (bornes 0-1), pas sa largeur en bits :
un champ de 2 bits borné à 0-1 est un booléen, et un bit unique aux bornes 0-1
aussi.

## 0.4.2

**« Déduire les champs » n'accuse plus ton étiquetage à tort.**

Quand aucun bit n'était fonction d'un paramètre, l'outil concluait « l'une des
deux captures est mal étiquetée » et exhibait deux captures aux bits différents.
Cette preuve n'en était pas une : elle piochait parmi les bits inexpliqués, où
vivent le checksum et le code de commande — ceux-là diffèrent entre deux captures
quelconques, par construction. Il « prouvait » donc n'importe quoi.

Et la conclusion elle-même était fausse une fois sur deux. Un paramètre
inexpliqué a deux causes indiscernables : un étiquetage fautif, ou un **champ
partagé** — sur certaines télécommandes, « ventilateur éteint » s'écrit
« vitesse 0 », sans bit d'alimentation dédié. Les deux ont la même signature.

À la place, l'outil rend la preuve la plus solide qui soit, et sans aucune
heuristique : **deux captures qui ne diffèrent QUE par ce paramètre**. Les bits
qui changent entre elles sont forcément les siens. C'est « ne fais varier qu'un
seul paramètre à la fois » appliqué par la machine. S'y ajoute le bit qui
suivrait le paramètre si quelques captures étaient ré-étiquetées — présenté comme
une piste, pas un verdict.

## 0.4.1

**Tri par ordre de capture.** La grille se triait par nom ou par paramètre
d'état ; elle sait maintenant rendre les captures dans l'ordre où tu les as
faites. Utile pour retrouver la dernière, ou pour relire une session dans son
déroulé.

Les captures portent désormais leur date, visible en infobulle sur leur nom.
Celles d'avant cette version n'en ont pas — l'infobulle le dit plutôt que
d'inventer une date.

Le tri chronologique ne s'appuie pas sur cette date, mais sur l'ordre du
fichier de captures, qui est l'ordre d'enregistrement. Il marche donc aussi sur
les captures d'avant, et une horloge qui saute ne le dérange pas.

## 0.4.0

**Déduction automatique des champs.** Bouton « Déduire les champs » : l'outil
retrouve seul quels bits portent quel paramètre, leur ordre (MSB/LSB) et leurs
bornes réelles — à partir des métadonnées que tu as déjà saisies.

Le principe : un bit appartient à `lum` s'il est fonction de `lum` **seul** —
même valeur, même bit, et il change quand `lum` change. Deux conséquences
gratuites et précieuses :

- **Le checksum se dénonce.** Il dépend de tous les champs à la fois, donc aucun
  paramètre isolé ne l'explique : il ressort dans les bits « inexpliqués ».
- **Les étiquetages fautifs sont détectés**, et l'outil nomme les deux captures
  qui se contredisent. C'est l'erreur la plus coûteuse du reverse, et la seule
  qu'on ne peut pas voir à l'œil.

Vérifié sur les 42 captures réelles : la carte de la RF00234, établie à la main
sur des heures, est retrouvée en une seconde — y compris l'erreur d'étiquetage
du ventilateur qui nous avait fait perdre du temps.

**L'IP du Broadlink est modifiable dans l'UI** avec annulation de la connexion.

## 0.3.0

**Profil d'appareil** — le livrable. Construire, télécharger (sauvegarde, partage),
enregistrer dans `/share/broadlink_lab/` où RF Bridge le détecte tout seul.

**Import d'un profil**, avec réancrage. Un profil mélange le savoir sur le *modèle*
(carte des bits, checksum, entités — partageable) et la capture de référence, qui
porte l'ID appairé d'*une* télécommande. À l'import, l'UI demande si c'est la tienne :
sinon elle garde tout sauf la référence, et tu captures une trame de ton exemplaire.
Le reverse de quelqu'un d'autre, récupéré en un appui de touche.

**L'IP du Broadlink se règle depuis l'UI**, et elle est persistée. Ces appareils sont
en DHCP : éditer les options de l'addon et le redémarrer à chaque bail renouvelé
n'avait pas de sens. Un bouton **Chercher** les trouve sur le réseau.

**La connexion s'annule.** Une mauvaise IP coûtait 18 s (3 essais × 6 s) sans moyen
d'abandonner — précisément le cas où on s'est trompé de chiffre.

**Rôle `const` et bornes par champ.** Un `const` n'est jamais réécrit : c'est là que
vivent le préambule et l'ID appairé, dont dépend l'acceptation de la trame. Les bornes
`min`/`max` évitent de proposer des états que la télécommande ne produit jamais.

**Checksum `sub8`** (`(k - somme) & 0xFF`) et sa détection automatique.

### Corrections

- Le téléchargement d'un profil pouvait produire un fichier vide ou partiel : le
  bouton était dans une ancre, du HTML invalide où le clic n'ouvre pas toujours le
  téléchargement. Et le blob était typé `text/plain` pour un `.json`.
- L'import échouait en accusant le fichier d'être un JSON invalide, alors que la
  lecture était en cause — le champ était vidé avant d'être lu. Les deux cas sont
  maintenant distingués, et l'erreur montre ce qui a été lu.
- `nuit` et `eco` étaient saisissables séparément alors qu'ils s'excluent dans la
  trame : un seul champ à trois valeurs.
- Course sur `/api/capture/start` : deux clics rapprochés pouvaient lancer deux
  captures concurrentes.

## 0.2.0

Séparation en deux addons : le labo (temporaire) et le pont (permanent). Accès à
`/share` pour l'échange de profils.

## 0.1.0

Capture directe via `python-broadlink`, grille de bits, nommage des champs au
clic-glisser, générateur, émission.
