# CropSync Project Status

Factual snapshot as of 2026-10-06. Update at each milestone. This file records state; it does not define requirements. Technical requirements live in the frozen baseline documents listed below.

## Current State

Completed and verified:

- Source/interface audit of the two source documents: complete.
- Interface contracts (record, WebSocket, arm serial) defined, verified, corrected, synchronized with the hardware/software interface document (HSI), and finally frozen (result: FREEZE APPROVED).
- Environment audit: complete (result: READY FOR SETUP).
- Git repository initialized; frozen baseline committed.
- Frozen baseline hash and line-ending verification: complete. All six hashes matched; Git detected no working-tree changes.
- Governance documentation (this file, `PROJECT_HISTORY.md`, `CHANGELOG.md`, `ARCHITECTURE_DECISIONS.md`): created, not yet committed at the time of writing.

Not done: any implementation (see Implementation Status).

## Git Baseline

| Item | Value |
|---|---|
| Branch | `master` |
| Baseline commit | `acb1daa` |
| Commit message | Initial CropSync frozen baseline |
| Working tree at baseline | clean |
| Commits at baseline | 1 |
| Git identity | existing global identity, unchanged |

The baseline commit contains exactly seven files: `.gitignore` and the six frozen documents.

## Frozen Baseline

Authority order: (1) the two source documents, (2) the contracts, (3) the HSI. The HSI must agree with the sources and contracts. The six documents are frozen and are not to be modified.

| Document | SHA-256 (verified) |
|---|---|
| `docs/cropsync_coding_overview.docx` | `88D7568939057F3C10202EB1CA09443F86B466736230DBD7C151DE99CB4FBC02` |
| `docs/CROPSYNC_Rover_Build_and_Parts.docx` | `1E3FC466AC273E3129E622E00386613430AD18992FBF85581F7C3B770E326DCA` |
| `docs/HARDWARE_SOFTWARE_INTERFACE.md` | `4A8B29620A2A7300FC5601F3079DCE5728EF520C10E5A8DE169CD481D7FB6AA9` |
| `docs/contracts/RECORD_CONTRACT.md` | `2950EAB0C35BAF93E7FA98D1275AB960E40711329F17283316A8E97076464913` |
| `docs/contracts/WEBSOCKET_CONTRACT.md` | `CFEB91854479447C6143FC8456C6D1962FC6A2FEE24FCF38D20DDA05C3FD710C` |
| `docs/contracts/ARM_SERIAL_CONTRACT.md` | `CC8F46A1FC213BE334E6670C2F852434AFCA0A0CD6E89B98185F863A8027EC1E` |

## Environment (verified in the environment audit)

- Project root: `C:\Users\Akshaj dasam\OneDrive\Documents\CROPSYNC` (inside OneDrive; kept there by decision, no OneDrive problem established).
- Git 2.55.0.windows.4, Python 3.14.6, Node.js v24.19.0, npm 11.17.0: present.
- Docker: not installed.
- Other tools (compilers, other package managers, WSL, database clients, serial/USB tooling): not checked.
- Git line-ending setting: `core.autocrlf=true` (system config); `core.eol` unset. The working-tree files were LF and unchanged at verification.

## Technical Unknowns / Open Items

These are unresolved. They must not be silently guessed during implementation. They are recorded in the baseline documents under the IDs below; this file does not restate their detail.

**Source ambiguities** (documented in the contracts and HSI):
1. Drive direction wording (W-1)
2. 25 cm obstacle boundary wording (W-3)
3. 9.9 V battery cut-off boundary wording (W-4)
4. "Turns the arm" versus "tells the camera board to turn" (A-6)
5. `SHOOT` carries `pan` despite unclear physical pan ownership (A-5, A-6)
6. Photo retrieval without a documented storage or transfer path (W-12, A-12)
7. Record versions without defined provenance (R-6)
8. Record time without a defined time source (R-2)
9. Meaning of "phone confirms upload" (W-11)
10. `LEAF` confidence scale (A-10, R-9)
11. Scope of "unclear" (A-11, R-10)
12. Handling of three-photo disagreement (A-11, R-11)
13. Capture mode selection (A-14)
14. Mock rover photo and "unclear" behaviour (W-16)
15. Sequence-number relationships (R-3, R-4, A-3, W-17)

**Open decisions:** `RECORD_CONTRACT.md` R-1 to R-18, `WEBSOCKET_CONTRACT.md` W-1 to W-18, `ARM_SERIAL_CONTRACT.md` A-1 to A-15.

**Hardware / interface unknowns:** HSI section M (GPIO maps, motor-driver wiring and what D19 drives, arm-cable wire assignment, serial pins and parameters, checksum algorithm, NPK Modbus settings, power distribution, and others).

**Other established items:**
- Non-blocking findings reported at the freeze audit (for example an inexact quotation in `WEBSOCKET_CONTRACT.md` item 3) were not applied. The frozen documents are unchanged. The audit report itself is not stored in this repository.
- Line-ending handling for the repository has not been decided (see `ARCHITECTURE_DECISIONS.md`).

## Implementation Status

**IMPLEMENTATION NOT STARTED.**

No source code, tests, dependency manifests, build files, containers, simulator or hardware-control code exist. The repository contains the frozen baseline, `.gitignore`, and these governance documents only.

## Immediate Next Step

Commit the governance foundation, then proceed through controlled implementation steps following:

UNDERSTAND -> PLAN -> IMPLEMENT -> TEST -> REVIEW -> FIX -> RETEST -> VERIFY -> RECORD

No implementation capability, stack selection or toolchain setup is claimed here beyond what the source documents themselves state.
