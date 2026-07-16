#!/usr/bin/env python3
"""
Déduire la carte des champs depuis les captures et leurs métadonnées.

C'est le travail qu'on fait à l'œil dans la grille — repérer les colonnes qui
bougent avec la luminosité, deviner l'ordre des bits, lire la correspondance —
sauf que la machine ne se fatigue pas et ne saute pas de ligne.

LE PRINCIPE

Un bit appartient au champ `lum` s'il est une **fonction de `lum` seul** :

  - déterminisme : deux captures de même `lum` ont le même bit. Sinon le bit
    dépend d'autre chose, et l'attribuer à `lum` serait faux.
  - information : le bit prend des valeurs différentes selon `lum`. Sinon c'est
    un bit constant, il n'appartient à personne.

Ce test a une propriété précieuse : **le checksum échoue partout**. Il dépend de
tous les champs à la fois, donc aucun paramètre pris isolément ne l'explique. Les
bits qui varient sans être expliqués sont donc les candidats CRC — et c'est le
piège n°1 du projet.

QUAND UN PARAMÈTRE N'EST EXPLIQUÉ PAR AUCUN BIT

Trois causes, qui appellent des gestes opposés — d'où l'importance de ne pas
sauter à la conclusion « c'est mal étiqueté » :

  - un CHAMP PARTAGÉ. Deux paramètres se partagent une tranche, et aucun ne
    l'explique seul. Vu en vrai sur une Mantra RF00143 : le ventilateur n'y a pas
    de bit d'alimentation, « éteint » s'y écrit « vitesse 0 ».
  - un ÉTIQUETAGE FAUTIF. Vu en vrai sur la RF00234 : deux captures dites `fan1`
    alors que le ventilo était à l'arrêt.
  - le paramètre est ABSENT de la trame.

ET ON NE PEUT PAS LES DÉPARTAGER PAR CORRÉLATION

Une version de ce fichier cherchait le champ partagé en testant les COUPLES de
paramètres. Vérifié sur les deux protocoles réels, ça se trompe dans les deux
sens : sur la RF00234, le couple (fan, lum) « explique » le bit 40 exactement
comme un champ partagé — alors que `lum` n'y est pour rien, la série de
luminosité ayant simplement été capturée ventilo à l'arrêt. Les deux situations
ont la MÊME signature formelle. En tolérant quelques exceptions pour rattraper le
cas RF00143, ça allait jusqu'à présenter deux bits du timer comme un champ
partagé avec `lum`.

D'où le choix inverse : ne pas deviner, et rendre à la place la preuve la plus
solide qui soit — `isolated_pairs`, deux captures qui ne diffèrent QUE par le
paramètre en cause. Aucune heuristique, et c'est exactement « ne fais varier
qu'un seul paramètre à la fois », la méthode du projet. Sur les deux protocoles,
elle désigne les bons bits du premier coup.

CE QUI N'EST PAS UNE PREUVE

« Deux captures de même `fan` portent des bits différents » ne prouve rien : sur
un bit de checksum ou de commande, c'est vrai de n'importe quel paramètre et de
n'importe quelle paire de captures, puisque ces bits dépendent de tout. Une
version antérieure accusait l'utilisateur sur cette base.

Même les captures aux métadonnées RIGOUREUSEMENT identiques peuvent légitimement
différer : sur la RF00234, les bits 26-31 décrivent le bouton pressé, pas l'état
obtenu. `contradictions` les signale sans trancher.

CE QUE ÇA NE PEUT PAS FAIRE

Si deux paramètres ne varient jamais indépendamment dans le jeu de captures, rien
ne les distingue : leurs bits sont fonction de l'un comme de l'autre. La méthode
le dit (`ambigu`) plutôt que de trancher au hasard. C'est la traduction formelle
de « ne fais varier qu'un seul paramètre à la fois ».
"""


def _key(v):
    """Valeur de métadonnée -> clé hashable et ordonnable."""
    if isinstance(v, bool):
        return int(v)
    return v


def _value(bits, start, end, msb_first=True):
    chunk = bits[start:end]
    if not chunk:
        return 0
    if not msb_first:
        chunk = chunk[::-1]
    return int(chunk, 2)


def analyse_bit(rows, key, i):
    """
    Le bit `i` est-il une fonction du paramètre `key` seul ?

    Retourne (fonctionnel, informatif, {valeur_param: bit}).
    """
    groups = {}
    for r in rows:
        v = r["meta"].get(key)
        if v is None:
            continue                     # capture antérieure à ce paramètre
        groups.setdefault(_key(v), set()).add(r["bits"][i])
    if not groups:
        return False, False, {}
    functional = all(len(s) == 1 for s in groups.values())
    mapping = {k: next(iter(s)) for k, s in groups.items() if len(s) == 1}
    informative = len(set(mapping.values())) > 1
    return functional, informative, mapping


def _runs(bits):
    """[36,37,38,39,44] -> [(36,40), (44,45)]"""
    out = []
    for i in sorted(bits):
        if out and i == out[-1][1]:
            out[-1] = (out[-1][0], i + 1)
        else:
            out.append((i, i + 1))
    return out


def _orientation(rows, key, start, end):
    """
    MSB ou LSB ? On tranche par la monotonie : trié par la grandeur physique, le
    champ doit croître. C'est ce qui distingue `lum10 -> 2` d'une valeur qui saute
    dans tous les sens.

    Retourne (msb_first, mapping, monotone).
    """
    obs = {}
    for r in rows:
        v = r["meta"].get(key)
        if v is None:
            continue
        obs.setdefault(_key(v), (_value(r["bits"], start, end, True),
                                 _value(r["bits"], start, end, False)))
    if not obs:
        return True, {}, False

    try:
        ordered = sorted(obs)
    except TypeError:                    # valeurs non ordonnables (enum textuel)
        return True, {k: v[0] for k, v in obs.items()}, False

    def monotone(vals):
        return all(b > a for a, b in zip(vals, vals[1:])) and len(set(vals)) == len(vals)

    msb = [obs[k][0] for k in ordered]
    lsb = [obs[k][1] for k in ordered]
    if monotone(msb):
        return True, {k: obs[k][0] for k in ordered}, True
    if monotone(lsb):
        return False, {k: obs[k][1] for k in ordered}, True
    # ni l'un ni l'autre : on garde MSB par défaut et on le signale
    return True, {k: obs[k][0] for k in ordered}, False


def isolated_pairs(rows, keys, key, limit=3):
    """
    Deux captures qui ne diffèrent QUE par `key`. Les bits qui changent entre
    elles sont, forcément, ceux que `key` touche.

    C'est la preuve la plus solide qu'on puisse produire, et elle ne repose sur
    aucune heuristique : c'est « ne fais varier qu'un seul paramètre à la fois »,
    la méthode même du projet, appliquée par la machine.

    Y figurent aussi le checksum et l'octet de commande — ils changent dès que
    quoi que ce soit change. Le dire, plutôt que de tenter de les retrancher :
    à ce stade on ne sait pas encore où ils sont.
    """
    out = []
    others = [k for k in keys if k != key]
    for n, a in enumerate(rows):
        for b in rows[n + 1:]:
            if a["meta"].get(key) == b["meta"].get(key):
                continue
            if any(a["meta"].get(k) != b["meta"].get(k) for k in others):
                continue
            d = [i for i in range(min(len(a["bits"]), len(b["bits"])))
                 if a["bits"][i] != b["bits"][i]]
            out.append({"a": a["name"], "b": b["name"], "bits": d})
            if len(out) >= limit:
                return out
    return out


def near_miss(rows, key, i):
    """
    Combien de captures faudrait-il ré-étiqueter pour que le bit `i` devienne une
    fonction de `key` ? Et lesquelles ?

    C'est la seule façon HONNÊTE de désigner un étiquetage douteux. Chercher deux
    captures de même `key` aux bits différents n'en est pas une : sur un bit de
    checksum ou de commande, ça réussit pour n'importe quel paramètre et n'importe
    quelle paire de captures, donc ça ne prouve rien.

    C'est un INDICE, pas un verdict : un étiquetage fautif et un champ partagé
    produisent tous deux des exceptions ici. D'où le ton du message.
    """
    groups = {}
    for r in rows:
        v = r["meta"].get(key)
        if v is None:
            continue
        groups.setdefault(_key(v), []).append(r)

    outliers, majority = [], {}
    for v, rs in groups.items():
        counts = {}
        for r in rs:
            counts.setdefault(r["bits"][i], []).append(r)
        best = max(counts.values(), key=len)
        majority[v] = best[0]["bits"][i]
        for lst in counts.values():
            if lst is not best:
                outliers.extend(lst)
    # sans information, le bit ne distingue rien : ce n'est pas un candidat
    if len(set(majority.values())) < 2:
        return None
    return {"bit": i, "outliers": outliers, "mapping": majority}


def contradictions(rows, keys, limit=3):
    """
    Deux captures aux métadonnées RIGOUREUSEMENT identiques, mais aux bits
    différents.

    Attention : ce n'est PAS une preuve d'étiquetage fautif. Sur la RF00234, des
    captures d'état identique diffèrent aux bits 26-31 — ces bits décrivent le
    BOUTON pressé, pas l'état obtenu, et le récepteur les ignore. Le doute est
    donc réel, et c'est à l'humain de trancher.
    """
    seen, out = {}, []
    for r in rows:
        k = tuple(_key(r["meta"].get(x)) for x in keys)
        if k in seen and seen[k]["bits"] != r["bits"]:
            d = [i for i in range(min(len(r["bits"]), len(seen[k]["bits"])))
                 if seen[k]["bits"][i] != r["bits"][i]]
            out.append({"a": seen[k]["name"], "b": r["name"], "bits": d})
            if len(out) >= limit:
                return out
        seen.setdefault(k, r)
    return out


def _isolated_clue(key, pairs):
    bits = sorted({i for p in pairs for i in p["bits"]})
    return {
        "kind": "isolated",
        "text": ("CONTRÔLE — « {a} » et « {b} » ne diffèrent QUE par « {k} ». Les bits "
                 "qui changent : {bits}. « {k} » est forcément parmi eux. Le checksum "
                 "et l'octet de commande y sont aussi (ils changent dès que quoi que "
                 "ce soit change) : écarte-les à l'œil dans la grille, il reste la "
                 "place de « {k} »."
                 ).format(k=key, a=pairs[0]["a"], b=pairs[0]["b"],
                          bits=", ".join(str(b) for b in bits)),
        "pairs": pairs,
    }


def _suspect_clue(key, best, total):
    bad = len(best["outliers"])
    return {
        "kind": "mislabel",
        "text": ("ÉTIQUETAGE — le bit {i} suit « {k} » pour {ok} captures sur {tot}. "
                 "Si les {n} qui le contredisent étaient mal étiquetées, tout "
                 "s'expliquerait. À vérifier, sans présumer : ce sont des candidates, "
                 "pas des coupables."
                 ).format(i=best["bit"], k=key, n=bad, tot=total, ok=total - bad),
        "suspects": [r["name"] for r in best["outliers"]],
    }


def suggest(rows, keys, min_values=2):
    """
    rows : [{name, bits, meta}] — les captures décodées
    keys : les paramètres à expliquer (les clés du meta_schema)

    Retourne {fields: [...], unexplained: [...], varying: [...]}.
    """
    rows = [r for r in rows if r.get("bits")]
    if len(rows) < 2:
        return {"fields": [], "unexplained": [], "varying": [],
                "note": "il faut au moins 2 captures"}

    n = min(len(r["bits"]) for r in rows)
    varying = [i for i in range(n) if len({r["bits"][i] for r in rows}) > 1]

    # quel(s) paramètre(s) expliquent chaque bit qui varie ?
    owners = {}
    for i in varying:
        owners[i] = [k for k in keys
                     if all(analyse_bit(rows, k, i)[:2])]

    fields, seen = [], set()
    for key in keys:
        mine = [i for i in varying if key in owners[i]]
        if not mine:
            continue
        distinct = len({_key(r["meta"][key]) for r in rows
                        if r["meta"].get(key) is not None})
        for start, end in _runs(mine):
            msb, mapping, monotone = _orientation(rows, key, start, end)
            # un bit expliqué par plusieurs paramètres est ambigu : ils n'ont
            # jamais varié indépendamment dans ce jeu de captures
            ambiguous = sorted({k for i in range(start, end)
                                for k in owners[i] if k != key})
            vals = [v for v in mapping.values()]
            fields.append({
                "name": key, "start": start, "end": end, "msb_first": msb,
                "role": "data",
                "min": min(vals) if vals else 0,
                "max": max(vals) if vals else 0,
                "mapping": {str(k): v for k, v in mapping.items()},
                "monotone": monotone,
                "distinct_values": distinct,
                "confident": distinct >= min_values and not ambiguous,
                "ambiguous_with": ambiguous,
            })
            seen.update(range(start, end))

    # Les bits qui varient sans qu'aucun paramètre ne les explique. Le checksum
    # est là — il dépend de tous les champs à la fois, donc d'aucun isolément.
    orphans = [i for i in varying if i not in seen]
    unexplained = _runs(orphans)

    # Un paramètre qui bouge mais dont aucun bit n'est fonction. Trois causes
    # possibles, et il faut les distinguer plutôt que d'accuser l'étiquetage :
    # elles appellent des gestes opposés.
    problems = []
    for key in keys:
        vals = {_key(r["meta"][key]) for r in rows if r["meta"].get(key) is not None}
        if len(vals) < 2 or any(f["name"] == key for f in fields):
            continue

        # 1. LA preuve : deux captures qui ne diffèrent que par `key`.
        pairs = isolated_pairs(rows, keys, key)

        # 2. l'indice : le bit qui expliquerait `key` si quelques captures étaient
        #    ré-étiquetées. Ce sont elles les candidates — pas un bit au hasard.
        cands = [c for c in (near_miss(rows, key, i) for i in varying) if c]
        best = min(cands, key=lambda c: (len(c["outliers"]), c["bit"]), default=None)

        # Le contrôle d'abord : c'est une preuve, pas une conjecture. L'indice
        # ensuite, et il reste un indice — un étiquetage fautif et un champ
        # partagé le produisent tous les deux.
        clues = []
        if pairs:
            clues.append(_isolated_clue(key, pairs))
        if best:
            clues.append(_suspect_clue(key, best, len(rows)))

        problems.append({
            "param": key, "distinct_values": len(vals),
            "isolated": pairs,
            "suspects": [r["name"] for r in best["outliers"]] if best else [],
            "near_bit": best["bit"] if best else None,
            "clues": clues,
            "reason": ("« %s » varie, mais aucun bit n'est fonction de « %s » seul. "
                       "Ça n'accuse pas ton étiquetage : un champ partagé avec un "
                       "autre paramètre (« éteint » écrit « niveau 0 ») donne la même "
                       "chose. Les indices :" % (key, key))
                      if clues else
                      ("« %s » varie mais aucun bit ne le suit : absent de la trame ?" % key),
        })

    return {
        "fields": sorted(fields, key=lambda f: f["start"]),
        "unexplained": [{"start": a, "end": b} for a, b in unexplained],
        "problems": problems,
        "contradictions": contradictions(rows, keys),
        "varying": varying,
        "captures": len(rows),
        "frame_bits": n,
    }
