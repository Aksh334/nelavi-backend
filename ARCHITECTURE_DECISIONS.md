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
