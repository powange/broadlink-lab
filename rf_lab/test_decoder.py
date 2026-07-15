#!/usr/bin/env python3
"""Calibration decoder.py (CLAUDE.md §9) sans matériel : trame synthétique."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decoder

BITS = "101100101011000011110000"          # 24 bits de données
MARK_1, SPACE_1 = 1200, 400               # bit 1 -> (long, court)
MARK_0, SPACE_0 = 400, 1200               # bit 0 -> (court, long)
GAP = 8000                                # espace inter-trames
REPEATS = 3

durations = []
for _ in range(REPEATS):
    for b in BITS:
        durations += [MARK_1, SPACE_1] if b == "1" else [MARK_0, SPACE_0]
    durations += [400, GAP]               # paire de clôture de trame

ok = True

# 1. round-trip b64 : encode -> decode -> encode doit ressortir identique
pkt = decoder.encode_packet(durations, decoder.HEADER_RF433, REPEATS)
b64 = decoder.to_b64(pkt)
dec = decoder.decode_packet(b64)
b64_again = decoder.to_b64(
    decoder.encode_packet(dec["durations"], dec["header"], dec["repeats"]))
print(f"1. round-trip b64 identique      : {b64 == b64_again}")
print(f"   header=0x{dec['header']:02x} repeats={dec['repeats']} ndur={len(dec['durations'])}")
ok &= b64 == b64_again

# 2. les durées survivent au TICK
drift = max(abs(a - b) for a, b in zip(durations, dec["durations"]))
print(f"2. dérive max sur les durées     : {drift} µs")
ok &= drift <= 17            # <= une demi-unité de TICK (32.84/2)

# 3. décodage PWM — pas de bit fantôme, bits identiques à l'entrée
frames = decoder.decode_pwm(dec["durations"], gap=2000)
print(f"3. trames décodées               : {len(frames)} x {len(frames[0]['bits'])} bits"
      f"  (attendu {REPEATS} x {len(BITS)})")
print(f"   bits[0] = {frames[0]['bits']}")
print(f"   BITS    = {BITS}")
exact = all(f["bits"] == BITS for f in frames)
print(f"   décodage exact sur les {REPEATS} répétitions : {exact}")
ok &= len(frames) == REPEATS and exact

# 4. rebuild : on change un champ et on vérifie
new_bits = decoder.set_field(frames[0]["bits"], 4, 8, 0b1010, msb_first=True)
rebuilt = decoder.rebuild_frame(dec["durations"], frames, new_bits)
verified = decoder.verify_rebuild(rebuilt, new_bits, gap=2000)
redecoded = decoder.decode_pwm(rebuilt, gap=2000)
print(f"4. rebuild champ [4:8] -> 0b1010 : verified={verified}")
print(f"   avant  = {frames[0]['bits']}")
print(f"   après  = {redecoded[0]['bits']}")
print(f"   voulu  = {new_bits}")
same_len = len(rebuilt) == len(dec["durations"])
all_reps = all(f["bits"] == new_bits for f in redecoded)
print(f"   longueur préservée={same_len}  toutes répétitions patchées={all_reps}")
ok &= verified and same_len and all_reps

# 5. field_value round-trip
print(f"5. field_value([4:8])            : {decoder.field_value(redecoded[0]['bits'], 4, 8)} (attendu 10)")
ok &= decoder.field_value(redecoded[0]["bits"], 4, 8) == 10

# 6. verify_rebuild doit REJETER une trame tronquée (régression du faux positif)
truncated = decoder.verify_rebuild(rebuilt, new_bits + "1", gap=2000)
empty = decoder.verify_rebuild([], new_bits, gap=2000)
print(f"6. verify_rebuild rejette tronqué: {not truncated}   rejette vide: {not empty}")
ok &= (not truncated) and (not empty)

print("\n=>", "OK" if ok else "ÉCHEC")
sys.exit(0 if ok else 1)
