# Car Hacking Village — BSides Luxembourg 2026

**Challenge:** take control of a Škoda Fabia 6J instrument cluster over the CAN bus, running locally on Kali (no WebUSB workshop console).

**Platform:** Kali Linux + CANable 2.0 USB-CAN adapter (candleLight / gs_usb firmware) + physical Fabia 6J (PQ25) cluster on a bench, 12V powered.

**Status:** All workshop challenges solved.

---

## TL;DR

The Fabia 6J cluster (PQ25 platform) listens to CAN frames at 500 kbit/s on pins 28 (CAN-H) and 29 (CAN-L). To drive it from Kali, I wrote a multi-threaded Python script that simultaneously simulates the engine ECU, ABS ECU, airbag ECU, and BCM. The hard part wasn't finding the IDs (that was quick) — it was figuring out that **the speedometer is computed by the cluster from an odometer counter that must be incremented over time**, not from a "speed" value sent in the payload.

```bash
sudo python3 fabia_cluster.py clean         # healthy idling car
sudo python3 fabia_cluster.py speed 130     # speedo at 130 km/h
sudo python3 fabia_cluster.py blink l       # left turn signal
sudo python3 fabia_cluster.py sweep         # 0 -> 200 -> 0 in a loop
```

---

## 1. Hardware setup

### Cluster side (32-pin connector)

| Pin | Signal | To |
|---|---|---|
| 32 | +12V constant | + bench PSU |
| 31 | +12V switched (IGN) | + bench PSU (both pins must be at +12V to wake the cluster) |
| 16 | GND | – PSU **and** adapter GND (common ground is mandatory) |
| 28 | CAN-H | adapter CAN-H |
| 29 | CAN-L | adapter CAN-L |

Classic gotcha: if the 12V PSU ground isn't tied to the USB-CAN adapter ground, the differential CAN levels float and you'll see nothing (or garbage with ERRORFRAMEs). Non-negotiable.

### Kali side

```bash
# Install
sudo apt install -y can-utils python3-can

# Verify CANable detected (gs_usb in dmesg)
dmesg | tail
ip link show can0

# Bring up CAN bus at 500 kbit/s
sudo ip link set can0 type can bitrate 500000
sudo ip link set can0 up

# Sanity check
ip -details -statistics link show can0
```

---

## 2. Phase 1 — Passive recon

First step: listen to the bus and figure out what the cluster broadcasts on its own.

```bash
candump -tz can0
cansniffer -c can0   # easier to read: groups by ID, highlights changing bytes
```

### What the cluster emits (do not send these)

The cluster broadcasts these IDs in a loop towards the rest of the network:

| ID | Description | Notes |
|---|---|---|
| `0x320` | Cluster status | byte 5 = rolling counter 0→F, byte 7 = checksum |
| `0x420` | Kombi 2 | byte 7 transitions from `0x04` to `0x84` once ready |
| `0x51A` | Internal counters status | |
| `0x520` / `0x52A` | Diag/status | |
| `0x5D2` | **Cluster VIN** multiplexed | byte 0 = 0/1/2 → "TMB" + "KN25J6B" + "3189133" |
| `0x5F3`, `0x60E`, `0x621`, `0x62E` | Various status frames | |
| `0x727` | UDS diag | |

### Key observation

None of the classic speedometer-related IDs (`0x1A0`, `0x4A0`, `0x5A0`) appear in the passive capture → these are **inputs** the cluster expects to receive, not outputs.

Boot capture (recording the bus while plugging in the 12V) also revealed:

- No needle sweep on this particular cluster at startup (this is an EEPROM-coded option, normal on some 6J variants)
- The `0x420` and `0x621` timeout bytes decrement when no other ECUs are present → the cluster realizes it's alone and gradually enters degraded mode

---

## 3. Phase 2 — PQ25 ID mapping

Sources used: OpenStreetMap VW-CAN wiki, the `an-ven/VW-Instrument-Cluster-Controller` GitHub repo, and the BSides workshop reference table.

### Validated IDs on this cluster

| ID | Function | Payload format | Cycle |
|---|---|---|---|
| `0x050` | Airbag / seatbelt | `00 00 00 00 00 00 00 00` (all OK) | 100 ms |
| `0x280` | Engine RPM | `49 0E LSB MSB 0E 00 1B 0E` with **RPM × 4** little-endian on bytes 2-3 | 20 ms |
| `0x288` | Coolant temperature | `00 9A 00 ...` (~80°C, green zone) | 100 ms |
| `0x3D0` | Immobilizer | `00 00 00 00 00 00 00 00` | 100 ms |
| `0x470` | Turn signals / doors | byte 0: `0x01` left, `0x02` right, `0x03` hazards | 100 ms |
| `0x480` | Engine warning flags (EPC, DPF, oil) | all `0x00` = no warning | 100 ms |
| `0x1A0` | Bremse 1 (ABS) | `04 00 LSB MSB FE FE 00 00` | 7 ms |
| `0x4A0` | Bremse 3 (4-wheel speeds) | 4 LSB/MSB pairs, identical for straight line | 10 ms |
| `0x4A8` | Bremse 4 (brake pressure) | all `0x00` | 20 ms |
| `0x5A0` | Bremse 2 (**odometer + speedo**) | see section 4 | 10 ms |

### Value encoding

- **RPM:** `16-bit value = RPM × 4` → 800 RPM = `0x0C80` → bytes 2-3 = `80 0C`
- **ABS wheel speed** (`0x1A0`, `0x4A0`): `16-bit value = km/h × 100` → 100 km/h = `0x2710`

---

## 4. Phase 3 — The speedometer trap

This is where I burned the most time. Several wrong turns:

### Attempt 1: send `0x1A0` (Bremse 1) with wheel speed
ABS warning goes off ✓ but speedo needle stays put ✗

### Attempt 2: send `0x5A0` with speed encoded in bytes 1-2
Still motionless ✗

### Attempt 3: try `0x320` directly
The cluster **emits** `0x320` itself, so my frames collided on the bus → ignored ✗

### Attempt 4: `cangen can0 -I 5A0 -L 8 -D R -g 50` for 5 minutes straight
Needle still frozen ✗

### The breakthrough

A colleague gave me the key insight:

> `0x5A0` is the odometer, and it must be incremented every frame according to the current speed.

The cluster **does not read a "speed" value** from the payload. It **derives speed by measuring how fast a counter increments** between frames. If the counter doesn't move → needle stays at 0, no matter what's in the other bytes.

This is documented in the `an-ven` repo: *"counter must be incremented according to current speed value in order for the speedometer to work properly."* I had read that line and missed its implications.

### The Python bug that cost me 2 hours

My early script versions used a `raw_msgs[ID] = fixed_bytes` dict sent in a loop. **The payload was static**, so the odometer never moved. When I added the increment in one version, I later removed it during a "cleanup" without realizing it was the only thing making the speedometer work.

### The format that finally works

```python
def thread_speed_5a0():
    while state["running"]:
        kmh = max(0, state["speed"])

        # 50 counts per meter, kmh/3.6 = m/s, * 50 = counts/s, * period = per frame
        increment = int(kmh / 3.6 * 50 * SEND_PERIOD_5A0)
        state["odo"] = (state["odo"] + increment) & 0xFFFF

        v_encoded = (kmh * 75) << 1
        data = [
            0x84,                          # neutral lateral acceleration
            v_encoded & 0xFF,              # speed LSB
            (v_encoded >> 8) & 0xFF,       # speed MSB
            0x00, 0x00,
            state["odo"] & 0xFF,           # ODO LSB <- THE KEY
            (state["odo"] >> 8) & 0xFF,    # ODO MSB <- THE KEY
            0xAD,                          # end marker
        ]
        send(0x5A0, data)
        time.sleep(0.010)  # 100 Hz
```

The increment formula comes from the PQ25 convention: 50 counts per meter. At 100 km/h = 27.78 m/s, that's 1389 counts/second, or ~14 per frame at 100 Hz.

---

## 5. Final script architecture

The `fabia_cluster.py` script runs **11 parallel threads**, each simulating a specific ECU behavior:

```
┌─────────────────────────────────────────────────────────────┐
│                  fabia_cluster.py                           │
│                                                             │
│  Engine ECU sim:           ABS ECU sim:                     │
│   - thread_immo (3D0)      - thread_abs (1A0)               │
│   - thread_rpm (280)       - thread_wheels (4A0)            │
│   - thread_coolant (288)   - thread_brake_4a8 (4A8)         │
│   - thread_engine (480)    - thread_speed_5a0 (5A0) ★       │
│                                                             │
│  Other:                    Tools:                           │
│   - thread_airbag (050)    - thread_raw (debug)             │
│   - thread_blink (470)                                      │
│                                                             │
│              shared state{} dict                            │
└─────────────────────────────────────────────────────────────┘
```

★ The critical thread is `thread_speed_5a0` which keeps the odometer consistent with the target speed.

### Inter-ECU consistency

All threads share the same `state["speed"]`, so when you request 100 km/h:

- ABS reports "100 km/h on all 4 wheels" (`0x1A0`, `0x4A0`)
- Brake reports "not pressed" (`0x4A8`)
- Odometer increments as if driving 100 km/h (`0x5A0`)
- Engine reports idle 800 RPM (`0x280`)

The cluster sees a coherent system speaking with one voice. No inconsistency warnings.

---

## 6. Workshop challenge solutions

### Challenge 01 — Listen
```bash
candump -tz can0
cansniffer -c can0
```

### Challenge 02 — Spoof the immobilizer
```bash
sudo python3 fabia_cluster.py clean
```
The orange wheel warning goes off. To find the max timeout, I played with `time.sleep` in `thread_immo`: threshold sits around 500 ms before the warning comes back.

### Challenge 03 — Turn signals
```bash
sudo python3 fabia_cluster.py blink l
sudo python3 fabia_cluster.py blink r
sudo python3 fabia_cluster.py blink h
```

`thread_blink` alternates between active payload and OFF payload every second to produce a real visible blink — otherwise the indicator stays solid because the cluster treats "stable input = stable state".

### Challenge 04 — RPM
```bash
sudo python3 fabia_cluster.py rpm 800     # idle
sudo python3 fabia_cluster.py rpm 7000    # redline
```

### Challenge 05 — Speed / sweep
```bash
sudo python3 fabia_cluster.py speed 130
sudo python3 fabia_cluster.py sweep        # 0 -> 200 -> 0 looping
```

### Challenge 06 — Responsible fuzzing
```bash
cangen can0 -I 588 -L 8 -D R -g 100
```
One ID at a time, never `-I i` (incremental over all IDs) which would flood the bus and likely brick the BCM into a fault state.

---

## 7. Bugs hit and lessons learned

### Bug 1 — Turn signal always stuck on hazard
```python
# BEFORE (broken)
byte0 = {"left": 0x01, "right": 0x02, "hazard": 0x03}.get(state["blink"], 0x00)
data = [0x03, 0x00, 0x00, ...]   # byte0 computed but ignored, hardcoded to 0x03
```
Computing `byte0` then not using it. Classic copy-paste mistake.

### Bug 2 — Sweep capping at 110 km/h
The odometer was advancing at a **fixed rate** independent of `state["speed"]`. The cluster always derived the same speed regardless of what we asked for. Fix: increment proportional to target speed.

### Bug 3 — Sweep interrupted by Ctrl+C in the wrong sleep
The `for v in range(0, 201)` loop was running in the main thread. A Ctrl+C would kill the upward sweep before it completed and skip the descent entirely. Fix: move the sweep into a daemon thread, keep `KeyboardInterrupt` on the main loop.

### Bug 4 — Invisible regressions between versions
At least twice, I deleted code that was working (the odometer increment) thinking it was useless — without realizing it was the cornerstone making the speedometer work. **Lesson: git commit before "cleanup".**

---

## 8. Possible improvements

- **Fine calibration** of the 75/148 factor in `v_encoded` based on the wheel diameter coded in the cluster's EEPROM (the ratio differs across cluster versions)
- **VIN read** via UDS on `0x727` (the cluster already partially broadcasts the VIN on `0x5D2`)
- **DTC injection**: send malformed frames on monitored IDs to surface specific fault codes
- **Replay attack**: record real traffic with `candump -L > log.txt` then replay with `canplayer`
- **MITM bridge**: insert a Raspberry Pi between two segments of the bus to modify frames on the fly

---

## 9. Tech stack

| Component | Version |
|---|---|
| OS | Kali Linux 2026.x |
| CAN adapter | CANable 2.0 (candleLight firmware, gs_usb driver) |
| Bus | Classic CAN, 500 kbit/s |
| Language | Python 3 + `python-can` (socketcan backend) |
| CLI tools | `can-utils` (`candump`, `cansend`, `cangen`, `cansniffer`) |

---

## 10. References

- VW-CAN OpenStreetMap wiki
- `an-ven/VW-Instrument-Cluster-Controller` (GitHub)
- *The Car Hacker's Handbook* — Craig Smith (essential reading)
- BSides Luxembourg 2026 — Car Hacking Village

---

*Everything works. The cluster is fully driven from Kali. No Frankenstein setup, no Wireshark on Windows — just Python talking straight to the bus.*
