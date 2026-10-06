# Rover <-> Phone WebSocket Contract

Status: **DRAFT specification (revision 2, corrected after verification), not approved.** No implementation exists.

Source tags: **[CO]** = `docs/cropsync_coding_overview.docx`; **[RB]** = `docs/CROPSYNC_Rover_Build_and_Parts.docx`; **[HSI]** = `docs/HARDWARE_SOFTWARE_INTERFACE.md` (an audit derived from CO/RB, not an independent source).

Classification used in every section:
- **SOURCE-ESTABLISHED**: directly stated in CO or RB.
- **PROPOSED DESIGN**: a suggestion, not required by the sources.
- **OPEN / TBD**: not defined by the sources; must be decided before implementation. IDs are `W-n`.

Companion contracts: `RECORD_CONTRACT.md` (IDs `R-n`), `ARM_SERIAL_CONTRACT.md` (IDs `A-n`).

## Shared Terminology (identical in all three contracts)

These terms are kept separate on purpose. **The sources do not define any relationship between them** unless a row says so.

| Term | What the sources say | Relationship to the others |
|---|---|---|
| Record sequence number | "sequence number" is one of the record items [CO s1]. | OPEN (R-3, R-4) |
| Rover ID | "rover ID" is one of the record items [CO s1]. | Part of "rover ID + counter" below |
| Retry-safe unique ID | "Unique IDs (rover ID + counter) make retries safe" [CO s4]. The sentence sits in the app paragraph about pulling and uploading records and photos. It doesn't say which objects carry these IDs, nor what the counter is. | Not stated to be the record sequence number or the photo ID (R-4) |
| ARM line sequence number | Every arm-cable line carries "a sequence number and checksum" [CO s1]. | Not stated to relate to the record sequence number (A-3) |
| Drive command sequence number | None in the sources. Listed only as a possible future item. | Does not exist in the sources (W-7) |
| Photo ID | `photo-id` field of `LEAF`, and a record item [CO s1]. | Not stated to relate to any of the above (R-12) |
| Received / queued / uploaded | The sources use "phone confirms upload" [CO s2] and "uploads when mobile data is available" [CO s4]. They don't define "received" or "queued" as confirmation events. | OPEN (W-11) |

## Purpose

Carries messages between the operator's phone and the rover's main ESP32. The mock rover "speaks the same WebSocket protocol" [CO s9].

## Source-Established Requirements

1. The main ESP32 runs a Wi-Fi access point at **192.168.4.1** and a **WebSocket server** on the comms task (core 0) [CO s2]. The rover's own Wi-Fi network is listed as a Rev 7 item [RB]. The phone joins this network [RB].
2. The WebSocket message list [CO s1]: `drive {dir, speed}` "about 10 times a second", plus `status`, `test_here` and `record` messages.
3. **Drive direction evidence.**
   - [CO s1] lists these messages under the heading "Rover to phone (WebSocket)", which does not match `drive` being a command.
   - [RB] says "The operator's phone joins the rover's own Wi-Fi network and sends joystick commands to the ESP32."
   - [CO s4] says the Drive screen has a joystick.
   - The direction phone -> rover is therefore supported by RB and CO s4, and it is **not** stated unambiguously in CO s1 (W-1). Whether the RB "joystick commands" are the `drive` messages is an inference.
4. Rover-side safety mechanisms, in the "Safety and drive (core 1, 50 Hz)" task group [CO s2]:
   - **command watchdog (500 ms)**
   - **HC-SR04 stop (25 cm)**
   - **300 ms PWM ramp**
   - **battery cut-off at 9.9 V**
   - **motor enables on D19 stay LOW until setup finishes**
5. Source wording of the two thresholds, preserved as written:
   - Obstacle: "HC-SR04 stop (25 cm)" [CO s2]; "the rover stops if anything is within 25 cm" [RB].
   - Battery: "battery cut-off at 9.9 V" [CO s2]; "stops driving below 9.9 V" [RB].
   - The sources don't define the exact comparison. See W-3 and W-4.
6. UI facts: the Drive screen shows a joystick, a big STOP, battery and obstacle distance; there is a Test here screen [CO s4]. These are UI requirements, **not** protocol field definitions.
7. "The app pulls records and photos from the rover over the rover's Wi-Fi" [CO s4]. The mechanism is not specified (W-12).
8. LittleFS stores records "until the phone confirms upload" [CO s2]. "Unique IDs (rover ID + counter) make retries safe" [CO s4].
9. The source describes no phone-side safety mechanism. The app is described as "the gateway" [CO s4].

## Contract Definition

Transport: WebSocket served by the rover on its Wi-Fi AP. Path, port, subprotocol, framing and authentication are OPEN (W-15).

### Message table

| Message | Direction | Status |
|---|---|---|
| `drive {dir, speed}` | phone -> rover (supported by RB and CO s4; CO s1 heading ambiguous, W-1) | Name, fields `dir`, `speed`, and rate ~10/s are SOURCE-ESTABLISHED; field types and ranges OPEN |
| `status` | OPEN (W-18) | Name SOURCE-ESTABLISHED; payload OPEN (W-8) |
| `test_here` | OPEN (W-18) | Name SOURCE-ESTABLISHED; semantics and payload OPEN (W-9) |
| `record` | OPEN (W-18) | Name SOURCE-ESTABLISHED; content relates to RECORD_CONTRACT |

Directions of `status`, `test_here` and `record` are not stated in the sources. A literal reading of the CO s1 heading ("Rover to phone") would apply to all four messages, which does not fit `drive`. The directions of the other three therefore remain OPEN. Proposed directions are given under Proposed Design.

### `drive {dir, speed}`
- SOURCE-ESTABLISHED: fields `dir` and `speed`; about 10 per second [CO s1].
- OPEN: types, ranges, units, neutral/stop representation, sequence number or ordering (W-7).
- The source names a "command watchdog (500 ms)" [CO s2] and doesn't say which messages feed it; that it is fed by `drive` messages is an inference (W-2). The nominal gap at 10/s is ~100 ms (derived arithmetic, not a source statement).
- Acknowledgement: none specified.

### `status`
- UI requirement (not protocol): the Drive screen shows battery and obstacle distance [CO s4].
- Protocol payload, send rate and trigger: OPEN (W-8). Whether those two values arrive in `status`, and in which fields, is not established.

### `test_here`
- SOURCE-ESTABLISHED: a message named `test_here` exists [CO s1] and a Test here screen exists [CO s4]. [RB] describes what happens at each stop (probe pressed in; ESP32 reads soil, air, battery; camera board checks a leaf; results stored).
- OPEN (W-9): what `test_here` carries, what the rover does on receiving it, whether it is acknowledged, how a resulting `record` is correlated with it, and behaviour if a test is already running or the camera is busy.

### `record`
- Content relates to `RECORD_CONTRACT.md`. Whether the rover pushes a record or the phone requests stored records (the source says the app "pulls") is OPEN (W-10).

### Photos
- Requirement: the phone must eventually obtain the photos from the rover over the rover's Wi-Fi [CO s4].
- Mechanism: **TBD** (W-12). No transport (WebSocket frames, HTTP, file download or other), encoding or chunking is chosen here. Where photos are stored, and how they reach the main ESP32 from the camera board, is also not specified (W-12, A-12).

### Upload / receipt semantics (important)
- SOURCE-ESTABLISHED: LittleFS holds records "until the phone confirms upload" [CO s2]. The app "uploads when mobile data is available" [CO s4] and, per [RB], "uploads everything when it has signal". Neither sentence names the upload destination; that it is a cloud-side upload is an inference.
- The sources do **not** say which event counts as "confirms upload". The candidate events are: the phone received the record; the phone stored or queued it (IndexedDB); the cloud upload succeeded; or something else.
- This contract does **not** equate "received" with "uploaded", and it authorizes **no** deletion trigger (W-11). Until W-11 is decided, nothing in this contract permits the rover to delete a stored record. Whether that is also the right long-term behaviour is part of W-11.

### Connection / disconnection
Not specified, apart from the command watchdog (W-2). Reconnection, multiple clients and authentication are OPEN (W-15).

## Validation

SOURCE-ESTABLISHED: none beyond the field names `dir`, `speed` and the rover-side safety mechanisms listed above.
OPEN: field types and ranges, unknown-message handling, size limits (W-7, W-15).

## Error / Failure Behavior

SOURCE-ESTABLISHED: the rover-side safety mechanisms exist (command watchdog, obstacle stop, ramp, battery cut-off, D19 enables). The sources don't describe their exact effect on motion beyond their names.
OPEN: what the rover does when the watchdog trips and how it recovers (W-2), the effect of obstacle stop on reverse motion (W-5), the effect of STOP (W-6), error message format (W-15).

## Proposed Design Decisions

PROPOSED, none of these is in the sources:

- P1. Messages are JSON text frames with a `type` field naming the message.
- P2. `drive` has no per-message acknowledgement. `status` is sent periodically so the phone can detect link loss.
- P3. Directions: `status` rover -> phone, `test_here` phone -> rover, `record` rover -> phone.
- P4. `test_here` is acknowledged, and its resulting `record` is correlated with it.
- P5. `status` reports obstacle-stop and low-battery states for display only.
- P6. Unknown or malformed messages are ignored and never affect motor outputs.
- P7. **Project-owner directive (from the project's task instructions, not stated in CO or RB):** the phone is not the safety authority; safety is enforced on the rover. The STOP button is treated as a command to the rover, and the rover-side mechanisms still apply when the link is lost. The exact STOP semantics remain OPEN (W-6).
- P8. The rover treats incoming drive values as untrusted input. This is a design choice, not a source statement.
- P9. Interim retention: until W-11 is decided, the rover does not delete stored records on any event the sources do not clearly establish.

## Open Decisions / TBD

| ID | Decision |
|---|---|
| W-1 | **Drive command direction wording**: CO s1 heading says "Rover to phone"; RB says the phone sends joystick commands. Final contract direction to be confirmed |
| W-2 | Which messages feed the 500 ms command watchdog; what the rover does on trip; recovery when commands resume |
| W-3 | **Obstacle threshold comparison operator** (source: "stop (25 cm)" [CO], "within 25 cm" [RB]) |
| W-4 | **Battery cut-off comparison operator** (source: "at 9.9 V" [CO], "below 9.9 V" [RB]); recovery behaviour |
| W-5 | Whether **reverse motion is permitted while obstacle stop is active** |
| W-6 | Semantics of the STOP button (distinct message or `drive` with zero speed; effect) |
| W-7 | `dir`/`speed` types, ranges, units; whether any drive sequence number exists; ordering and stale-command handling; relationship to other sequence numbers (see Terminology) |
| W-8 | **Exact `status` fields**, rate and trigger |
| W-9 | **Exact `test_here` request/response semantics** |
| W-10 | Record delivery: pushed or requested; framing, chunking, ordering |
| W-11 | **What "phone confirms upload" means** (received / stored or queued / cloud upload succeeded / other), the confirmation message and transport, and the rover's deletion trigger |
| W-12 | **Photo transfer path** from the rover to the phone; photo storage location; camera-to-main transfer (with A-12) |
| W-13 | Time source and synchronisation: whether any message is involved (with R-2) |
| W-14 | How rules and calibration versions reach the rover, if at all (with R-6) |
| W-15 | WebSocket path, port, subprotocol, Wi-Fi credentials, authentication, reconnect, multiple clients, error format |
| W-16 | How the mock rover represents the sample photo and the "unclear" scenario [CO s9] |
| W-17 | Relationship between record sequence number, ARM line sequence number and any drive sequence number |
| W-18 | Directions of `status`, `test_here`, `record` |

## Source Traceability

| Item | Source |
|---|---|
| AP, WebSocket server, core 0, safety group | CO s2 |
| Message names, heading, ~10/s | CO s1 |
| Phone sends joystick commands; Rev 7 Wi-Fi | RB |
| Drive screen, gateway, pulls records and photos, IndexedDB | CO s4 |
| LittleFS until phone confirms upload | CO s2 |
| Mock rover speaks same protocol | CO s9 |
| Threshold wording | CO s2, RB (sensor table) |
| Known gaps | HSI sections I, K |
