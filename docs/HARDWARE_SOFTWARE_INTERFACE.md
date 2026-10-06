# CROPSYNC Hardware/Software Interface (PROPOSED DRAFT)

Status: **draft from an audit, not an approved spec.** Synchronized with the corrected contracts (`docs/contracts/*`, revision 2). Sources:
- **[CO]** `docs/cropsync_coding_overview.docx` ("CROPSYNC: Coding Overview", 2026-10-06)
- **[RB]** `docs/CROPSYNC_Rover_Build_and_Parts.docx` (Rev 6 + marked Rev 7 changes)

Authority order: [CO] and [RB] first, then the contracts (`RECORD_CONTRACT.md` IDs `R-n`, `WEBSOCKET_CONTRACT.md` IDs `W-n`, `ARM_SERIAL_CONTRACT.md` IDs `A-n`), then this document. Where this document and a contract differ, the contract (and above it the sources) wins.

Status labels used throughout:
- **CONFIRMED** = source-established: stated in [CO] or [RB]
- **PROPOSED / INFERRED** = interpretation by the audit author, not stated in the sources (marked "implied" or "inferred")
- **NOT SPECIFIED** = documents are silent; open / TBD before implementation
- **VERIFY** = requires physical/wiring verification

Rule: no GPIO number appears below unless a document states it. Everything else is `TBD`.

---

## A. Rover hardware inventory (all CONFIRMED from [RB] unless noted)

| Group | Part | Qty |
|---|---|---|
| Computers | ESP32 DevKit V1 (main) | 1 |
| | AI-Thinker ESP32-CAM, OV2640 | 1 |
| | MAX485 module (RS-485) | 1 |
| | Perfboard 7x9 cm (ESP32, MAX485, resistors) | 1 |
| Sensors | JXBS-3001 NPK probe; Capacitive Soil Moisture v2.0; DS18B20 waterproof (soil) | 1 each |
| | BME280; BH1750; HC-SR04 | 1 each |
| | Battery divider 47 kΩ + 10 kΩ; home micro limit switch | 1 each |
| | Second DS18B20 on battery (Rev 7) | 1 |
| Drive | JGB37-520 motor, 12 V, 107 rpm | 4 |
| | BTS7960 driver (one per side) | 2 |
| | 100 mm wheels, L-brackets, couplers | 4 each |
| Camera arm | 28BYJ-48 + ULN2003; SG90 servo | 1 each |
| | 5 V LED ring + IRLZ44N transistor; 4-wire arm cable | 1 each |
| Power | 12 V 10 W solar panel; SS54 diode; XL4015 charger (12.60 V, 1.0 A) | 1 each |
| | 45 °C thermal switch (Rev 7); 3S BMS 20 A; 3 x 18650; 10 A fuse + main switch | 1 each (cells x3) |
| | LM2596 (11.1 V to 5 V) | 1 |
| Probe pod (Rev 7) | NPK + moisture + soil temp in one frame, 1.5 m cable, 8-pin plug, fixed 10 cm depth | 1 |

Mechanical (CONFIRMED): 3.2 kg, 360 x 220 mm, ~41 cm tall at camera; two decks; solar panel doubles as rain roof. Not needed for firmware.

## B. Main ESP32 responsibilities (CONFIRMED [CO] s2, [RB])
- Core 1, 50 Hz safety/drive: command watchdog (500 ms), HC-SR04 stop (25 cm), 300 ms PWM ramp, battery cut-off at 9.9 V. Motor enables on **D19** stay LOW until setup finishes.
- Core 0 comms: Wi-Fi AP at 192.168.4.1, WebSocket server, phone message handling.
- Low-priority sensors: NPK over Modbus (library with DE/RE callbacks, CRC check, middle of 3 reads), async DS18B20, moisture average, BME280, BH1750; per-sensor status flag; one failure never blocks a record.
- AccelStepper for the arm (non-blocking); LittleFS record storage until phone confirms upload; task watchdog; all pins/thresholds in `config.h`.
- [RB]: reads soil, air and battery at each stop, "tells the camera board to turn, tilt, light up and check a leaf", and stores results.

## C. ESP32-CAM responsibilities (CONFIRMED [CO] s3, [RB])
- Capture mode saving labelled photos (same LED ring and distance as field); built first because training depends on it. How the mode is selected is NOT SPECIFIED (A-14).
- SHOOT handler: set servo tilt, LED ring on, ~2 s exposure settle then lock, 3 photos, Edge Impulse inference, majority label.
- Report "unclear" if the top score is under 70% or is not at least 20 points ahead of the second. NOT SPECIFIED: what the rule is applied to (per photo, aggregate, majority result), the case of 3 different labels, and the scale of `conf` (A-10, A-11, R-9 to R-11).
- Start camera before attaching servo, on a different timer.
- [RB] board description: "Takes leaf photos, runs the disease AI, tilts the camera and switches the lights." The physical wiring of the servo and LED ring to a specific board is NOT SPECIFIED; ownership of the pan/tilt mechanisms is unresolved (see section F, A-6).
- Model: Edge Impulse, 96x96 RGB, MobileNetV2 transfer learning, int8 ([CO] s7).
- NOT SPECIFIED: photo storage location on the camera side and how photos reach the main ESP32 or the phone (A-12, W-12); where the main ESP32 gets the model version (A-13).

## D. Sensor interfaces

| Sensor | Bus/type | Doc-stated detail | Status |
|---|---|---|---|
| JXBS-3001 NPK | RS-485 Modbus via MAX485 | DE/RE callbacks, CRC, middle of 3 reads. Register map, baud, slave address NOT SPECIFIED | Partly CONFIRMED; NOT SPECIFIED: registers/baud/address; VERIFY probe datasheet |
| Capacitive moisture v2.0 | Analog (implied; type not stated) | Averaged | NOT SPECIFIED: ADC pin, calibration |
| DS18B20 soil | 1-Wire (implied) | Async read | NOT SPECIFIED: pin, bus sharing with battery DS18B20 |
| DS18B20 battery (Rev 7) | 1-Wire (implied) | Alert in app; threshold NOT SPECIFIED | NOT SPECIFIED |
| BME280 | I2C (implied) | Temp/humidity/pressure | NOT SPECIFIED: address, pins |
| BH1750 | I2C (implied) | Daylight | NOT SPECIFIED: address, pins |
| HC-SR04 | Trig/Echo (implied) | Stop at 25 cm; 5 V echo scaled to 3.3 V by resistors ([RB] "small parts") | NOT SPECIFIED; VERIFY echo level shifting |
| Battery divider | ADC | 47 kΩ / 10 kΩ; cut-off 9.9 V | NOT SPECIFIED: ADC pin, which resistor is high side, calibration |
| Home switch | Digital input (implied) | Arm start position | NOT SPECIFIED: pin, polarity, pull |

"(implied)" means the bus type is an inference by the audit author from the part type, not stated in the documents.

## E. Drive interfaces
- 2 x BTS7960, one per side, tank-style turning, 4 x JGB37-520 (CONFIRMED).
- Enables on D19 held LOW until setup completes (CONFIRMED requirement).
- NOT SPECIFIED: which signals D19 drives (the BTS7960's enable-input count is general component knowledge, not from the sources; two drivers exist), PWM pins, direction pins, PWM frequency/resolution, whether motors on one side are paralleled on one driver channel, current-sensing use.
- VERIFY: enable polarity and pull-down on D19 during boot/reset.

## F. Camera-arm interfaces
- 28BYJ-48 + ULN2003: "Turn the camera 0–350° in precise steps" [RB]. The rover firmware list includes "AccelStepper for the arm (non-blocking)" [CO s2]. Pins NOT SPECIFIED.
- SG90 servo: "Tilts the camera from 60° down to 45° up" [RB]. No signed coordinate convention is defined by the sources (A-5). Pin and which board it is wired to: NOT SPECIFIED.
- LED ring 5 V via IRLZ44N; on only for the photo ([RB]). Controlling GPIO and board NOT SPECIFIED.
- Home micro-switch on top deck ([RB]). Pin NOT SPECIFIED.
- 4-wire arm cable "Carries power and messages between the two computers" ([RB]). Wire assignment NOT SPECIFIED (A-1).
- **Pan/tilt ownership is unresolved (A-4, A-5, A-6).** The sources describe the main ESP32 as the board that "turns the arm" and as the board that "tells the camera board to turn, tilt, light up and check a leaf", and describe the camera board as the board that "tilts the camera". `SHOOT <pan> <tilt>` carries both values. The sources do not state which board physically performs which motion, the meaning or units of `pan`/`tilt`, or the order. This document does not choose.

## G. Power/safety-related inputs
- Battery voltage via divider, cut-off at 9.9 V (CONFIRMED). Rev 7 battery temperature via DS18B20, alert in app (CONFIRMED); a 45 °C thermal switch independently cuts charging (hardware, no MCU input documented).
- BMS, fuse, SS54, XL4015, LM2596 are hardware-only; no MCU interface documented.
- NOT SPECIFIED: low-battery recovery/hysteresis, temperature alert threshold, brown-out behaviour. [RB] mentions capacitors to prevent camera restart on power dips (VERIFY).
- VERIFY: full power-distribution wiring (not documented).

## H. Main ESP32 <-> ESP32-CAM communication
CONFIRMED [CO] s1: "Arm cable (serial)": one line per message with a sequence number and checksum. `SHOOT <pan> <tilt>` and `LEAF <class> <conf> <photo-id>` are given "for example"; `BUSY` while the camera is working.
- NOT SPECIFIED: serial interface/pins on both boards, baud rate, voltage level, parity/stop bits, line terminator, field order of sequence/checksum, checksum algorithm, timeouts/retries, units/range/meaning of pan/tilt, who originates `SHOOT` at message level, who performs pan and tilt, `conf` scale, photo-id format, whether `BUSY` is a reply or periodic, error replies, how an "unclear" result is encoded, whether the three messages are the complete set (A-1 to A-15).
- NOT SPECIFIED: the arm line sequence number's generation and its relationship to the record sequence number or any drive sequence (A-3, W-17). No relationship is defined.
- NOT SPECIFIED: photo storage and the camera-to-main photo path (A-12).

## I. Phone <-> Main ESP32 WebSocket
CONFIRMED [CO]: Wi-Fi AP 192.168.4.1; WebSocket server; named messages `drive {dir, speed}` (about 10 times a second), `status`, `test_here`, `record`.
- Drive direction wording: [CO] s1 lists these messages under "Rover to phone (WebSocket)"; [RB] says the phone "sends joystick commands to the ESP32". Direction wording is an open decision (W-1). Directions of `status`, `test_here`, `record` are NOT SPECIFIED (W-18).
- NOT SPECIFIED: WebSocket port/path, SSID/password, message schemas and encoding, `dir` and `speed` ranges/units, `status` payload fields (the Drive screen shows battery and obstacle distance, which is a UI fact, not a payload definition; W-8), exact `test_here` request/response semantics (W-9), record delivery (push or request), photo transfer path (W-12), the meaning of "phone confirms upload" and the confirmation message (W-11), error messages, authentication.
- "Unique IDs (rover ID + counter) make retries safe" (CONFIRMED [CO] s4). Which objects carry the ID and what the counter is: NOT SPECIFIED (R-4). No relationship to the record sequence number is established.

## J. Record data contract (JSON [CO] s1; item list CONFIRMED)
time; sequence number; rover ID; battery; soil; air; sensor status flags; leaf label; confidence; photo ID; firmware, model, rules and calibration versions.
- NOT SPECIFIED: field names, types, units, nesting, which soil/air values are included (moisture, NPK, soil temp, air temp/humidity/pressure, light?), status-flag encoding, battery temperature inclusion, NPK raw vs calibrated.
- NOT SPECIFIED: the time source and any synchronisation mechanism; neither source names a time source (R-2, W-13). None is assumed here.
- NOT SPECIFIED: exact semantics of the record sequence number (R-3, R-4).
- NOT SPECIFIED: how the rover obtains the rules and calibration versions, given that rules and calibration live on the app side, and where model and firmware versions come from (R-6, W-14, A-13).
- NOT SPECIFIED: effect of camera or leaf-check failure on a record (R-8). The proxy's `/second-opinion` input is "the photo, readings, crop and language" [CO s5]; the sources don't say it is the record, and crop/language are not record items (R-14).
- Calibration (CONFIRMED [CO] s8): Lab = a*Probe + c*Moisture + b; kg/ha = mg/kg x 2.24.

## K. Safety behaviours (source-established [CO] s2 unless marked)
1. Command watchdog (500 ms). Which messages feed it and what the rover does on a trip: NOT SPECIFIED (W-2).
2. Obstacle stop (25 cm). Source wording: "HC-SR04 stop (25 cm)" [CO]; "stops if anything is within 25 cm" [RB]. The comparison operator is OPEN (W-3); no operator is chosen here. Whether reverse is allowed while stopped: NOT SPECIFIED (W-5).
3. 300 ms PWM ramp.
4. Battery cut-off at 9.9 V [CO]; "stops driving below 9.9 V" [RB]. The comparison operator is OPEN (W-4); no operator is chosen here. Recovery behaviour: NOT SPECIFIED.
5. D19 motor enables LOW until setup finished.
6. Task watchdog.
7. Sensor failure never blocks a record; status flags.
8. Records kept in LittleFS until the phone confirms upload. What "confirms upload" means (received, stored/queued, cloud upload succeeded, other) is NOT SPECIFIED (W-11); receipt is not equated with upload.
NOT SPECIFIED: STOP button semantics (W-6).

## L. Configuration values

| Value | Setting | Status |
|---|---|---|
| Drive loop | 50 Hz | CONFIRMED |
| Command watchdog | 500 ms | CONFIRMED |
| Phone drive rate | about 10 per second | CONFIRMED |
| Obstacle stop | 25 cm (comparison operator OPEN, W-3) | CONFIRMED value; operator OPEN |
| PWM ramp | 300 ms | CONFIRMED |
| Battery cut-off | 9.9 V (comparison operator OPEN, W-4) | CONFIRMED value; operator OPEN |
| AP IP | 192.168.4.1 | CONFIRMED |
| Exposure settle | ~2 s | CONFIRMED |
| Photos per shoot | 3 | CONFIRMED |
| Unclear rule | top score under 70%, or not at least 20 points ahead of the second; scope and `conf` scale OPEN (A-10, A-11) | CONFIRMED thresholds; scope OPEN |
| NPK read | middle of 3 reads | CONFIRMED |
| Charger | 12.60 V, 1.0 A | CONFIRMED |
| Arm ranges | Stepper "0–350°"; servo "60° down to 45° up" as written in [RB]; relationship to `SHOOT` parameters and any signed convention OPEN (A-5) | CONFIRMED wording; use OPEN |
| Probe depth | 10 cm | CONFIRMED |
| Wi-Fi credentials, ports, baud rates, sensor timings, battery warn/temp thresholds | | NOT SPECIFIED |

## M. Exact information still missing before firmware
1. Final GPIO map for the main ESP32 (every item in the table below).
2. Final GPIO map for the ESP32-CAM, including serial, servo and LED-ring pins.
3. Motor-driver wiring (what D19 drives; PWM, direction and enable per side).
4. Arm-cable wire assignment and the serial parameters and framing (baud, voltage, terminator, checksum algorithm and encoding).
5. NPK Modbus register map, slave address, baud.
6. WebSocket message schemas and record field names/units (the `status` and `test_here` semantics included).
7. Complete schematic and power-distribution wiring.
8. Time source and synchronisation (R-2, W-13).
9. Whether D19 corresponds to a particular GPIO number on the DevKit V1 is unverified; no GPIO number is assumed.
10. Photo storage and transfer path (A-12, W-12).
11. Version provenance for rules, calibration, model, firmware (R-6).
12. Meaning of "phone confirms upload" (W-11).
13. Pan/tilt ownership and `pan`/`tilt` meaning (A-4 to A-6).
14. Comparison operators for the 25 cm and 9.9 V thresholds, and reverse behaviour during obstacle stop (W-3, W-4, W-5).
15. `conf` scale, scope of "unclear", behaviour with 3 different labels (A-10, A-11).
16. Sequence-number semantics and relationships (R-3, R-4, A-3, W-17).
17. Capture-mode selection (A-14).

---

## GPIO / interface mapping table

Pins appear only where a document states them.

| Component | Function | Interface type | Proposed ESP32 pin | Status | Source/Reason | Verification required |
|---|---|---|---|---|---|---|
| BTS7960 (x2) | Motor enables | Digital out | **D19** (as written in [CO]; which enable(s) it covers not stated) | CONFIRMED (pin) / NOT SPECIFIED (scope) | [CO] s2: stay LOW until setup finishes | Yes: scope, polarity, boot-state |
| BTS7960 (x2) | PWM / direction | Not stated | TBD | NOT SPECIFIED | Not in documents | Yes |
| Stepper (ULN2003) | Arm turntable | Not stated (digital outputs implied) | TBD (and which board drives it: A-6) | NOT SPECIFIED | [CO] AccelStepper; [RB] | Yes |
| Home switch | Arm zero | Digital in (implied) | TBD | NOT SPECIFIED | [RB] | Yes |
| HC-SR04 | Distance | Trig out / Echo in (implied) | TBD | NOT SPECIFIED | [RB] | Yes: echo level |
| Battery divider | Voltage | ADC | TBD | NOT SPECIFIED | [RB] 47 kΩ / 10 kΩ | Yes |
| Moisture v2.0 | Soil moisture | ADC (implied) | TBD | NOT SPECIFIED | [RB] | Yes |
| DS18B20 soil / battery | Temperature | 1-Wire (implied) | TBD | NOT SPECIFIED | [RB] | Yes |
| BME280 / BH1750 | Air / light | I2C (implied) | TBD | NOT SPECIFIED | [RB] | Yes: addresses |
| MAX485 | NPK Modbus | RS-485 via MAX485; DE/RE control | TBD | NOT SPECIFIED | [CO] DE/RE callbacks | Yes |
| Arm cable to ESP32-CAM | Serial link | Serial (interface/pins not stated) | TBD (both ends) | NOT SPECIFIED | [CO], [RB] | Yes |
| SG90 servo | Tilt | Not stated | TBD (and which board: A-6) | NOT SPECIFIED | [RB] | Yes |
| LED ring + IRLZ44N | Light switch | Not stated (transistor switch per [RB]) | TBD (and which board) | NOT SPECIFIED | [RB] | Yes |

## Conflicts and risks

**Confirmed conflict**
- None found between the two source documents on interface facts.
- The coding overview file is `cropsync_coding_overview.docx` (a Word document).

**Source ambiguities (documented in the contracts, not resolved here)**
- [CO] s1 heading "Rover to phone" versus `drive` being a phone command (W-1).
- Differing threshold wording for the obstacle stop and battery cut-off (W-3, W-4).
- "Turns the arm" versus "tells the camera board to turn", and `SHOOT` carrying `pan` (A-4, A-6).
- Photos pulled from the rover, with no documented photo storage or camera-to-main path (A-12, W-12).
- Meaning of "phone confirms upload" (W-11).
- Version provenance for a rover-written record (R-6).

**Potential risk requiring verification** (general ESP32/hardware knowledge, not stated in the documents)
- Pin budget on the main ESP32: motors, stepper, HC-SR04, serial links, I2C, 1-Wire, ADC, home switch. Whether it fits depends on the final map.
- ADC2 pins can't be used with Wi-Fi active; the moisture and battery ADC pins must avoid them.
- Strapping/boot pins can disturb boot if used for motor/enable lines; relevant to the "enables LOW until setup" requirement.
- ESP32-CAM has few free GPIOs while the camera runs; the serial link, servo and LED ring (wherever each is wired) all need pins, and serial lines may share GPIO with flashing/boot logs.
- 5 V signals into 3.3 V pins (HC-SR04 echo) need level scaling; [RB] says resistors do this but gives no map.
- Servo/camera power dips and the camera restart noted in [RB]: depends on power wiring.
- Divider output at full charge: 12.6 V x 10/57 = 2.21 V, inside ADC range (derived from stated resistors).

**Unknown**
- Everything listed under M.

## Repository readiness

| Area | Status | Reason |
|---|---|---|
| Rover firmware | NOT READY | No `rover-firmware/`, no pin map; draft contracts exist but their schemas and semantics are open. Build order puts it 4th. |
| Camera firmware | NOT READY | No project; serial, servo and LED pins unspecified; photo storage and transfer unresolved. |
| Mock rover | BLOCKED on contract decisions | Draft contracts exist (`docs/contracts/`), but message and record schemas are OPEN (W-8, W-9, R-1). |
| Phone app | BLOCKED on contract decisions | Same; no project scaffolding. |
| Nebius proxy | NOT STARTED, least hardware-dependent | Needs its own payload definition (photo, readings, crop, language; R-14) and the Token Factory model/CPU-endpoint checks listed in [CO] s5. |
