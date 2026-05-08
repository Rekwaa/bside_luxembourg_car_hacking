# How I Hacked a Car Dashboard from My Laptop

### A walkthrough of taking control of a Škoda Fabia instrument cluster over CAN bus

---

## The Setup

Imagine a car dashboard sitting on a table — speedometer, RPM gauge, warning lights, the whole thing — disconnected from any actual car. Just the cluster, two wires for power, two wires for data. Now imagine making the speedometer needle climb to 200 km/h while the car isn't moving. That's the challenge.

This was at the **BSides Luxembourg 2026 Car Hacking Village**. The hardware: a Škoda Fabia 6J cluster (the same family as VW Polo, Seat Ibiza, etc.). The goal: control everything on it from my Kali Linux laptop.

---

## Background: What is CAN bus?

Modern cars don't have one giant computer running everything. They have **dozens of small computers (ECUs)** — one for the engine, one for the brakes, one for the airbags, one for the dashboard — and they all talk to each other on a shared network called the **CAN bus** (Controller Area Network).

Think of it like a group chat where every ECU shouts messages, and each one listens for the messages it cares about.

A CAN message looks like this:

```
ID: 0x280   Data: 49 0E 80 0C 0E 00 1B 0E
```

- **ID** identifies the type of message (here, `0x280` happens to mean "engine RPM")
- **Data** is up to 8 bytes of payload (the actual values)

If I can put fake messages on the bus, I can make the cluster believe whatever I want — the engine is at 7000 RPM, the car is doing 200 km/h, the left turn signal is on, etc.

---

## The Hardware Connection

The cluster has a 32-pin connector on the back. I only needed five wires:

```
   Cluster pin       What it is        Where it goes
   ───────────       ──────────        ─────────────
       32     ──     +12V constant     PSU positive
       31     ──     +12V switched     PSU positive (both pins must be hot)
       16     ──     Ground            PSU negative AND adapter ground
       28     ──     CAN-High          Adapter CAN-H
       29     ──     CAN-Low           Adapter CAN-L
```

To connect my laptop to the bus I used a **CANable 2.0** — a tiny USB dongle that bridges USB on one side and CAN on the other. On Kali, it shows up as a regular network interface called `can0`, which means standard Linux tools work on it.

> **The most common rookie mistake:** forgetting to tie the PSU ground to the adapter ground. CAN works on the *difference* between the two wires, and if the grounds aren't connected, that difference floats unpredictably. Symptom: the bus looks dead, or you get garbage with `ERRORFRAME` everywhere. Always common ground.

Bringing up the bus on Kali:

```bash
sudo ip link set can0 type can bitrate 500000
sudo ip link set can0 up
```

---

## Step 1: Listen Before Speaking

Before sending anything, I just wanted to see what the cluster was saying on its own:

```bash
candump -tz can0
```

A flood of messages appeared. The cluster, even with no other ECUs around, was constantly broadcasting status updates:

```
0x320   12 00 FF 00 00 01 00 3F   <- cluster heartbeat
0x420   85 FF FF 00 00 7F FF 84   <- "Kombi 2" status
0x5D2   00 00 00 00 00 54 4D 42   <- VIN piece 1: "TMB"
0x5D2   01 4B 4E 32 35 4A 36 42   <- VIN piece 2: "KN25J6B"
0x5D2   02 33 31 38 39 31 33 33   <- VIN piece 3: "3189133"
```

Funny detail: the cluster was happily broadcasting its own VIN (`TMBKN25J6B3189133`) — it really thought it was in a real car.

This passive listen step is huge because it tells me which IDs the cluster **emits**. Whatever it emits, I shouldn't try to send myself (collision territory). Whatever it doesn't emit but exists in PQ25 documentation, those are likely the **inputs** I can use to control it.

---

## Step 2: Mapping the Inputs

Volkswagen has been using the same CAN protocol family (PQ25) across many models for years, and a lot of it has been reverse-engineered by hobbyists. Cross-referencing the OpenStreetMap VW-CAN wiki and a GitHub project called `VW-Instrument-Cluster-Controller`, I built this map:

| ID | What it controls | Format |
|---|---|---|
| `0x050` | Airbag light | All zeros = OK |
| `0x280` | RPM needle | Bytes 2-3 = `RPM × 4` little-endian |
| `0x288` | Coolant temp | Byte 1 = temp encoding (`0x9A` ≈ 80°C) |
| `0x3D0` | Immobilizer | All zeros = key recognized |
| `0x470` | Turn signals | Byte 0: `01` left, `02` right, `03` hazards |
| `0x480` | Engine warnings | All zeros = no warnings |
| `0x1A0` | ABS status | `04 00 LSB MSB FE FE 00 00` |
| `0x4A0` | 4-wheel speeds | Each wheel as LSB/MSB pair |
| `0x4A8` | Brake pressure | All zeros = not braking |
| `0x5A0` | **Speedometer** | Special — see below |

Each of these has a **cycle time** — how often a real ECU would send it. The immobilizer at `0x3D0`, for example, must be sent every 100 ms or so. If I stop, after about half a second the cluster panics and lights up the warning. So everything has to be sent **continuously in the background**.

---

## Step 3: The Architecture

I wrote a Python script with `python-can` that runs **11 threads in parallel**, each one impersonating a different ECU's behavior:

```
┌────────────────────────────────────────────────────┐
│            fabia_cluster.py                        │
├────────────────────────────────────────────────────┤
│                                                    │
│  Engine ECU             ABS ECU                    │
│  ──────────             ───────                    │
│  • immo (3D0)           • abs (1A0)                │
│  • RPM  (280)           • wheels (4A0)             │
│  • coolant (288)        • brakes (4A8)             │
│  • flags (480)          • speedo (5A0) ★           │
│                                                    │
│  Other                  Tools                      │
│  ─────                  ─────                      │
│  • airbag (050)         • raw injector (debug)     │
│  • blinker (470)                                   │
│                                                    │
│  All threads share one global state{}              │
└────────────────────────────────────────────────────┘
```

The clever part: every thread reads from the same shared dictionary `state`. When I want to "drive at 130 km/h", I just set `state["speed"] = 130`, and every thread automatically adjusts its output:

- ABS now reports 130 km/h on all 4 wheels
- The brakes report "not pressed"
- The odometer advances at the right rate
- The engine still says "idle, 800 RPM"

The cluster sees a coherent system. No mismatch warnings, no error lights.

---

## Step 4: The Speedometer Trap

This is where I got humbled. RPM was easy — set bytes 2-3 of `0x280` to `RPM × 4`, done. The needle moves perfectly.

But the speedometer? **Nothing worked**. I tried:

1. ❌ Sending `0x1A0` with wheel speed → ABS warning goes off, but needle stays at 0
2. ❌ Encoding speed in `0x5A0` bytes 1-2 → still nothing
3. ❌ Trying `0x320` → it collides with the cluster's own broadcasts, ignored
4. ❌ Random fuzzing for 5 minutes → not a twitch

I was stuck. Then a colleague gave me the missing piece:

> *"The cluster doesn't read a speed value. It computes the speed from the odometer."*

This was the breakthrough. Let me unpack what it means.

### How the cluster actually computes speed

The cluster doesn't just look at a "speed" field in the message. Instead, it watches the **odometer counter** in the `0x5A0` payload (bytes 5-6, a 16-bit number representing distance traveled). It measures **how fast that number grows** between consecutive frames:

- If the odometer adds 100 every 10 ms → "wow, this car is moving fast!"
- If the odometer doesn't change → "we're stopped"

So the trick is: I have to send `0x5A0` very frequently, **and the odometer has to actually increment** at a rate proportional to the speed I want to fake.

### The math

The PQ25 platform uses a convention of **50 odometer counts per meter**. So:

```
At 100 km/h:
  100 km/h = 27.78 m/s
  27.78 m/s × 50 counts/m = 1389 counts/sec
  At 100 Hz transmission (every 10 ms) = ~14 counts per frame
```

So at 100 km/h, every time I send a `0x5A0` frame, the odometer should be 14 higher than the last frame.

### The fix in code

```python
def thread_speed_5a0():
    while running:
        kmh = state["speed"]
        
        # Compute how much to bump the odometer this frame
        increment = int(kmh / 3.6 * 50 * 0.010)
        state["odo"] = (state["odo"] + increment) & 0xFFFF
        
        # Build the frame (odometer in bytes 5-6)
        data = [
            0x84,                       # constant marker
            ...,                        # speed also encoded here
            state["odo"] & 0xFF,        # odo LSB ← THE KEY
            (state["odo"] >> 8) & 0xFF, # odo MSB ← THE KEY
            0xAD,                       # constant end marker
        ]
        send(0x5A0, data)
        time.sleep(0.010)  # send 100 times per second
```

The moment I plugged this in: the needle moved. Smoothly. To exactly the speed I asked for.

---

## Step 5: The Bugs I Hit Along the Way

This wasn't a clean linear story. I fought four real bugs:

### Bug 1: The turn signal always showed hazards

```python
# What I thought I was doing
byte0 = {"left": 0x01, "right": 0x02, "hazard": 0x03}.get(direction)

# What the code actually had
data = [0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
                      # ↑ byte0 hardcoded to 0x03, my variable ignored
```

Computed the right value and then used a hardcoded one. Pure copy-paste sloppiness. Took me embarrassingly long to spot.

### Bug 2: The 0→200 sweep stopped at 110

The first version of the sweep loop set `state["speed"]` going up by 1 every 150 ms. But the odometer was incrementing at a **fixed rate** (not proportional to speed). So the cluster was always seeing the same "real" speed regardless of what I claimed in `state["speed"]`, and the needle just plateaued at whatever speed the fixed odometer rate represented.

Fix: make the odometer increment scale with the target speed (the formula above).

### Bug 3: Ctrl+C broke the sweep

The sweep `for v in range(0, 201)` was running in the main thread, with sleeps inside it. When I hit Ctrl+C to stop, the `KeyboardInterrupt` would land somewhere mid-sweep, the cleanup code would set `running = False`, and the descent half of the sweep was never reached. The needle would freeze wherever the sweep happened to be when interrupted.

Fix: move the sweep into a daemon thread, keep the main thread doing nothing but waiting for Ctrl+C. The interrupt now lands cleanly on the main thread, signals the sweep thread to exit, and the script shuts down properly.

### Bug 4: I deleted working code thinking it was useless

Twice. **Twice** I removed the odometer increment during "cleanup refactors", because I didn't understand it was the only reason the speedometer was working. Then I'd spend an hour wondering why the needle stopped moving.

The lesson is uncomfortable but real: **don't simplify code you don't understand yet.** Commit working code first. Refactor second. If you can't articulate exactly what a line does, leave it alone.

---

## The Final Result

With everything working, the script gives me a clean command-line interface:

```bash
sudo python3 fabia_cluster.py clean         # all warnings off, idling engine
sudo python3 fabia_cluster.py speed 130     # speedometer at 130 km/h
sudo python3 fabia_cluster.py rpm 7000      # engine at redline
sudo python3 fabia_cluster.py blink l       # left turn signal blinking
sudo python3 fabia_cluster.py sweep         # smooth 0→200→0 in a loop
```

Every workshop challenge solved:

| Challenge | Solution |
|---|---|
| 01 — Listen | `candump can0` |
| 02 — Spoof immobilizer | Send `0x3D0` every 100 ms |
| 03 — Turn signals | `0x470` with bit pattern in byte 0 |
| 04 — RPM | `0x280` with `RPM × 4` |
| 05 — Speed | `0x5A0` with incrementing odometer |
| 06 — Fuzz | `cangen` on one ID at a time |

---

## What This Taught Me

**Reverse engineering is rarely about cleverness, it's about patience and observation.** The single most useful command in the whole project was `candump`. Just listening. Watching what bytes change when something happens. The speedometer mystery couldn't be solved by reading documentation alone — it took someone else's specific knowledge plus my own experiments.

**Real systems have sanity checks.** The cluster doesn't trust a single value. It cross-checks the odometer against the wheel speed, the engine RPM against the speed, the brake pressure against the deceleration. Faking one value isn't enough — you have to fake the whole picture **consistently**.

**A multi-threaded simulator is the right architecture.** Each ECU on a real car runs independently and broadcasts at its own rhythm. Trying to do this in one big loop would have been a nightmare. One thread per simulated ECU is clean and matches reality.

**Modern cars are fragile.** Once you're on the bus with valid frames, you can convince the dashboard of pretty much anything. There's basically no authentication on classic CAN. The only thing protecting cars from this kind of attack is the difficulty of physically accessing the bus — once an attacker is plugged in (via OBD-II port, infotainment hack, or compromised telematics), the dashboard will believe whatever it's told.

Newer cars have **CAN-FD with cryptographic message authentication** for exactly this reason. But there are tens of millions of cars on the road that don't.

---

## The Tech Stack, in One Glance

```
┌─────────────────────────────────────────────────┐
│  Python 3 + python-can                          │
│        ↓ socketcan                              │
│  Linux kernel SocketCAN                         │
│        ↓ gs_usb driver                          │
│  CANable 2.0 USB dongle (candleLight firmware)  │
│        ↓ CAN-H / CAN-L                          │
│  Škoda Fabia 6J cluster (PQ25 platform)         │
└─────────────────────────────────────────────────┘
```

Total cost of the rig: less than €60 (the dongle was €40, the wires and PSU were stuff lying around). Total time invested: about a weekend of evenings, half of which was spent chasing the speedometer mystery.

---

## Further Reading

If this got you curious:

- **The Car Hacker's Handbook** by Craig Smith — the bible of automotive security, free PDF online
- **OpenGarages** community — workshops and ICSim, a software CAN simulator to practice without hardware
- **comma.ai's openpilot project** — open source self-driving stack that talks to a huge range of cars over CAN
- The **OpenStreetMap VW-CAN wiki** — surprisingly detailed PQ25 reverse engineering reference

---

*Built on a kitchen table with a screwdriver, a USB dongle, and Python. The cluster currently sits in my desk drawer between sessions, still believing it's a 2010 Škoda Fabia. It is not.*
