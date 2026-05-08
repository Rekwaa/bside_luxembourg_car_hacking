#!/usr/bin/env python3
"""
fabia_cluster.py v5 - Pilote le cluster Skoda Fabia 6J (PQ25)

Usage:
    sudo python3 fabia_cluster.py <commande>

Commandes:
    listen
    clean               # Voiture saine
    rpm <n>
    speed <kmh>
    blink <l|r|h>
    full <kmh> <rpm>
    sweep               # 0 -> 200 -> 0 km/h en boucle
    dance               # Les aiguilles dancent gauche-droite en synchro !
    dance opposite      # Aiguilles en opposition (une monte, l'autre descend)
    dance chaos         # Les aiguilles partent en n'importe quoi
    raw <ID> <hex>
    hunt <ID>
    probe5a0 <hex16>
"""
import can
import sys
import time
import math
import threading

BUS = "can0"
BLINK_PERIOD = 1.0
SEND_PERIOD_5A0 = 0.010

state = {
    "rpm": 0,
    "speed": 0,
    "blink": None,
    "running": True,
    "clean_mode": False,
    "raw_msgs": {},
    "odo": 0,
}

bus = None


def send(arb_id, data):
    msg = can.Message(arbitration_id=arb_id, data=list(data), is_extended_id=False)
    try:
        bus.send(msg)
    except can.CanError as e:
        print(f"[!] send error on {arb_id:03X}: {e}")


# --- Threads cycliques de base ---

def thread_immo():
    while state["running"]:
        send(0x3D0, [0x00] * 8)
        time.sleep(0.1)


def thread_rpm():
    while state["running"]:
        rpm_val = state["rpm"] * 4
        data = [0x49, 0x0E, rpm_val & 0xFF, (rpm_val >> 8) & 0xFF, 0x0E, 0x00, 0x1B, 0x0E]
        send(0x280, data)
        time.sleep(0.02)


def thread_abs():
    while state["running"]:
        v = state["speed"] * 100
        data = [0x04, 0x00, v & 0xFF, (v >> 8) & 0xFF, 0xFE, 0xFE, 0x00, 0x00]
        send(0x1A0, data)
        time.sleep(0.02)


def thread_speed_5a0():
    while state["running"]:
        kmh = max(0, state["speed"])
        increment = int(kmh / 3.6 * 50 * SEND_PERIOD_5A0)
        state["odo"] = (state["odo"] + increment) & 0xFFFF
        v_encoded = (kmh * 75) << 1
        v_lsb = v_encoded & 0xFF
        v_msb = (v_encoded >> 8) & 0xFF
        odo_lsb = state["odo"] & 0xFF
        odo_msb = (state["odo"] >> 8) & 0xFF
        data = [0x84, v_lsb, v_msb, 0x00, 0x00, odo_lsb, odo_msb, 0xAD]
        send(0x5A0, data)
        time.sleep(SEND_PERIOD_5A0)


def thread_wheels():
    while state["running"]:
        v = state["speed"] * 100
        lsb = v & 0xFF
        msb = (v >> 8) & 0xFF
        data = [lsb, msb, lsb, msb, lsb, msb, lsb, msb]
        send(0x4A0, data)
        time.sleep(0.02)


def thread_brake_4a8():
    while state["running"]:
        send(0x4A8, [0x00] * 8)
        time.sleep(0.05)


def thread_blink():
    on_payloads = {
        "l":      [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
        "left":   [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
        "r":      [0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
        "right":  [0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
        "h":      [0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
        "hazard": [0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    }
    off_payload = [0x00] * 8
    cycle_start = time.monotonic()
    while state["running"]:
        elapsed = time.monotonic() - cycle_start
        is_on_phase = (elapsed % (2 * BLINK_PERIOD)) < BLINK_PERIOD
        if state["blink"] and is_on_phase:
            data = on_payloads.get(state["blink"], off_payload)
        else:
            data = off_payload
        send(0x470, data)
        time.sleep(0.1)


def thread_airbag():
    while state["running"]:
        if state["clean_mode"]:
            send(0x050, [0x00] * 8)
        time.sleep(0.1)


def thread_coolant():
    while state["running"]:
        if state["clean_mode"]:
            send(0x288, [0x00, 0x9A, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        time.sleep(0.1)


def thread_engine():
    while state["running"]:
        if state["clean_mode"]:
            send(0x480, [0x00] * 8)
        time.sleep(0.1)


def thread_raw():
    while state["running"]:
        for arb_id, data in list(state["raw_msgs"].items()):
            send(arb_id, data)
        time.sleep(0.05)


def start_threads(extras=()):
    base = [
        thread_immo, thread_rpm, thread_abs, thread_speed_5a0,
        thread_wheels, thread_brake_4a8, thread_blink,
        thread_airbag, thread_coolant, thread_engine, thread_raw,
    ]
    threads = [threading.Thread(target=t, daemon=True) for t in base]
    for t in extras:
        threads.append(threading.Thread(target=t, daemon=True))
    for t in threads:
        t.start()
    return threads


# --- Commandes ---

def cmd_listen():
    print(f"[*] Ecoute sur {BUS}")
    for msg in bus:
        data_hex = " ".join(f"{b:02X}" for b in msg.data)
        print(f"  {msg.timestamp:.3f}  {msg.arbitration_id:03X}  [{msg.dlc}]  {data_hex}")


def cmd_clean():
    print("[*] CLEAN mode")
    state["clean_mode"] = True
    state["rpm"] = 800
    start_threads()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        state["running"] = False


def cmd_full(speed, rpm):
    print(f"[*] {speed} km/h, {rpm} RPM")
    state["clean_mode"] = True
    state["speed"] = speed
    state["rpm"] = rpm
    start_threads()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        state["running"] = False


def cmd_blink(direction):
    labels = {"l": "GAUCHE", "left": "GAUCHE",
              "r": "DROITE", "right": "DROITE",
              "h": "HAZARD", "hazard": "HAZARD"}
    label = labels.get(direction)
    if not label:
        print(f"[!] Direction inconnue: {direction}.")
        return
    print(f"[*] Clignotant: {label} - cycle {BLINK_PERIOD}s ON / {BLINK_PERIOD}s OFF")
    state["clean_mode"] = True
    state["blink"] = direction
    start_threads()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        state["running"] = False


def cmd_sweep():
    print("[*] Sweep continu 0 -> 200 -> 0 km/h (Ctrl+C pour stop)")
    state["clean_mode"] = True
    state["rpm"] = 1500

    def sweep_loop():
        while state["running"]:
            for v in range(0, 201):
                if not state["running"]:
                    return
                state["speed"] = v
                time.sleep(0.01)
            for v in range(200, -1, -1):
                if not state["running"]:
                    return
                state["speed"] = v
                time.sleep(0.01)

    start_threads(extras=(sweep_loop,))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Stop demande")
        state["running"] = False


def cmd_dance(mode="sync"):
    """Les aiguilles dansent !
    
    mode = "sync"     : les deux aiguilles montent/descendent ensemble
    mode = "opposite" : RPM monte pendant que speed descend, et inverse
    mode = "chaos"    : frequences differentes, mouvement chaotique
    """
    mode_names = {
        "sync": "SYNCHRO (les deux ensemble)",
        "opposite": "OPPOSITION (l'une monte, l'autre descend)",
        "chaos": "CHAOS (rythmes differents)",
    }
    print(f"[*] DANCE MODE: {mode_names.get(mode, mode)}")
    print("    Mets de la musique. Ctrl+C pour arreter le show.")
    state["clean_mode"] = True

    def dance_loop():
        # Plages d'oscillation
        SPEED_MIN, SPEED_MAX = 0, 180     # km/h
        RPM_MIN, RPM_MAX = 800, 6500      # tr/min
        
        # Centre et amplitude pour les sinus
        speed_center = (SPEED_MIN + SPEED_MAX) / 2
        speed_amp = (SPEED_MAX - SPEED_MIN) / 2
        rpm_center = (RPM_MIN + RPM_MAX) / 2
        rpm_amp = (RPM_MAX - RPM_MIN) / 2
        
        t_start = time.monotonic()
        
        if mode == "sync":
            # Periode 2s, les deux en phase
            speed_freq = 0.5  # Hz
            rpm_freq = 0.5
            phase_offset_rpm = 0
        elif mode == "opposite":
            # Meme periode mais opposees (decalage pi)
            speed_freq = 0.5
            rpm_freq = 0.5
            phase_offset_rpm = math.pi
        elif mode == "chaos":
            # Frequences premieres entre elles -> jamais en phase
            speed_freq = 0.4
            rpm_freq = 0.7
            phase_offset_rpm = 0
        else:
            speed_freq = 0.5
            rpm_freq = 0.5
            phase_offset_rpm = 0

        while state["running"]:
            t = time.monotonic() - t_start
            
            # Sinusoides : centre + amplitude * sin(2*pi*freq*t)
            speed_val = speed_center + speed_amp * math.sin(2 * math.pi * speed_freq * t)
            rpm_val = rpm_center + rpm_amp * math.sin(2 * math.pi * rpm_freq * t + phase_offset_rpm)
            
            state["speed"] = max(0, int(speed_val))
            state["rpm"] = max(0, int(rpm_val))
            
            time.sleep(0.02)  # 50 Hz de mise a jour, mouvement bien fluide

    start_threads(extras=(dance_loop,))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Show termine !")
        state["running"] = False


def cmd_raw(arb_id_hex, data_hex):
    arb_id = int(arb_id_hex, 16)
    data = list(bytes.fromhex(data_hex))
    if len(data) > 8:
        print("[!] payload > 8 bytes")
        return
    print(f"[*] Boucle: {arb_id:03X}#{data_hex.upper()}")
    state["clean_mode"] = True
    state["rpm"] = 800
    state["raw_msgs"][arb_id] = data
    start_threads()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        state["running"] = False


def cmd_probe5a0(hex16):
    val = int(hex16, 16)
    lsb = val & 0xFF
    msb = (val >> 8) & 0xFF
    data = [0x84, lsb, msb, 0x00, 0x00, 0x00, 0x00, 0xAD]
    data_hex = "".join(f"{b:02X}" for b in data)
    print(f"[*] Probe 0x5A0 avec val=0x{val:04X}")
    print(f"    Trame: 5A0#{data_hex}")
    state["clean_mode"] = True
    state["rpm"] = 800
    state["raw_msgs"][0x5A0] = data
    state["speed"] = -1
    start_threads()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        state["running"] = False


def cmd_hunt(arb_id_hex):
    arb_id = int(arb_id_hex, 16)
    print(f"[*] HUNT sur 0x{arb_id:03X}")
    state["clean_mode"] = True
    state["rpm"] = 800
    start_threads()
    try:
        for pos in range(8):
            print(f"\n=== Byte position {pos} ===")
            for val in range(0, 256, 8):
                data = [0x00] * 8
                data[pos] = val
                if arb_id == 0x5A0:
                    data[0] = 0x84
                    data[7] = 0xAD
                    if pos == 0 or pos == 7:
                        continue
                state["raw_msgs"][arb_id] = data
                hex_str = " ".join(f"{b:02X}" for b in data)
                print(f"  pos={pos} val=0x{val:02X}  -> {arb_id:03X}#{hex_str}", end="\r")
                time.sleep(0.5)
        print("\n[*] Hunt termine")
    except KeyboardInterrupt:
        print(f"\n[*] STOP. Dernier essai: pos={pos} val=0x{val:02X}")
    finally:
        state["running"] = False


def main():
    global bus
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    bus = can.interface.Bus(channel=BUS, interface="socketcan")
    cmd = sys.argv[1]
    try:
        if cmd == "listen": cmd_listen()
        elif cmd == "clean": cmd_clean()
        elif cmd == "rpm": cmd_full(0, int(sys.argv[2]))
        elif cmd == "speed": cmd_full(int(sys.argv[2]), 800)
        elif cmd == "blink": cmd_blink(sys.argv[2])
        elif cmd == "full": cmd_full(int(sys.argv[2]), int(sys.argv[3]))
        elif cmd == "sweep": cmd_sweep()
        elif cmd == "dance":
            mode = sys.argv[2] if len(sys.argv) > 2 else "sync"
            cmd_dance(mode)
        elif cmd == "raw": cmd_raw(sys.argv[2], sys.argv[3])
        elif cmd == "probe5a0": cmd_probe5a0(sys.argv[2])
        elif cmd == "hunt": cmd_hunt(sys.argv[2])
        else:
            print(f"Commande inconnue: {cmd}")
            print(__doc__)
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
