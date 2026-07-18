"""
Décodage / encodage / reconstruction de trames RF Broadlink (RM4 Pro).

Principe central de la génération (rebuild_frame) :
  on ne reconstruit JAMAIS une trame à partir de zéro. On part d'une capture
  réelle et on inverse les paires (mark, space) des seuls bits à changer.
  En PWM, bit=1 -> (long, court) et bit=0 -> (court, long) : swapper les deux
  durées inverse le bit tout en préservant à l'identique le préambule, les gaps,
  les répétitions et le timing exact de la télécommande d'origine.
"""

import base64

TICK = 269 / 8.192  # ~32.84 µs par unité Broadlink

HEADER_IR = 0x26
HEADER_RF433 = 0xB2
HEADER_RF315 = 0xD7


# ------------------------------------------------------------ packet <-> durées

def decode_packet(data):
    """
    bytes|b64 Broadlink -> dict(header, repeats, durations[µs], terminator)

    `terminator` dit si le paquet portait le marqueur de fin 0x0d 0x05. Tous n'en
    ont pas : les captures brutes d'un RM4 Pro (check_data) s'arrêtent sur leur
    dernier gap, sans marqueur. Il faut le savoir pour ré-encoder à l'identique —
    en rajouter un absent ajouterait une impulsion parasite (13 et 5 ticks, soit
    ~427 et ~164 µs) à la fin de la trame émise.
    """
    if isinstance(data, str):
        data = base64.b64decode(data)

    header, repeats = data[0], data[1]
    length = int.from_bytes(data[2:4], "little")
    payload = data[4:4 + length]
    terminator = payload.endswith(b"\x0d\x05")
    if terminator:
        payload = payload[:-2]

    durations, i = [], 0
    while i < len(payload):
        v = payload[i]
        if v == 0:
            if i + 3 > len(payload):
                break
            v = int.from_bytes(payload[i + 1:i + 3], "big")
            i += 3
        else:
            i += 1
        durations.append(round(v * TICK))

    return {"header": header, "repeats": repeats, "durations": durations,
            "terminator": terminator}


def encode_packet(durations, header=HEADER_RF433, repeats=0, terminator=True):
    """
    [durées µs] -> bytes Broadlink prêts pour send_data().

    `terminator` doit refléter le paquet d'origine (cf. decode_packet), sinon le
    round-trip ne boucle pas — et le round-trip est la calibration du projet (§4).
    """
    body = bytearray()
    for us in durations:
        v = int(round(us / TICK))
        v = max(1, min(v, 0xFFFF))
        if v < 256:
            body.append(v)
        else:
            body += b"\x00" + v.to_bytes(2, "big")
    if terminator:
        body += b"\x0d\x05"
    return bytes([header, repeats]) + len(body).to_bytes(2, "little") + bytes(body)


def to_b64(data):
    return base64.b64encode(data).decode()


# ------------------------------------------------------------ durées -> bits

def _split_frames(durations, gap):
    """
    Découpe la liste de durées en trames sur les espaces > gap.

    La paire (mark, gap) qui clôt une trame n'est pas décodable : son space est
    l'espace inter-trames, donc `mark > space` répond toujours 0 quelle que soit
    la valeur réelle. On l'exclut plutôt que de produire un bit fantôme — et
    surtout pour que rebuild_frame ne puisse jamais swapper un mark avec le gap,
    ce qui détruirait le timing de la trame.
    """
    frames, current = [], []
    i = 0
    while i + 1 < len(durations):
        space = durations[i + 1]
        if space > gap:
            if len(current) > 8:
                frames.append(current)
            current = []
        else:
            current.append(i)
        i += 2
    if len(current) > 8:
        frames.append(current)
    return frames


def tail_pulse(durations, idx, gap):
    """
    L'impulsion qui suit le dernier bit décodé — celle dont l'espace est le gap.

    Deux lectures possibles, et rien dans UNE trame ne permet de trancher :

      - une impulsion de TERMINAISON, qui ne porte pas de donnée. C'est le cas de
        la RF00234 : l'exclure donne 64 bits = 8 octets, et le checksum par octets
        le confirme.
      - le DERNIER BIT de données, dont l'espace a fusionné avec le gap. Là,
        l'exclure fait perdre un bit — une trame de 48 bits se lit 47.

    D'où ce diagnostic : on rend la durée du mark et ce que vaudrait le bit, en
    comparant le mark aux AUTRES marks de la trame (pas au gap, qui écrase tout).
    C'est à l'utilisateur de trancher — la longueur de trame l'y aide : 47 bits
    n'est ni un multiple de 8 ni de 4, 48 si.
    """
    if not idx:
        return None
    j = idx[-1] + 2
    if j + 1 >= len(durations) or durations[j + 1] <= gap:
        return None
    marks = sorted(durations[i] for i in idx)
    if not marks:
        return None
    lo, hi = marks[0], marks[-1]
    mark = durations[j]
    # plus proche du mark long ou du mark court ?
    bit = "1" if abs(mark - hi) < abs(mark - lo) else "0"
    return {"index": j, "mark": mark, "space": durations[j + 1],
            "short": lo, "long": hi, "bit": bit,
            "looks_like_data": abs(mark - hi) < abs(mark - lo)}


def decode_pwm(durations, gap=2000, tail=False):
    """
    Décodage PWM : bit = 1 si mark > space.

    Retourne [{bits, idx, tail}] — idx[j] = index de la durée 'mark' du bit j
    (indispensable au rebuild).

    `tail=True` inclut l'impulsion de clôture comme dernier bit, en décidant sa
    valeur par comparaison aux autres marks. À n'activer que si la longueur de
    trame le réclame (cf. tail_pulse).
    """
    out = []
    for idx in _split_frames(durations, gap):
        bits = "".join(
            "1" if durations[i] > durations[i + 1] else "0" for i in idx
        )
        t = tail_pulse(durations, idx, gap)
        if tail and t:
            bits += t["bit"]
            idx = idx + [t["index"]]
        out.append({"bits": bits, "idx": idx, "tail": t})
    return out


def pick_frame(frames):
    """
    Trame représentative d'une capture : la PLUS FRÉQUENTE, pas la première.

    Une télécommande répète sa trame (6× sur la RF00234). La première répétition
    est celle qui porte le bruit : le RM4 commence à enregistrer avant que l'AGC
    ne se cale, et on récupère des impulsions parasites en tête. Observé sur une
    vraie capture : répétition #0 = 72 bits (8 zéros parasites devant), #1 à #5 =
    64 bits identiques.

    Prendre frames[0] revient donc à comparer des trames décalées entre captures
    — le diff devient du bruit et chaque colonne semble varier.
    """
    if not frames:
        return ""
    counts = {}
    for f in frames:
        counts[f["bits"]] = counts.get(f["bits"], 0) + 1
    # à égalité, la plus longue série gagne ; puis la plus courte (moins de bruit)
    return max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


def decode_manchester(durations, gap=2000, half=None):
    """
    Décodage Manchester (fallback si le PWM sort du bruit).
    Estime la demi-période sur la durée la plus fréquente si non fournie.
    """
    body = [d for d in durations if d < gap]
    if not body:
        return []
    if half is None:
        half = sorted(body)[len(body) // 4]

    levels, level = [], 1
    for d in durations:
        if d > gap:
            break
        n = max(1, round(d / half))
        levels.extend([level] * n)
        level ^= 1

    bits = ""
    for i in range(0, len(levels) - 1, 2):
        a, b = levels[i], levels[i + 1]
        if a == b:
            continue
        bits += "1" if (a == 0 and b == 1) else "0"
    return [{"bits": bits, "idx": [], "half": round(half)}] if bits else []


# ------------------------------------------------------------ analyse

def analyze(frames):
    """
    frames = {nom: 'bitstring'}
    Retourne les colonnes qui varient + la longueur commune.
    """
    if not frames:
        return {"length": 0, "varying": [], "truncated": False}

    lengths = {len(b) for b in frames.values()}
    n = min(lengths)
    varying = [
        i for i in range(n)
        if len({b[i] for b in frames.values()}) > 1
    ]
    return {
        "length": n,
        "varying": varying,
        "truncated": len(lengths) > 1,
        "lengths": sorted(lengths),
    }


def field_value(bits, start, end, msb_first=True):
    """Valeur entière d'une tranche de bits [start, end)."""
    chunk = bits[start:end]
    if not chunk:
        return 0
    if not msb_first:
        chunk = chunk[::-1]
    return int(chunk, 2)


def set_field(bits, start, end, value, msb_first=True):
    """Écrit value dans la tranche [start, end) et retourne la nouvelle bitstring."""
    # Une tranche hors de la trame écrivait à côté et pouvait DOUBLER la taille
    # de la bitstring silencieusement (start > len, ou start négatif). On refuse
    # plutôt que de corrompre : le validate du profil est censé l'éviter en amont,
    # ce garde est le filet.
    if not 0 <= start < end <= len(bits):
        raise ValueError(f"tranche [{start}, {end}) hors de la trame de {len(bits)} bits")
    width = end - start
    chunk = format(value & ((1 << width) - 1), f"0{width}b")
    if not msb_first:
        chunk = chunk[::-1]
    return bits[:start] + chunk + bits[end:]


# ------------------------------------------------------------ checksums

def _to_bytes(bits):
    pad = (-len(bits)) % 8
    padded = bits + "0" * pad
    return [int(padded[i:i + 8], 2) for i in range(0, len(padded), 8)]


CHECKSUM_KINDS = ("none", "sum8", "xor8", "sub8")


def compute_checksum(bits, kind, crc_start, crc_end, k=0):
    """
    Recalcule un checksum sur tous les bits SAUF la tranche [crc_start, crc_end).

    kind:
      sum8  somme des octets & 0xFF
      xor8  ou-exclusif des octets
      sub8  (k - somme) & 0xFF — c'est celui de la RF00234, avec k=0x55.
            Autrement dit (somme + checksum) & 0xFF vaut toujours k.

    ATTENTION : `payload` est repaddé à l'octet. Si la tranche CRC n'est pas
    alignée sur un octet, la retirer décale tout ce qui suit et le résultat ne
    veut rien dire. Sur la RF00234 le CRC occupe les bits [56, 64) et il n'y a
    rien après, donc le cas est sain — mais un autre protocole demanderait de
    revoir ça (cf. §10).
    """
    if kind in (None, "none"):
        return None
    payload = bits[:crc_start] + bits[crc_end:]
    data = _to_bytes(payload)
    if kind == "xor8":
        acc = 0
        for b in data:
            acc ^= b
        return acc
    if kind == "sum8":
        return sum(data) & 0xFF
    if kind == "sub8":
        return (k - sum(data)) & 0xFF
    raise ValueError(f"checksum inconnu: {kind}")


def detect_checksum(samples, crc_start, crc_end):
    """
    Cherche le checksum qui explique TOUS les échantillons.

    samples : liste de bitstrings (une par capture, même longueur)
    Retourne {kind, k, confidence} ou {kind: 'none'} si rien ne colle.

    `confidence` = nombre d'échantillons distincts qui contraignent le résultat.
    En dessous de 3, une correspondance peut être fortuite : sub8 a 256 valeurs
    de k possibles, donc 2 échantillons suffisent rarement à trancher.
    """
    uniq = sorted(set(s for s in samples if len(s) >= crc_end))
    if len(uniq) < 2:
        return {"kind": "none", "k": 0, "confidence": len(uniq)}

    def crc_of(b):
        return field_value(b, crc_start, crc_end)

    for kind in ("sum8", "xor8"):
        if all(compute_checksum(b, kind, crc_start, crc_end) == crc_of(b) for b in uniq):
            return {"kind": kind, "k": 0, "confidence": len(uniq)}

    # sub8 : k est déduit du 1er échantillon, puis vérifié sur les autres
    b0 = uniq[0]
    data0 = _to_bytes(b0[:crc_start] + b0[crc_end:])
    k = (crc_of(b0) + sum(data0)) & 0xFF
    if all(compute_checksum(b, "sub8", crc_start, crc_end, k) == crc_of(b) for b in uniq):
        return {"kind": "sub8", "k": k, "confidence": len(uniq)}

    return {"kind": "none", "k": 0, "confidence": len(uniq)}


# ------------------------------------------------------------ rebuild

def rebuild_frame(durations, decoded_frames, new_bits, ref_bits=None):
    """
    LE cœur de l'outil.

    durations       : durées de la capture de référence (modifiées en place sur copie)
    decoded_frames  : sortie de decode_pwm() sur cette capture
    new_bits        : bitstring cible (même longueur que ref_bits)
    ref_bits        : trame de référence ; par défaut la plus fréquente (pick_frame)

    Pour chaque bit qui diffère, on swappe (mark, space) — ce qui inverse le bit
    en PWM sans toucher au reste. Appliqué à TOUTES les répétitions.

    Chaque répétition est réalignée sur ref_bits avant patch : une répétition
    bruitée porte des bits parasites en tête (cf. pick_frame), et patcher à
    l'index brut y écrirait à côté. Une répétition où ref_bits est introuvable
    est laissée intacte — verify_rebuild s'en apercevra et refusera la trame.
    """
    out = list(durations)
    if ref_bits is None:
        ref_bits = pick_frame(decoded_frames)
    n = min(len(ref_bits), len(new_bits))

    for frame in decoded_frames:
        bits, idx = frame["bits"], frame["idx"]
        if not idx:
            raise ValueError("rebuild impossible : pas d'index (décodage Manchester ?)")
        off = bits.find(ref_bits)
        if off < 0:
            continue
        for j in range(n):
            if ref_bits[j] != new_bits[j]:
                i = idx[off + j]
                out[i], out[i + 1] = out[i + 1], out[i]

    return out


def verify_rebuild(durations, new_bits, gap=2000):
    """
    Re-décode une trame reconstruite pour vérifier qu'elle porte bien new_bits.

    Deux exigences, et les deux comptent :
      - la trame majoritaire vaut EXACTEMENT new_bits (pas un préfixe : une
        comparaison laxiste validerait une trame tronquée) ;
      - AUCUNE répétition ne porte encore l'ancien état — sinon on émettrait
        des trames contradictoires, dont le récepteur ferait n'importe quoi.
        Une répétition bruitée est tolérée tant qu'elle contient new_bits.
    """
    frames = decode_pwm(durations, gap)
    if not frames:
        return False
    if pick_frame(frames) != new_bits:
        return False
    return all(new_bits in f["bits"] for f in frames)
