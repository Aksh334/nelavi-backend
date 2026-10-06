# Arm Serial Contract (Main ESP32 <-> ESP32-CAM)

Status: **DRAFT specification (revision 2, corrected after verification), not approved.** No implementation exists.

Source tags: **[CO]** = `docs/cropsync_coding_overview.docx`; **[RB]** = `docs/CROPSYNC_Rover_Build_and_Parts.docx`; **[HSI]** = `docs/HARDWARE_SOFTWARE_INTERFACE.md` (an audit derived from CO/RB, not an independent source).

Classification used in every section:
- **SOURCE-ESTABLISHED**: directly stated in CO or RB.
- **PROPOSED DESIGN**: a suggestion, not required by the sources.
- **OPEN / TBD**: not defined by the sources; must be decided before implementation. IDs are `A-n`.

Companion contracts: `RECORD_CONTRACT.md` (IDs `R-n`), `WEBSOCKET_CONTRACT.md` (IDs `W-n`).

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

The main ESP32 and the ESP32-CAM on the camera arm communicate over a serial link on the arm cable [CO s1, RB]. This contract covers that link only.

## Source-Established Requirements

1. The link is **serial** and is the "Arm cable (serial)" [CO s1]. The 4-wire arm cable "Carries power and messages between the two computers" [RB].
2. **One line per message, with a sequence number and checksum** [CO s1].
3. Example messages [CO s1]: `SHOOT <pan> <tilt>`, `LEAF <class> <conf> <photo-id>`, and `BUSY` "while the camera is working". CO s1 introduces `SHOOT` and `LEAF` with "for example"; whether these three are the complete message set is not stated (A-2).
4. Camera-side SHOOT handler [CO s3]: set the servo tilt, turn the LED ring on, let exposure settle for about 2 s, then lock it, take 3 photos, run the Edge Impulse model and return the majority label. Report "unclear" if the top score is under 70% or is not at least 20 points ahead of the second.
5. Start the camera before attaching the servo, on a different timer [CO s3].
6. System-level roles [RB]:
   - Main ESP32: "Reads every sensor, drives the wheels, turns the arm...". It "tells the camera board to turn, tilt, light up and check a leaf".
   - Camera board: "Takes leaf photos, runs the disease AI, tilts the camera and switches the lights".
   - These are descriptions of the whole system. They do not state which side sends `SHOOT` or which board executes pan.
7. Firmware: the main ESP32 uses "AccelStepper for the arm (non-blocking)" [CO s2].
8. Arm hardware ranges [RB], quoted as written: the 28BYJ-48 stepper and ULN2003 board "Turn the camera 0–350° in precise steps"; the SG90 servo "Tilts the camera from 60° down to 45° up". The source **does not** say that the `pan` and `tilt` parameters of `SHOOT` use these angles, ranges, units or direction conventions. No sign convention is defined (A-5).
9. The LED ring is on "only for the photo" [RB].
10. A capture mode "that saves labelled photos, using the same LED ring and distance as in the field" exists in camera firmware [CO s3]. How it is selected is not stated (A-14).
11. The model is Edge Impulse, 96x96 RGB, MobileNetV2 transfer learning, int8 [CO s7].

## Contract Definition

### Line format
SOURCE-ESTABLISHED: one line per message; each line carries a sequence number and a checksum. OPEN: separator, field order, terminator, checksum algorithm, scope and encoding (A-2).

### Messages

| Message | Direction | Parameters | Status |
|---|---|---|---|
| `SHOOT <pan> <tilt>` | Not stated at message level. RB says the main ESP32 tells the camera board to act (A-4) | `pan`, `tilt` | Name and parameter names SOURCE-ESTABLISHED. Meaning, units, ranges OPEN (A-5, A-6) |
| `LEAF <class> <conf> <photo-id>` | Camera board to main ESP32 is the natural reading, not stated | class, confidence, photo ID | Name and parameter names SOURCE-ESTABLISHED. Types and formats OPEN (A-10) |
| `BUSY` | Not stated | none | Name and "while the camera is working" SOURCE-ESTABLISHED. Whether periodic or a reply, and who sends it, OPEN (A-7) |

### LEAF fields
- `class`: the class or label. "unclear" is a result the camera reports [CO s3]; how it is encoded in `LEAF` is OPEN (A-11).
- `conf`: the meaning and scale are **TBD**; 0-1, 0-100, percentage or probability are all unconfirmed (A-10). The thresholds in [CO s3] are written as "70%" and "20 points", which doesn't define the `conf` field's scale.
- `photo-id`: format and relation to photo storage OPEN (A-10, A-12).
- Scope of the "unclear" decision (per photo, aggregate, majority result) is OPEN (A-11), as is the case of 3 different labels (A-11).

### Sequence number and checksum
Both are required on each line [CO s1]. The sources state neither their purpose nor generation, correlation or algorithm (A-2, A-3). The relationship of the ARM line sequence number to the record sequence number is not defined (see Terminology).

### Relationship to the record
`LEAF` carries class, confidence and photo ID [CO s1], which correspond to the record items leaf label, confidence and photo ID. How the model version is obtained from the camera board is OPEN (A-13).

## Proposed Interpretation of the Flow

This is an interpretation of CO s3 and RB, **not** a specified sequence:
1. The main ESP32 sends a `SHOOT` line.
2. The camera board carries out the CO s3 steps and reports `BUSY` while working.
3. The camera board sends a `LEAF` line with its result.

Acknowledgements, retries and error replies are not defined (A-7, A-8, A-9).

## Validation

SOURCE-ESTABLISHED: each line has a sequence number and checksum [CO s1]. OPEN: what happens when validation fails (A-9).

## Error / Failure Behavior

SOURCE-ESTABLISHED: `BUSY` is sent "while the camera is working" [CO s1]. Everything else is OPEN: checksum failure, lost lines, camera not responding, `BUSY` that never clears, `SHOOT` while busy, camera restart, and the effect of a failed leaf check on the record (A-9; R-8). [RB] notes capacitors against a camera restart on power dips, which is a power-side remark and defines no protocol behaviour.

## Proposed Design Decisions

PROPOSED, none of these is in the sources:
- P1. The main ESP32 is the master and originates `SHOOT` (consistent with RB's "tells the camera board", but not stated at message level).
- P2. If requests and responses are to be matched, the responder echoes the requester's sequence number.
- P3. Lines are printable text, since the source examples are text.

## Open Decisions / TBD

| ID | Decision |
|---|---|
| A-1 | **Serial interface and pins on both boards**, wire assignment of the 4-wire cable (which wires carry power and which carry data), baud rate, voltage level / level shifting, parity, stop bits, power feed. None is given in the sources |
| A-2 | Line terminator, separator, field order, checksum algorithm, scope and encoding; whether `SHOOT`, `LEAF` and `BUSY` are the complete message set |
| A-3 | **ARM line sequence number**: who generates it, width, wraparound, how responses correlate; relationship to the record sequence number and any drive sequence |
| A-4 | **SHOOT command ownership** (which board sends it, at message level) |
| A-5 | Meaning, units, ranges, sign convention and absolute-vs-relative of `pan` and `tilt` |
| A-6 | **Pan mechanism ownership**: which board physically performs pan, and which performs tilt, and in what order. Note: [RB] says the main ESP32 "turns the arm" and also says it "tells the camera board to turn, tilt, light up and check a leaf" |
| A-7 | Acknowledgement / response sequence; whether `BUSY` is periodic or a reply; who sends it |
| A-8 | Timeouts and retry timing |
| A-9 | Error replies and failure handling (checksum failure, no response, `BUSY` that never clears, `SHOOT` while busy) |
| A-10 | **`LEAF` confidence meaning and range**; class list; photo-id format |
| A-11 | **Scope of "unclear"** (per photo, aggregate, majority result, other), its encoding in `LEAF`, and **what happens if the 3 photos give different labels** |
| A-12 | **Photo storage location** on the camera side, and **how photos get from the camera board to the main ESP32 / phone**. No storage device or transfer path is defined in the sources |
| A-13 | How the model version is obtained from the camera board (with R-6) |
| A-14 | **Camera capture mode**: how it is selected |
| A-15 | Whether the camera board can send unsolicited lines (boot or ready messages) |

## Source Traceability

| Item | Source |
|---|---|
| Serial link, line format, example messages | CO s1 |
| SHOOT handler, unclear rule, start camera before servo, capture mode | CO s3 |
| AccelStepper non-blocking | CO s2 |
| Model details | CO s7 |
| Board roles, arm cable, LED ring, stepper and servo ranges | RB |
| Known gaps | HSI sections F, H, M |
