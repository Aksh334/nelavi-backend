# Architecture Decisions

## Decision Status / Rules

- Only actual decisions are recorded as decisions here.
- Requirements belong in the authoritative source and interface documents (the six frozen baseline documents). This file does not define or change them.
- Unresolved questions stay unresolved and are listed under Open Architectural Questions.
- A proposed future decision must be labelled as a proposal and is not implemented architecture.
- A decision should record enough context to understand why it was made.

## Confirmed Decisions

These were established by the project owner or formally during the completed audit and contract work. They predate the record format below, so they carry no decision IDs.

| Decision | Context / source |
|---|---|
| The six frozen baseline documents are the authoritative technical baseline. Authority order: source documents, then contracts, then HSI. | Established at the final contract freeze (FREEZE APPROVED). |
| The frozen documents are not modified. Their open items remain open unless an authoritative source explicitly resolves them. | Project-owner rule for the implementation phase. |
| The interface contracts (record, WebSocket, arm serial) are written before implementation and hold the components together. | `cropsync_coding_overview.docx` section 1. |
| Safety mechanisms are rover-side (command watchdog, obstacle stop, PWM ramp, battery cut-off, D19 enables held LOW until setup finishes). The phone is not the safety authority. | Rover-side mechanisms: coding overview section 2 (a requirement). The "phone is not the safety authority" stance is a project-owner directive recorded in `WEBSOCKET_CONTRACT.md` (P7). |
| Development follows UNDERSTAND -> PLAN -> IMPLEMENT -> TEST -> REVIEW -> FIX -> RETEST -> VERIFY -> RECORD. | Project-owner process rule. |
| The project stays at `C:\Users\Akshaj dasam\OneDrive\Documents\CROPSYNC`. No OneDrive problem has been established. | Project-owner decision after the environment and Git pre-setup audits. |
| The existing global Git identity is kept unchanged. | Project-owner decision. |
| `.gitignore` is minimal: operating-system files and Office lock files only; no stack-specific rules. | Git baseline setup. |

### Architectural direction stated by the source documents

The coding overview names the following. They come from the source, not from a decision made during the audit; they are not scaffolded and have not been separately re-confirmed.

- Planned repository layout: `rover-firmware/`, `camera-firmware/`, `app/`, `proxy/`, `rules/`, `calibration/`, `mock-rover/`, `tests/`.
- Build order: contracts and repo, Nebius call and proxy, mock rover and app, rover firmware, camera firmware, model training and escalation, calibration and rules, tests and README.
- Named technologies: ESP32 with FreeRTOS, AccelStepper, LittleFS, ESP32-CAM with Edge Impulse, a Capacitor phone app with IndexedDB, a Nebius-hosted proxy (the source calls Python with FastAPI "simplest"), Nemotron through Token Factory, Tavily.
- The first checkpoint is a record flowing from the mock rover through the app and proxy to Nemotron and back to the screen.

## Open Architectural Questions

None of these is resolved here.

- Photo storage location and the camera-to-main-board and rover-to-phone photo transfer path (W-12, A-12, R-12).
- Authoritative time source and synchronisation for the record (R-2, W-13).
- Provenance of firmware, model, rules and calibration versions in a rover-written record (R-6, W-14, A-13).
- Meaning of "phone confirms upload", and hence when a stored record may be removed (W-11, R-15).
- Pan/tilt ownership and `SHOOT` command ownership between the main ESP32 and the ESP32-CAM (A-4, A-6).
- Camera capture-mode selection (A-14).
- Token Factory checks stated in the coding overview section 5: which Nemotron model accepts images, and whether Serverless Endpoints can run a CPU-only container on the available credits.
- Per-component implementation toolchain: only Git, Python 3.14.6, Node.js v24.19.0 and npm 11.17.0 are verified present, Docker is not installed, and other tooling was not checked. No toolchain has been set up.
- Repository line-ending handling: `core.autocrlf=true` (system config), no `.gitattributes`. The frozen hashes are of the current LF working files.

The full open decision lists are in the three contracts (R-, W-, A- IDs).

## Decision Record Format (for future decisions)

Use this format only for decisions actually made. Do not create entries or IDs in advance.

- **ID:** sequential, assigned when the decision is made (for example `ADR-0001`)
- **Date:** date the decision was made
- **Status:** Proposed / Accepted / Superseded / Rejected
- **Context:** the situation and constraints
- **Decision:** what was decided
- **Alternatives considered:** other options and why they were not chosen
- **Consequences:** what follows from the decision, including follow-up work
- **References:** the baseline documents, contract IDs, commits or reports that support it

## Owner-Approved Implementation Decisions: Record Validation Core (MD-0 to MD-6)

**Status of all seven entries: Accepted.** These are **owner-approved implementation decisions** for the current Nelavi Record Core milestone (the project was previously named CropSync; the frozen documents keep the historical name). They were first recorded on 2026-10-06 as choices made by the implementer, and were approved by the owner on 2026-10-07 with the answers given below (MD-4 was corrected to match the owner's wording).

**What these decisions are not.** They are implementation decisions made for the current project milestone. They do not change the frozen source documents or the frozen contracts. The open items they touch **remain open in the frozen documents**. Nothing here claims that a source or contract ambiguity has been retroactively removed. Frozen contracts remain unchanged; their underlying open items remain open.

**Identifiers.** `MD-0` to `MD-6` are the owner's labels for this decision package. They are not contract IDs. The `ADR-0001` form in the format section above is only an example, so the smallest consistent scheme is used: `MD-` followed by the owner's number. The R-, W- and A- IDs below are the real open-item IDs from the frozen contracts.

**How each entry is organised.** Each entry separates (1) the source or contract requirement, (2) the implementation decision, and (3) what is still open in the frozen contracts.

### MD-0: Governance mechanism
- **Related open items:** none (governance decision).
- **Source/contract requirement:** none. The rule recorded in "Confirmed Decisions" above is that the frozen documents are not modified and their open items remain open unless an authoritative source explicitly resolves them.
- **Implementation decision:** an owner-approved entry in this file may establish an implementation decision for a currently open source or contract item without modifying the frozen source or contract. The entry must say that it is an implementation decision for the current project milestone, must not claim the underlying frozen ambiguity is removed, and must use the real open-item IDs.
- **Still open:** every item the entries refer to stays open in the frozen documents.
- **Alternatives considered:** a formal contract revision; a separate decisions folder.

### MD-1: Implementation language and location
- **Related open items:** none from the contracts. The per-component toolchain question under "Open Architectural Questions" stays open for every other component.
- **Source/contract requirement:** the coding overview section 1 repository layout names `rover-firmware/`, `camera-firmware/`, `app/`, `proxy/`, `rules/`, `calibration/`, `mock-rover/`, `tests/`; it has no `record/` directory. Section 5 mentions Python only for the proxy.
- **Implementation decision:** Python 3.14.6, standard library only, in a top-level package `record/` with `record/validation.py`; tests in `tests/`. No packages or dependencies are installed. This is an implementation choice for the Record Core and does not fix any other component's language.
- **Alternatives considered:** an existing layout directory; the app's language.

### MD-2: Canonical top-level record
- **Related open items (remain open in the frozen contract):** R-1, R-2, R-5, R-6, R-12, R-18.
- **Source/contract requirement:** the coding overview section 1 lists the record items: "time, sequence number, rover ID, battery, soil, air, sensor status flags, leaf label, confidence, photo ID, and the firmware, model, rules and calibration versions". It does not define structure, key names or types.
- **Implementation decision:** the canonical Record Core representation has exactly these top-level keys: `time`, `sequence_number`, `rover_id`, `battery`, `soil`, `air`, `sensor_status`, `leaf_label`, `confidence`, `photo_id`, `versions`. `versions` has exactly `firmware`, `model`, `rules`, `calibration`. `time`, `rover_id` and each version are non-empty strings. `sequence_number` is an integer; a boolean is not accepted as an integer. No time-format rule is applied.
- **Still open:** R-2 (time source, synchronisation and format); R-1 and the other items above.
- **Alternatives considered:** grouped keys; a fixed time format.

### MD-3: Canonical readings
- **Related open items (remain open in the frozen contract):** R-13, R-1 (units).
- **Source/contract requirement:** the build document lists the sensors (NPK probe, capacitive moisture, DS18B20 soil temperature, BME280 air temperature, humidity and pressure, battery voltage divider, BH1750, and a Rev 7 battery DS18B20). The coding overview says NPK probe values pass through the calibration correction before the rules see them. Which readings the record holds is not defined.
- **Implementation decision:** `soil` has `nitrogen`, `phosphorus`, `potassium`, `moisture`, `temperature`; `air` has `temperature`, `humidity`, `pressure`; `battery` has `voltage_v`. NPK values are raw readings; no calibrated NPK fields are added. BH1750/light and battery temperature are **not** part of this Record Core. This is an implementation-scope decision only: it does not claim those sensors do not exist or can never be represented elsewhere, and the frozen source is unchanged. A reading is a finite JSON number or `null` (unavailable); NaN and infinity are rejected. `voltage_v` is the battery voltage in volts. No other reading carries a unit.
- **Still open:** R-13 (which readings the contract record holds, raw versus calibrated NPK, units).
- **Alternatives considered:** calibrated or raw-plus-calibrated NPK; including light and battery temperature.

### MD-4: Sensor status and leaf fields
- **Related open items (remain open in the frozen contract):** R-7, R-8.
- **Source/contract requirement:** the coding overview section 2: "Each sensor has a status flag, so one failure never blocks a record." The `LEAF <class> <conf> <photo-id>` line carries the class, confidence and photo ID together (section 1). The flag values and the effect of a camera failure on the record are not defined.
- **Implementation decision:** `sensor_status` has exactly `npk`, `moisture`, `soil_temperature`, `air`, `battery`, each exactly `"ok"` or `"fault"`; no other value is accepted. All canonical fields are present. An unavailable numeric reading is `null`. A sensor flagged `"fault"` may have its associated readings `null` or a finite number; both are valid. A sensor fault never by itself makes the record invalid, and a sensor fault is not a machine fault or machine health state. A sensor flagged `"ok"` must have finite numeric readings, so `"ok"` with a `null` reading is invalid (intentional). The owner confirmed these two interpretations on 2026-10-07. Mapping: `npk` to the three NPK readings, `moisture`, `soil_temperature`, `air` to the three air readings, `battery` to `battery.voltage_v`. `leaf_label`, `confidence` and `photo_id` are either all populated or all `null`. No camera status field is added. `"unclear"` is an ordinary non-empty `leaf_label`.
- **Still open:** R-7 (status-flag set and encoding, missing-reading representation) and R-8 (camera failure effect).
- **Alternatives considered:** omitted readings; a three-state flag; a camera flag.

### MD-5: Confidence scale (Record Core field only)
- **Related open items (remain open in the frozen contract):** R-9. A-10 and any other `conf` field are not affected.
- **Source/contract requirement:** the coding overview section 3 states the "unclear" thresholds as "70%" and "20 points". It does not define a scale for the confidence field.
- **Implementation decision:** `confidence` is a number from 0 through 100 inclusive. Values below 0 or above 100 are invalid. `confidence` may be `null` only when `leaf_label` and `photo_id` are also `null`.
- **Still open:** R-9 for the contract, A-10, and the scope of "unclear" (R-10, R-11).
- **Alternatives considered:** 0 to 1; unconstrained.

### MD-6: Validation policy
- **Related open items (remain open in the frozen contract):** R-1 (validation).
- **Source/contract requirement:** only "one failure never blocks a record" (coding overview section 2). Handling of invalid records and unknown fields is not defined.
- **Implementation decision:** the validator (1) rejects a record when a required structural, type or value rule fails, (2) collects all applicable errors, (3) rejects unknown fields at every validated level, (4) treats sensor faults as valid record content, (5) treats `"unclear"` as an ordinary valid label, (6) returns a valid record only when all rules pass, (7) does not modify its input, and (8) uses no network, file system, database, clock, calibration or hardware access. It remains a pure validation component.
- **Still open:** R-1.
- **Alternatives considered:** accept with warnings; mark offending readings invalid; ignore or preserve unknown fields.

### Items that remain open in the frozen contracts
These decisions do not resolve them: R-1, R-2, R-3, R-4, R-6, R-7, R-8, R-9, R-10, R-11, R-12, R-13, R-14, R-16, R-17, R-18, every W- item, and every A- item. The frozen contracts are unchanged and still list them as open. Items not named here, such as R-5 and R-15, are also still open in the frozen documents.

## Simulator Implementation Decisions: Mock Rover + Telemetry (MR-1 to MR-6)

**Status of all six entries: Accepted.** Each is an **Owner-approved simulator implementation decision**, approved on 2026-10-07 after a focused review (first recorded as Proposed the same day). They were made while building the Nelavi mock rover because the frozen sources are open on these points. They are local simulator implementation decisions and apply to the simulator only. **Frozen contracts remain unchanged; their underlying open items remain open.** Nothing here resolves a frozen open item (for example W-3, W-5, A-5, A-6, R-2, R-3, R-5 or R-12) or describes the real rover's behavior. Each entry separates the SOURCE REQUIREMENT / PHYSICAL REFERENCE from the SIMULATOR IMPLEMENTATION DECISION.

**Identifiers.** `MR-1` to `MR-6` follow the same smallest-numbering approach as `MD-n` above and are not contract IDs. The R-, W- and A- IDs are the real open-item IDs.

### MR-1: Simulator package, language and determinism
- **Status:** Accepted. Owner-approved simulator implementation decision.
- **Related open items:** none.
- **Source requirement / physical reference:** the coding overview section 9 describes the mock rover as "a laptop program that speaks the same WebSocket protocol, returns fake readings and a sample photo, and has an 'unclear' switch". The coding overview section 1 repository layout names the directory `mock-rover/`.
- **Simulator implementation decision:** a top-level `mock_rover/` package. Python 3.14.6 and standard-library-only are owner-selected implementation choices for this milestone; the frozen documents do not require them. The package is named `mock_rover` because a hyphenated directory cannot be imported as a Python package. This does not alter the frozen `mock-rover/` terminology. Simulated time only (no wall clock, no sleeping, no network). Determinism comes from an explicit seed and hash-based noise keyed by (seed, name, coordinates or time bucket), so results do not depend on call order. Movement is integrated in sub-steps of at most `time_step_s`, and simulated time is set once per `advance` call to avoid float drift.
- **Still open:** the real WebSocket protocol (W- items) is not implemented here.

### MR-2: Camera home and tilt convention
- **Status:** Accepted. Owner-approved simulator implementation decision.
- **Related open items (remain open):** A-5, A-6.
- **Source requirement / physical reference:** the build document gives the stepper "0–350°", a home switch, and the servo "60° down to 45° up". The physical home direction remains unspecified, and so does the meaning of `pan` and `tilt` in `SHOOT`.
- **Simulator implementation decision:**
  - The simulator's default pan zero is 0 degrees, and the pan zero is configurable.
  - `startup()` automatically performs a simulated homing, and panning requires the camera to be homed.
  - Tilt is an abstract simulator range. The default simulator tilt range is -60 to +45 degrees, with negative values meaning down in the simulator convention.
  - The initial simulator tilt defaults to 0.0 degrees.
  - These conventions are simulator-only. They are not physical rover requirements, they do not define the physical `SHOOT` parameters, and they do not resolve the underlying frozen ambiguity.
- **Still open:** A-5, A-6.

### MR-3: Obstacle behavior
- **Status:** Accepted. Owner-approved simulator implementation decision.
- **Related open items (remain open):** W-3, W-5.
- **Source requirement / physical reference:** an HC-SR04 stop at 25 cm is a rover-side mechanism ("the rover stops if anything is within 25 cm", build document, which also describes the sensor as measuring the "Distance ahead"). The comparison operator (W-3) and the reverse behavior (W-5) are open.
- **Simulator implementation decision:**
  - The default stop distance is 0.25 m.
  - The comparison mode is configurable, and inclusive comparison is the simulator default. The inclusive default is not a resolution of W-3.
  - Reverse motion and in-place turning are permitted by fixed simulator behavior. This is not configurable and is not a resolution of W-5.
  - When an arc turn meets the forward obstacle condition, its forward component is inhibited while rotation continues.
  - The 0.02 m and 4.0 m sensor range values are simulator and reference defaults only; they do not establish a frozen hardware requirement.
  - All of this is simulator behavior only and is not the final physical safety implementation.
- **Still open:** W-3, W-5.

### MR-4: Record values the simulator generates
- **Status:** Accepted. Owner-approved simulator implementation decision.
- **Related open items (remain open):** R-2, R-3, R-5, R-6, R-12, R-17, W-16.
- **Source requirement / physical reference:** the record items are fixed by the Record Core decisions MD-2 to MD-6. The coding overview section 9 says the mock rover returns "a sample photo" and has an "unclear" switch.
- **Simulator implementation decision:**
  - Simulator time is the string `sim:<seconds>`, where seconds are simulated elapsed seconds since the simulator started. This is for deterministic simulation and testing. It does not establish the final wall-clock or time-source behavior. R-2 remains open.
  - The sequence number starts from a configurable simulator value and increments only when a Record is successfully validated. This is simulator behavior only and does not resolve R-3.
  - The default `rover_id` is a simulator placeholder. It does not establish final rover identity or provenance. R-5 remains open.
  - Fixture captures contain no real image data. Photo IDs use the placeholder form `mock-photo-<seed>-<nnnn>`; these identifiers represent simulator and test fixtures only and not the final physical photo retrieval scheme. R-12 remains open.
  - Version strings are placeholders (R-6 remains open). Fixture labels use a `mock_*` placeholder form, plus `unclear` (R-17 remains open). An explicit "unclear" switch forces the `unclear` label.
  - Every generated Record is validated by the Record Core validator; the simulator defines no second schema.
- **Still open:** R-2, R-3, R-5, R-6, R-12, R-17, W-16.

### MR-5: Telemetry and link
- **Status:** Accepted. Owner-approved simulator implementation decision.
- **Related open items (remain open):** W-1, W-7, W-8, W-9, W-10, W-15.
- **Source requirement / physical reference:** the WebSocket message names are `drive`, `status`, `test_here` and `record`; their payloads and semantics are open.
- **Simulator implementation decision:**
  - The telemetry snapshot is simulator-internal. It is not presented as a new WebSocket `status` message.
  - Wheel commands are simulator controls and not a definition of the frozen `drive {dir, speed}` protocol.
  - `test_here` is not implemented in this milestone.
  - The connection model is limited to connection state, a configurable delay and scheduled or manual dropouts.
- **Still open:** all W- items listed above; the unresolved WebSocket open items remain open.

### MR-6: Probe and sensor faults
- **Status:** Accepted. Owner-approved simulator implementation decision.
- **Related open items (remain open):** R-7, R-13.
- **Source requirement / physical reference:**
  - Coding overview section 2: each sensor has a status flag and a failure never blocks a record.
  - Build document: the operator presses the probe pod into the soil "with a foot to a fixed 10 cm depth".
  - The owner-supplied physical-model mapping for this milestone: probe insertion is an operator action, and the 10 cm depth stop is a design reference.
- **Simulator implementation decision:**
  - The probe is retracted, inserted with depth OK, or inserted with depth not OK. A measurement needs depth OK. This probe insertion and depth model is a simulator decision based on the build document and the owner-supplied mapping.
  - An injected sensor fault makes that sensor's readings `null` and its status `"fault"`. This follows the already accepted MD-4 decision.
  - BH1750 light remains simulator telemetry only and is not added to the canonical Record Core.
  - A measurement, a camera movement and a camera capture each consume simulated time and use the simulator's testing-mode battery drain. These are simulator behaviors, not physical performance claims.
- **Still open:** R-7, R-13.
