# Journal des modifications

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
