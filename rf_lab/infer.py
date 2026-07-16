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


def conflicts(rows, key, i, limit=2):
    """
    Deux captures de même valeur de `key` mais de bit différent : la preuve que
    `key` n'explique pas ce bit. Presque toujours un étiquetage faux — et c'est
    l'information la plus utile qu'on puisse rendre, parce qu'elle est invisible
    à l'œil.
    """
    by = {}
    out = []
    for r in rows:
        v = r["meta"].get(key)
        if v is None:
            continue
        k = _key(v)
        if k in by and by[k][1] != r["bits"][i]:
            out.append({"value": k, "a": by[k][0], "a_bit": by[k][1],
                        "b": r["name"], "b_bit": r["bits"][i]})
            if len(out) >= limit:
                return out
        else:
            by.setdefault(k, (r["name"], r["bits"][i]))
    return out


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

    # Un paramètre qui bouge mais dont AUCUN bit n'est fonction : soit il n'est
    # pas dans la trame, soit — bien plus souvent — une capture est mal étiquetée.
    # C'est l'erreur la plus coûteuse du reverse, et la plus invisible à l'œil.
    problems = []
    for key in keys:
        vals = {_key(r["meta"][key]) for r in rows if r["meta"].get(key) is not None}
        if len(vals) < 2 or any(f["name"] == key for f in fields):
            continue
        ex = next((c for i in orphans for c in [conflicts(rows, key, i)] if c), None)
        problems.append({
            "param": key, "distinct_values": len(vals),
            "conflicts": ex or [],
            "reason": ("deux captures de même « %s » portent des bits différents : "
                       "l'une des deux est mal étiquetée" % key) if ex else
                      ("« %s » varie mais aucun bit ne le suit : absent de la trame ?" % key),
        })

    return {
        "fields": sorted(fields, key=lambda f: f["start"]),
        "unexplained": [{"start": a, "end": b} for a, b in unexplained],
        "problems": problems,
        "varying": varying,
        "captures": len(rows),
        "frame_bits": n,
    }
