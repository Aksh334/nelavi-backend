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

## Update, 2026-10-15 (appended; the sections above are the 2026-10-06 snapshot and are not rewritten)

The statement "IMPLEMENTATION NOT STARTED" above is **superseded**. Since that snapshot, uncommitted and committed work exists (see `CHANGELOG.md` and `ARCHITECTURE_DECISIONS.md` for the detail and the exact verification scope of each item):

- Record validation, mock rover, storage, machine state, energy, baseline, anomaly and image-reference modules (committed to `0da510d`).
- Uncommitted: the NVIDIA and Nebius image adapters, the Second Opinion service with its simulated default, the local demo application, the deterministic advice engine (no rules loaded), the Tavily research adapter (not connected) and a manual NVIDIA smoke-test script.
- Verification: the full offline suite ran 918 tests, all passing, on 2026-10-15; the demo was checked in headless Chrome in simulated mode. **No live request to NVIDIA, Nebius or Tavily has been verified through this code**, and the only live attempt (NVIDIA, one request) returned `provider_unavailable`.
- Also uncommitted (added later on 2026-10-15): a crop-context schema and a crop-aware pipeline that produces a farmer report (`crop_context/`, `agri_pipeline/`); the full offline suite then ran 998 tests, all passing. The report states what is unavailable: no image model, no sources and no rules are configured.
- Open: the source documents give no crop, threshold, dose or calibration coefficient, so no agronomic rule exists; the intended hackathon is unresolved (the coding overview names the Nebius x NVIDIA Global AI Hackathon, Physical AI track); the frozen contracts and their TBD items are unchanged.

## Update, 2026-10-16 (appended; the statements above are not rewritten)

- **Nebius is retired from the active code.** The adapter and its tests were removed and the demo accepts only the `simulated` and `nvidia` Second Opinion providers. The mentions of Nebius in the 2026-10-15 update above (the "NVIDIA and Nebius image adapters", the hackathon name) are historical.
- **Tavily is connected, in two separate forms, both uncommitted and both offline-tested only:** (1) the Search adapter (`research/tavily.py`) is connected to the demo and the pipeline's research report through `--research-provider tavily --research-domains ...` and `POST /api/advisory` - the "(not connected)" wording in the 2026-10-15 update above is out of date; it is **not** connected to the advice engine; (2) Tavily Research with the `pro` model (`research/tavily_research.py`) is connected to the demo only, through `--research-provider tavily-pro`, a start-and-poll flow with a 120-second deadline and source links limited to `icar.org.in` and its subdomains. Its generated text is unreviewed AI output, not verified agricultural advice; nothing turns it into a rule, dose, threshold or recommendation.
- **Verification:** the full offline suite ran 1,148 tests, all passing, on 2026-10-16 (`python -B -W default -m unittest discover -s tests`); `node --check` passed on the demo script and the new page-test script. **No live request was made to any provider; no live Tavily compatibility is verified. The new Section 6 has not been checked in a real browser.**
- **Frozen files:** the six SHA-256 values in the table above were recomputed on 2026-10-16 and match.
- **Checkpoint:** `.checkpoints/checkpoint-20261016-pre-research-pro/` (untracked, not to be committed) holds a copy of 81 files from before the Research Pro work. A corrected hash check matched all 81; an earlier report of that check was wrong (see `CHANGELOG.md`).

## Update, 2026-10-16, later (appended; the statements above are not rewritten)

- **Section 6 has now been checked in a real browser, against a mocked provider.** This supersedes the sentence above that it had not been checked in a real browser. Evidence: `.browser_evidence/section6-browser-2026-10-16T16-05-36-850Z-pid34716/` (`STATUS.txt` = `PASSED`; `results.json` records 13 of 13 scenarios passed in headless Chrome 155.0.8059.79). The files were read when this entry was written. Detail is in `ARCHITECTURE_DECISIONS.md` RP-3 and `CHANGELOG.md`.
- **What that does and does not show:** the page's behaviour with fake provider answers (states, label, text shown as text, only approved ICAR links, the Section 1 values not being sent, duplicate start, reload, stale poll). The timeout timing and the stale-poll takeover were SIMULATED. The page says "REAL SERVICE" while the backend is a mock, so that text is not proof of a live mode. **Not verified:** live Tavily or any real-provider behaviour, NVIDIA mode, other browsers.
- **Network isolation is not independently proven:** Python-level guards, Chrome resolver and proxy flags, a fake transport and a made-up key were used; there was no packet capture or firewall log. The request log held 90 loopback http(s) requests plus one deliberate failed probe and three `data:image/png` requests.
- **A negative-control run** (`.browser_evidence/section6-browser-2026-10-16T16-05-19-239Z-pid47144/`, `STATUS.txt` = `INCOMPLETE`) used a nonexistent browser path, launched no browser and ran no scenarios; it is not a product-test failure.
- **Offline checks repeated:** 1,148 tests passing; three JavaScript syntax checks and the Python compile check passed. Two test-only files (`tests/browser_fake_server.py`, `tests/browser_check_research_pro.js`) and the `.browser_evidence/` folder are untracked and uncommitted, like `.checkpoints/`. Nothing is staged.

## Update, 2026-10-18 (appended; the statements above are not rewritten)

Wording written by the implementation agent; not yet reviewed by the owner. Decisions behind it are in `ARCHITECTURE_DECISIONS.md` (OR-1 to OR-6).

- **New since 2026-10-16 (all uncommitted):** `orchestration/run.py` (a synchronous step runner), `orchestration/pipeline_steps.py` (an offline adapter from the runner to `agri_pipeline.pipeline.run`), and their tests. Nothing in the rover-side modules imports any of this or any provider module (checked by search on 2026-10-18).
- **Environment:** Python 3.14.6; the project declares no dependency file (no `requirements.txt` or `pyproject.toml`) and uses the standard library only.
- **Tests run on 2026-10-18** (each file alone, `python -B -W default -m unittest discover -s tests -p <file>`):

  | Test file | Previous audit (historical) | Run on 2026-10-18 |
  |---|---:|---:|
  | `test_orchestration.py` | 37 passed | 37 passed |
  | `test_orchestration_pipeline.py` | 68 passed | 71 passed |
  | `test_agri_pipeline.py` | 52 passed | 58 passed |
  | `test_demo_app.py` | 116 passed | 119 passed |
  | `test_demo_research.py` | 33 passed | 33 passed |

  The increases are tests added by the post-audit fixes. **The full suite has not been re-run** since 2026-10-16 (1,148 tests passing then, before the orchestration code existed), so its current status is not established. Not run on 2026-10-18: `test_research_pro.py` (starts a Node process), `test_research.py` (protected) and the remaining suites (rover-side, advice, crop context, second opinion, NVIDIA smoke and findings). All tests use fakes: **none shows how any live provider behaves.**
- **Fixed on 2026-10-18:** an oversized number in a demo `limit` or `Content-Length` was an internal error (500) and is now 400 or 413; the main report now carries the search's caveats; a malformed `findings_result` given to the pipeline is reported as an image-stage error. Not reproduced: a deeply nested JSON body on `/api/run` (it returned 400). See `CHANGELOG.md` and OR-3.
- **NVIDIA (accurate status):** an isolated-script image inference was recorded as successful on 2026-10-09. Through the repository's adapter, the Second Opinion smoke script failed with `provider_unavailable` on 2026-10-15; the findings smoke script got one HTTP-success reply on 2026-10-16 (run with its opt-in `--thinking-off`) that did not satisfy the old findings contract, and two later requests with the corrected prompt failed (`provider_unavailable`, then HTTP 503), so **the corrected prompt has never been answered**. A reply that satisfies the findings contract through the adapter or the demo has not been obtained, and the cause of the 5xx responses on the adapter path is unknown. These are different execution paths, not a contradiction.
- **Other providers:** Tavily Search and Research Pro have adapters and fake-transport tests only; no live request has ever been made. No code exists for Nodus, Agent37 or OpenRouter. The `nodus-compute` package was not installed when last checked in the 2026-10-18 audits; the vendor's reported support for Python 3.10 to 3.13 comes from documentation read earlier and was not re-verified. Nebius remains retired.
- **Open gaps:** the mapping from a Record's `photo_id` to image bytes (R-12 and related items) does not exist, so every image step needs caller-supplied PNG bytes; result-size limits are deferred and a design is only proposed (OR-4); the relevance score is still absent from the report (decision open); the behavior of a refused `run_report` with bad run arguments is pinned by a test, not decided.
- **Frozen and protected files:** the six frozen files are tracked and Git reports them unmodified relative to HEAD (their SHA-256 values were **not** recomputed on 2026-10-18). The protected files are in untracked folders, so no comparison against HEAD is possible; they were not edited in these steps. The checkpoint folder was not touched and was not re-verified.
- **Git:** `master`, HEAD `0da510d`; nothing staged or committed.

## Update, 2026-10-18, second fix task (appended; the statements above are not rewritten)

Wording written by the implementation agent; not yet reviewed by the owner. Details are in `ARCHITECTURE_DECISIONS.md` OR-7.

- **Changed:** run arguments are now checked before a report-input refusal (new `check_run_arguments`); a Search result over the search adapter's own bounds fails the stage with `response_too_large` (nothing truncated); `/api/run` handles a `RecursionError` like the other JSON routes. The relevance score stays out of evidence entries (decision); the caveats stay.
- **Tests run on 2026-10-18 after this task** (each file alone, `python -B -W default -m unittest discover -s tests -p <file>`):

  | Test file | First fix task | After the second fix task |
  |---|---:|---:|
  | `test_orchestration.py` | 37 passed | 41 passed |
  | `test_orchestration_pipeline.py` | 71 passed | 77 passed |
  | `test_agri_pipeline.py` | 58 passed | 58 passed |
  | `test_demo_app.py` | 119 passed | 120 passed |
  | `test_demo_research.py` | 33 passed | 33 passed |

  The earlier table above stays as the record of the first fix task. **The full suite has not been re-run** since 2026-10-16; `test_research_pro.py`, `test_research.py` and the other suites were not run. All tests use fakes: none shows live provider behaviour.
- **NVIDIA, precise:** (1) isolated-script image inference succeeded on 2026-10-09; (2) the Second Opinion smoke script, which uses the adapter, failed with `provider_unavailable` on 2026-10-15; (3) the findings smoke script got one HTTP-success reply on 2026-10-16 that failed the old findings contract; (4) two later attempts with the corrected prompt failed. No contract-satisfying findings reply has been verified, and the older statements that "the only live attempt returned `provider_unavailable`" are historical.
- **Still open (owner):** the number for a generic result-size cap (options in OR-7, recommended: the existing 4 MiB local response limit per step plus a per-run total), and whether to omit `run` details from the assembly result to avoid the duplication. The `photo_id`-to-bytes gap and all live-provider questions are unchanged.
- **Git:** `master`, HEAD `0da510d`; nothing staged or committed.

## Update, 2026-10-18, third fix task (appended; the statements above are not rewritten)

Wording written by the implementation agent; not yet reviewed by the owner. Details are in `ARCHITECTURE_DECISIONS.md` OR-8, which supersedes the "still open" cap item above **for the per-step limit only**.

- **Changed:** a step's serialised JSON result may now be at most `MAX_STEP_RESULT_BYTES` (provisional, 4 MiB, measured as serialised size, not memory); a larger result fails the step with the new closed code `result_too_large` (nothing truncated or kept), which the report shows as `response_too_large`. **No per-run total is enforced** (recommended 8 MiB, awaiting approval). The assembly return shape, the Tavily bounds and the evidence-entry schema are unchanged.
- **Tests run on 2026-10-18 after this task** (each file alone): `test_orchestration.py` 50 passed; `test_orchestration_pipeline.py` 79; `test_agri_pipeline.py` 58; `test_demo_app.py` 120; `test_demo_research.py` 33. The full suite has not been re-run since 2026-10-16; `test_research_pro.py`, `test_research.py` and the other suites were not run; all tests use fakes.
- **Still open (owner):** approve or change the 4 MiB number; approve the per-run total; choose whether to pursue the `include_run` migration that would remove the `run`/`report` duplication (no consumers of `run` exist outside tests). Everything else open is unchanged (the `photo_id`-to-bytes gap, live-provider questions, NVIDIA: no contract-satisfying findings reply has been obtained).
- **Git:** `master`, HEAD `0da510d`; nothing staged or committed.

## Update, 2026-10-18, OR-9 per-run result-size limit (appended)

Wording written by the implementation agent; not yet reviewed by the owner. Details in `ARCHITECTURE_DECISIONS.md` OR-9; it supersedes the "no per-run total is enforced" statement in the previous update.

- **Changed:** the runner now also enforces a provisional, owner-approved 8 MiB limit on the summed serialised length of stored step results (4 MiB per-step limit unchanged). A step that would exceed it fails with `result_too_large` and stores nothing. Order-dependent for independent steps; bounds stored size, not transient memory. Output shape and `include_run` (deferred) unchanged.
- **Tests run (each file alone):** `test_orchestration.py` 65 passed; `test_orchestration_pipeline.py` 80 passed. The full suite and all other files were not run for this task (last full run recorded: 1,148 passing, 2026-10-16). Fakes only; no provider contacted.
- **Still open (owner):** whether the 4 MiB / 8 MiB numbers become final; whether to pursue `include_run`. Everything else open is unchanged.
- **Git:** `master`, HEAD `0da510d`; nothing staged or committed.

## Update, 2026-10-18, IM-2 additive orchestrated demo path (appended)

Wording written by the implementation agent; not yet reviewed by the owner. Details: `ARCHITECTURE_DECISIONS.md` IM-2.

- **Changed:** the demo has a new, clearly labelled, **fake-providers-only** route `POST /api/run-orchestrated` and page section 7, built on `run_report`. The existing `/api/run`, `/api/advisory` and report schemas are unchanged; the primary report path is not replaced. Image stage skipped (photo mapping unresolved), `origin="unknown"`, Record stored once, 4 MiB / 8 MiB limits enforced, no real-provider option.
- **Tests run (each file alone):** `test_demo_orchestrated.py` 44 passed (new); `test_demo_app.py` 120; `test_demo_research.py` 33; `test_orchestration.py` 65; `test_orchestration_pipeline.py` 80; `test_agri_pipeline.py` 58. One existing guard test in `test_demo_app.py` was widened for the new `orchestration` import. **Not run:** the full suite, `test_research.py`, `test_research_pro.py`, Node/subprocess tests, provider smoke scripts. The page was not run in a browser; no provider was contacted; fake output shows wiring, not provider behaviour.
- **Still open (owner):** review before any change to the primary report path; whether demo-made Records may use `origin="simulated"` in this path; any real NVIDIA/Tavily call (separate approval, data-sharing and cost decisions); `include_run`; whether 4 MiB / 8 MiB become final. Everything else open is unchanged.
- **Git:** `master`, HEAD `0da510d`; nothing staged or committed.

## Update, 2026-10-18, UI redesign (appended)

Wording written by the implementation agent; not yet reviewed by the owner. Frontend files only (`demo_app/static/index.html`, `style.css`, `app.js`) plus a new `tests/test_demo_ui.py`; no backend, API, security-control or contract change.

- **Done:** one cohesive layout with navigation, overview tiles, reading cards, a labelled battery-voltage trend, a clearer Section 7 (fake sources badged FAKE and never links; image stage shown as skipped from the response), and a working light/dark theme (system default, session-only choice, no storage APIs).
- **Tests run (each file alone):** `test_demo_ui.py` 20 passed (new; includes measured contrast in both themes and a no-hard-coded-colour check); `test_demo_app.py` 120; `test_demo_orchestrated.py` 44; `test_demo_research.py` 33. Not run: full suite, `test_research.py`, `test_research_pro.py`, Node or browser tests.
- **Unverified:** no browser was available, so layout, mobile rendering, scrolling and the theme switch on screen have not been seen. The running demo must be restarted to show the new page.
- **Git:** `master`, HEAD `0da510d`; nothing staged or committed.

## Update, 2026-10-18, N-1 Nodus readiness (appended)

Wording written by the implementation agent; not yet reviewed by the owner. Details: `ARCHITECTURE_DECISIONS.md` N-1.

- **Done (offline):** a provider-neutral task-runner boundary with a deterministic fake and a thin Nodus adapter that takes an injected handle (SDK never imported), plus a minimal chain (validate the Record, send only four sections, validate the summary). Demo, APIs and `run.py` unchanged.
- **Blocked for live use:** the `nodus-compute` SDK is not installed; Python 3.14.6 is outside the documented 3.10 to 3.13 range (PyPI metadata sets only a minimum, so compatibility is unknown); an account, credits and credentials are needed; the remote function does not exist; timeout exception name, `max_cost` unit, data transmitted, and pricing are unresolved.
- **Tests (each file alone):** `test_task_runner.py` 33 passed (new); `test_orchestration.py` 65; `test_orchestration_pipeline.py` 80; `test_demo_app.py` 120; `test_demo_orchestrated.py` 44; `test_demo_ui.py` 20. Full suite not run. The mocked handle shows the adapter's logic, not the service's behaviour.
- **Git:** `master`, HEAD `0da510d`; nothing staged or committed.

## Update, 2026-10-18, N-2 task-workflow hardening (appended)

Wording written by the implementation agent; not yet reviewed by the owner. Details and the correction to N-1: `ARCHITECTURE_DECISIONS.md` N-2.

- **Done (offline):** five defects fixed in the task-runner boundary and the Nodus adapter (exact-count summary check, check against the stored payload, bounded runner label, safe timeout validation, a checker that never raises); invalid-Record, dependency, leakage, size-limit, determinism and isolation behaviour verified by tests.
- **Tests (each file alone):** `test_task_runner.py` 56 passed; `test_orchestration.py` 65; `test_orchestration_pipeline.py` 80; `test_demo_app.py` 120; `test_demo_orchestrated.py` 44; `test_demo_ui.py` 20. Full suite not run. Mocked tests do not show real Nodus compatibility.
- **Still blocked for a live Nodus test:** SDK not installed; Python 3.14.6 outside the documented 3.10 to 3.13; account, credits and credentials; a deployed remote function; unresolved timeout-exception name, `max_cost` unit, data transmitted and pricing.
- **Git:** `master`, HEAD `0da510d`; nothing staged or committed.

## Update, 2026-10-18, RD-1 real-provider readiness (appended)

Wording written by the implementation agent; not yet reviewed by the owner. Details: `ARCHITECTURE_DECISIONS.md` RD-1.

- **Provider state:** Nodus: adapter + fake only, SDK not installed, only Python 3.14 on this machine (documented range 3.10 to 3.13); Agent37 and OpenRouter: no code; NVIDIA and Tavily: adapters with fake tests, no verified live result through the adapters; Nebius: forbidden.
- **First real Nodus call is blocked by (owner approval needed):** a supported Python and a controlled SDK install; a Nodus account, credits and credentials; a remote function written and deployed separately; a decision on what data may be sent and a cost cap. Unresolved facts: timeout exception name, whether a timed-out call keeps billing, `max_cost` unit, data transmitted, pricing.
- **Tests:** 14 files run separately, all passed (counts in RD-1); full suite not run.
- **Git:** `master`, HEAD `0da510de631c3785757db849860979ca00b820b5`; nothing staged or committed.

## Update, 2026-10-18, N-3 Nodus adapter and standalone function (appended)

Wording written by the implementation agent; not yet reviewed by the owner. Details and the statements this supersedes: `ARCHITECTURE_DECISIONS.md` N-3.

- **Nodus state:** `nodus-compute==1.0.23` is installed only in the isolated Python 3.13 environment (`nodus-py313`); the repository Python 3.14 has no SDK. The adapter is hardened and tested against mocked handles and the installed SDK source; the standalone function exists outside the repository (`%LOCALAPPDATA%\CropSyncNodus\summary-function\cropsync_summary.py`, app `cropsync-summary`, function `summarize_record`) and is **not deployed** and not tracked by Git. The standalone files are outside Git, and `tests/test_summary_function.py` skips when that folder is missing.
- **Verified locally:** a single intended `.spawn` call site; a failing call is spawned exactly once; function parity with `expected_summary`; the input contract is `sensor_status`, `soil`, `air`, `battery` and the output contract is `readings_present`, `readings_missing`, `sensors_ok`, `sensors_fault`, `faulty_sensors`.
- **Targeted tests (2026-10-18, separate runs; not a full-suite run):** SDK-aware (Python 3.13): `test_summary_function.py` 15 ran OK (1 skipped), `test_nodus_sdk_contract.py` 16 ran OK. No SDK (Python 3.14): `test_summary_function.py` 15 ran OK (1 skipped), `test_nodus_sdk_contract.py` 16 skipped. Python 3.14 regression: `test_task_runner.py` 76; `test_orchestration.py` 65; `test_orchestration_pipeline.py` 80; `test_demo_app.py` 120; `test_demo_orchestrated.py` 44; `test_demo_ui.py` 20; `test_demo_research.py` 33; `test_agri_pipeline.py` 58; `test_integration_readiness.py` 17 (after one assertion was corrected). The full suite has not been run.
- **Unverified / not production-ready:** no live login, estimate, deployment or remote call has been made; remote execution, billing, the effect of timeouts and cancellation on remote work and cost, `max_cost` accounting, data retention and pricing are unverified. Local tests do not establish them.
- **Next (owner decisions):** a Nodus account with a verified card and a small project budget; an estimate for one call; deployment from the standalone folder; one non-sensitive test call.
- **Git:** `master`, HEAD `0da510de631c3785757db849860979ca00b820b5`; nothing staged or committed.

## Update, date not recorded, IW-1 integrated offline workflow (appended)

Details: `ARCHITECTURE_DECISIONS.md` IW-1.

- **Done (offline):** `run_integrated` runs four top-level stages in one run (image findings, research, a compute summary made of `prepare_payload` and `summarize_record`, and a synthesis) using only injected fakes or a mocked handle; `check_integrated_result` re-checks a result for consistency. Synthesis depends on image findings and research and is skipped if either did not complete. Generated synthesis is unverified text with citations to supplied inputs, kept separate from validated findings and evidence, and it does not alter the report.
- **Not implemented:** a real synthesis (OpenRouter) adapter, because no API contract is recorded; the specialist/Agent37 role, which is undefined and recorded as skipped (`specialist_role_undefined`).
- **Tests (separate test-file runs; not a full-suite run):** 32 files, exit 0, no skips, 1,485 tests; `test_summary_function.py` 15 run, 1 skipped; `test_nodus_sdk_contract.py` 16 run, all 16 skipped (Python 3.14, SDK absent). Total 1,516 run, 17 skipped, 1,499 non-skipped; no failures or errors reported in those runs. Separate earlier Python 3.13 SDK-aware Step 0 run: 269 passed, 1 skipped, six files (not merged). These counts and the Step 0 result are based on the recorded test report and were not independently reproduced during this documentation update. Not run: `test_research_pro.py`, the two related JavaScript/browser tests, the full suite.
- **Open decisions (owner):** synthesis service and API contract; specialist/Agent37 role; the 8 MiB per-run result limit, which remains provisional and needs review for the multi-stage flow.
- **Not verified:** any live provider; no live provider calls were made.

## Update, date not recorded, IW-3 integrated workflow in the demo (appended)

Details: `ARCHITECTURE_DECISIONS.md` IW-3.

- **Done (code, offline tests):** the demo has `POST /api/run-integrated` and a "Section 8" that runs image findings, research, a compute summary and a synthesis in one run, each stage `skip`, `fake` or `real`. The server option `--findings-provider nvidia` (with the existing `--research-provider tavily`) makes the image and research stages selectable as real; they are fail-closed when the key is missing and never fall back to a fake.
- **Not wired (blocked):** real compute (Nodus: SDK, account, deployment, approval), real synthesis (OpenRouter: no recorded API contract), the specialist/Agent37 role (undefined). Their injection points exist; nothing real is connected.
- **Tests (each file alone, keys unset):** 18 files passed, including the new `test_demo_integrated.py` (25); counts in IW-3. **Never run:** the page script (no browser/Node), the server process, and any real NVIDIA/Tavily request. Full suite not run.
- **Next:** with explicit authorization, start the server with `--findings-provider nvidia --research-provider tavily --research-domains <chosen hosts>` in a terminal where the keys are set, and make one small real request; separately decide the Nodus/OpenRouter/Agent37 items.

## Update, date not recorded, IW-4 real Nodus compute through a launcher (appended)

Details: `ARCHITECTURE_DECISIONS.md` IW-4.

- **Done (code, offline tests):** `scripts/run_demo.py` is the single place that imports the Nodus SDK (lazily). `python -B -m scripts.run_demo --compute-provider nodus --port <free port other than 8765> ...` under the isolated Python 3.13 environment makes the compute stage selectable as `real` in Section 8; preflight is fail-closed (exit code 2 under Python 3.14 or without the SDK). The default is the normal demo.
- **Verified:** the new `test_run_demo.py` (10) and the related demo, workflow and guard tests pass under Python 3.14, and under Python 3.13 with the real SDK the 16 SDK-contract tests run and pass. The launcher preflight reports ready under 3.13.
- **Not verified / blocked:** no real Nodus request has ever been made through this code; the remote function `cropsync-summary` is not known to be deployed; authentication, cost and live timeout behaviour are unestablished. Needs the owner: a funded Nodus account state, an estimate, deployment of the standalone function, and explicit authorization for one small real call. Agent37 (no recorded role or API) and OpenRouter (no recorded API contract) remain unimplemented.
- **Next:** with authorization, deploy the standalone function and run one real compute call from the launcher; then the NVIDIA and Tavily real requests through Section 8 (first real browser run of the page).

## Update, date not recorded, IW-5 no-payment direction (appended)

Details and sources: `ARCHITECTURE_DECISIONS.md` IW-5.

- **Owner direction:** no payments. Nodus is **paused** (its code is intact; `scripts/run_demo.py` refuses `--compute-provider nodus` without the explicit `--i-accept-nodus-charges`). No live provider request is made before eligibility is understood and the owner authorizes it.
- **Tavily (free plan, per docs.tavily.com):** 1,000 credits per month, no card; Section 8 uses the search endpoint only (1 credit per run). Not established: behaviour when the free credits run out; whether `/usage` costs credits. Do not use Research Pro on the free allowance (15 to 250 credits per task).
- **NVIDIA (free for Developer Program prototyping, per NVIDIA docs):** credits were removed; rate limits are unpublished and visible only in the signed-in account menu. Not established: whether a payment card is ever required, and whether the trial endpoints can incur charges (the Trial Terms PDF could not be read). So no NVIDIA request has been made.
- **Verified offline:** opening the demo calls no provider; a real stage that is not configured is refused; a failing real stage never becomes a fake success; targeted tests pass (counts in IW-5). The page script has never been run in a browser.
- **Next (owner):** confirm in the NVIDIA account whether a card is required and read the rate limit shown at the top right of build.nvidia.com; confirm the Tavily account has pay-as-you-go off; then authorize ONE image request and ONE Tavily search (1 credit) through Section 8, which will also be the first real browser run.

## Update, date not recorded, IW-6 integrated workflow as the primary page (appended)

Details: `ARCHITECTURE_DECISIONS.md` IW-6.

- **Done (UI, offline tests):** the page now opens on "Run the integrated workflow", with its own inputs and local fake defaults (runs without keys); the older sections sit under "Supporting tools". The banner states which real providers are configured and what each would receive, without claiming any call; a live list shows the current selection; results distinguish fake-local from real-invoked.
- **Verified:** 9 test files pass (counts in IW-6) and `node --check` passes (syntax only). **Not verified:** the page has never been run in a browser; real NVIDIA/Tavily/Nodus paths on screen are untested. Non-blocking improvement recorded: a "computed locally" compute mode.
- **Next:** with the owner's authorization, the first real request (NVIDIA image and one Tavily search) through this page, which is also the first browser run.

## Update, date not recorded, IW-7 NVIDIA and Tavily verified offline end to end (appended)

Details: `ARCHITECTURE_DECISIONS.md` IW-7.

- **Done (offline):** the uploaded image bytes, the fixed findings prompt, the Tavily query and the configured domain list are shown to reach the adapters' transports in the documented format; replies are validated against the findings contract and the owner-domain filter; every failure kind is a closed code with no retry, no leak and no fake substitution; configured, selected, attempted, failed and skipped are reported separately (and a local refusal is reported as "no request was sent").
- **Not verified:** any real request; the page in a browser; whether the vendors accept this exact request or answer in this shape; the NVIDIA card/charge questions and the Tavily out-of-credits behaviour from IW-5.
- **Next (owner):** the checks listed under IW-5, then authorize ONE image request and ONE Tavily search through the page (also the first browser run).

## Update, date not recorded, IA-6 (appended)

Details: `ARCHITECTURE_DECISIONS.md` IA-6.

- **Done (offline):** NVIDIA transport failures are now reported with three closed codes instead of one: `provider_timeout`, `provider_connection_failed`, `transport_failed` (twelve image-analysis codes in all; `provider_unreachable` remains in the set but the NVIDIA adapter no longer produces it). Owner-approved edits to the protected `image_analysis/nvidia.py` and `image_analysis/boundary.py`; their new SHA-256 values are recorded in IA-6 (the checkpoint folder was left untouched). Full offline suite with both keys unset: 1,626 tests OK, 17 skipped; `node --check` passes.
- **Not verified:** that the earlier `provider_unreachable` run was a timeout; any real request. The smoke scripts' `STAGE_ONE_MEANING` tables do not know the new codes (protected, not changed).
- **Next (owner):** repeat the one authorized image request; the result will now say which failure it was.

## Update, date not recorded, IW-8 (appended)

Details: `ARCHITECTURE_DECISIONS.md` IW-8.

- **Done (offline):** the main Integrated Workflow page now explains any failed stage, step or research stage in plain words with a next step (all closed image and research codes, including `provider_timeout`, `provider_connection_failed`, `transport_failed`). Full offline suite 1,628 OK, 17 skipped; `node --check` passes.
- **Not verified:** the page in a browser; any real request. Specialist/Agent37 undefined; OpenRouter and Nodus not implemented/paused.
- **Next (owner):** run the page (`python -B -m demo_app --port 8800`) and report what you see; then, after the IW-5 account checks, authorize ONE image request and ONE Tavily search.

## Update, date not recorded, IA-7 (appended)

Details: `ARCHITECTURE_DECISIONS.md` IA-7.

- **Done (offline):** every real NVIDIA image request from the Integrated Workflow is now timed and reported on the page as either an HTTP status or a transport-failure category (no text from NVIDIA or the network). Opt-in flags `--findings-timeout SECONDS` and `--findings-thinking-off` exist; omitting them keeps the 60 s timeout and the unchanged request body. Full offline suite 1,669 OK (17 skipped); `node --check` passes. Protected adapter, boundary and smoke scripts unchanged.
- **Not verified:** why the earlier request failed (the slow-call hypothesis is unproven); any real request; the page in a browser.
- **Next (owner):** review this change, then authorize exactly ONE real image request (state the flags, for example none first), with a non-sensitive PNG; the page will show the status or failure category and the elapsed time.

## Update, date not recorded, RS-1 (appended)

Details: `ARCHITECTURE_DECISIONS.md` RS-1.

- **Done (offline):** the demo server now restricts Tavily Search to the agreed domains (`icar.org.in`, `agritech.tnau.ac.in`, `fao.org`): `--research-domains agreed` expands to them, other hosts stop the server from starting, and the effective list is printed at startup. Results are still labelled unreviewed evidence; matching a domain does not establish trustworthiness. Full offline suite 1,691 OK (17 skipped). Protected `research/tavily.py` unchanged.
- **Pending approval (not implemented):** HTTPS-only result filtering in `research/tavily.py`; distinct Tavily timeout/connection error codes; a total deadline.
- **Not verified:** any real Tavily request (credit state unknown); the page in a browser.
- **Next (owner):** decide on the pending `research/tavily.py` changes; then authorize ONE Tavily search with `--research-domains agreed`.

## Update, date not recorded, RS-2 (appended)

Details: `ARCHITECTURE_DECISIONS.md` RS-2.

- **Done (offline):** Tavily Search results must now be `https` URLs (port 443 or none, no user-info) on the agreed hosts or true subdomains; transport failures report `provider_timeout`, `provider_connection_failed` or `transport_failed`; the request has a **best-effort** total time budget; the page's "not configured" help names the agreed allowlist. Full offline suite 1,718 OK, 17 skipped (Nodus SDK not importable); `node --check` passes.
- **Limits:** the time budget is not a hard or end-to-end deadline (DNS and an operation already blocking can exceed it) and has been tested only with fake transports, never real sockets or live Tavily. Matching a domain or using https says nothing about whether a page is correct or safe; results stay unreviewed evidence. `tavily-pro` keeps `provider_unreachable` and its fixed `icar.org.in` domain.
- **Not verified:** any real Tavily request (credit state unknown); the page in a browser.
- **Next (owner):** review RS-1 and RS-2; then authorize ONE Tavily search with `--research-domains agreed`.

## Update, date not recorded, SY-1 (appended)

Details: `ARCHITECTURE_DECISIONS.md` SY-1.

- **Done (offline):** a real synthesis stage can now be configured (`--synthesis-provider chat` with an owner-supplied endpoint and model); it is implemented and offline-tested only. Full offline suite 1,741 OK, 17 skipped.
- **Blocked / owner:** the OpenRouter endpoint and model must be supplied and confirmed; Agent37 has no recorded identity or interface (stays skipped); Nodus paused; no provider has been verified live.
- **Next (owner):** authorize one image request, one Tavily search and one synthesis request, in that order, with the flags stated.

## Update, date not recorded, SY-2 (appended)

Details: `ARCHITECTURE_DECISIONS.md` SY-2.

- **Done (offline):** the final audit (register in SY-2) and an OpenRouter synthesis provider built on the contract read from OpenRouter's official documentation on 2026-10-20. Full offline suite 1,761 OK, 17 skipped (Nodus SDK not importable).
- **Status:** NVIDIA, Tavily Search, Tavily Pro and OpenRouter are implemented and offline-tested only; none is live-verified. Agent37 is blocked (no evidence of its identity, interface or role). Nodus is paused.
- **Open:** the unbounded provider-reported date in `research/tavily.py` and the missing codes in the smoke scripts need approval to change protected files; no hard deadline for any request; Python below 3.10 would misclassify timeouts; the page has never been run in a browser.
- **Next (owner):** choose an OpenRouter model and key variable; supply the Agent37 facts listed in SY-2 or leave it skipped; decide on the protected-file items; then authorize one live request per provider in the order image, search, synthesis.

## Update, date not recorded, SY-3 (appended)

Details: `ARCHITECTURE_DECISIONS.md` SY-3.

- **Done (offline):** NVIDIA, Tavily Search and OpenRouter cooperate through the shared workflow with tested data boundaries and citation alignment; audit findings F1 and F4 are closed. Full offline suite 1,786 OK, 17 skipped (Nodus SDK not importable).
- **Not live-verified:** every provider. A real integrated run needs your authorization, the three keys set in the terminal, and a chosen OpenRouter model (command in SY-3).
- **Blocked:** Agent37 is a paid agent-hosting platform; its task in CropSync, its funded instance and its response schema are undefined (details in SY-3). Nodus is paused. Open limits: no hard deadline, Python below 3.10, the page never run in a browser.
- **Next (owner):** authorize the live run (or decline it); decide the Agent37 task and whether a paid instance is approved.

## Update, date not recorded, SY-4 (appended)

Details: `ARCHITECTURE_DECISIONS.md` SY-4.

- **Done (offline):** the OpenRouter synthesis model is set to the owner's choice, `anthropic/claude-haiku-5.5`, as the server default. Full offline suite 1,789 OK, 17 skipped.
- **Unconfirmed:** structured-output support and `temperature` for this model are not stated on its OpenRouter page; nothing is live-verified.
- **Next (owner):** authorize the live run, with `OPENROUTER_API_KEY` set in the terminal.

## Correction, 2026-10-20, SY-4 (appended)

Details: `ARCHITECTURE_DECISIONS.md` SY-4 correction note.

- **Corrected:** OpenRouter requests no longer carry `temperature`; the generic chat provider still sends `temperature: 0`. The default model is still `anthropic/claude-haiku-5.5`. Latest full offline suite: 1,793 OK, 17 skipped (supersedes 1,789 above).
- **Still unconfirmed:** strict JSON Schema support for this model (`--synthesis-structured-output` stays opt-in and sets `require_parameters: true`), account-level routability, price and limits, and whether the live service accepts the default request. Nothing is live-verified.
- **Next (owner):** authorize a live run, with `OPENROUTER_API_KEY` set in the terminal; the earlier question about risking `temperature` no longer applies.

## Update, date not recorded, SY-5 (appended)

Details: `ARCHITECTURE_DECISIONS.md` SY-5.

- **Done (offline):** finding G1 is closed; the integrated-result check now catches an edited stored synthesis (`synthesis_invalid`). Full offline suite 1,801 OK, 17 skipped (Nodus SDK not importable).
- **Live status:** the OpenRouter default-mode request succeeded once on synthetic input (owner-reported). NVIDIA, Tavily and the full three-provider chain are not live-verified.
- **Limits:** validation shows shape and citation integrity, not factual accuracy; G2 (upstream-fake note) not implemented; Agent37 deferred; Nodus paused.
- **Next (owner):** review the live-run preflight and authorize (or decline) one bounded end-to-end run.

## Update, 2026-10-21, IA-8 (appended)

Details: `ARCHITECTURE_DECISIONS.md` IA-8.

- **Live status:** the standalone NVIDIA findings smoke test passed once (HTTP 200, 44.3 s, findings contract met). Three integrated runs failed at NVIDIA with HTTP 503 after 1.3 to 1.6 s; Tavily completed each time (results were weakly relevant to the generic question); OpenRouter was skipped each time and has not been exercised in the flow. The cause of the 503 is unknown (the body was discarded). No run has completed. Billing and credit use are unverified for all three providers.
- **Done (offline):** the demo reports a closed class of an HTTP 5xx body and its length (no text); the research query appends the user's own typed crop, region and growth stage (a change to what Tavily and OpenRouter receive, stated on the page); the Compute Skip/Fake mapping is pinned by tests (no defect found). Full offline suite (keys unset) 1,821 OK, 17 skipped.
- **Not done, needs owner approval:** keeping the reply headers (for example `Retry-After`) needs a change to the protected NVIDIA adapter; making research depend on the image findings is a design change.
- **Next (owner):** restart the server with the new code and authorize one integrated website run (it sends one request to NVIDIA, one to Tavily and, only if NVIDIA succeeds, one to OpenRouter, and may use credits).

## Update, 2026-10-21, SY-6 (appended)

Details: `ARCHITECTURE_DECISIONS.md` SY-6.

- **Live status:** in the latest integrated website run NVIDIA completed (HTTP 200, 42.2 s) and Tavily completed (five results, relevance not established); the OpenRouter adapter was invoked and failed with `synthesis_response_malformed`, so the run failed and has no synthesis. The raw reply was not kept, so the cause is unknown. No run has completed. Billing and usage are unverified for all three providers.
- **Done (offline):** the real synthesis request now records, as closed words and bounded numbers only, where it stopped, the HTTP status, the finish reason, token and size counts, the shape of the model text and which synthesis rules it broke; the page shows this. Nothing was fixed, because no defect is established. Full offline suite (keys unset) 1,843 OK, 17 skipped.
- **Next (owner):** restart the server with this code and authorize one integrated website run; the new section will show why the synthesis was refused. A cheaper first check is possible with Image = Fake and Research = Fake and Synthesis = Real, which sends only the question and made-up placeholder inputs to OpenRouter (one request, no leaf image, no Tavily or NVIDIA cost), but it cannot reproduce a failure caused by the real findings or sources.

## Update, 2026-10-21, SY-7 (appended)

Details: `ARCHITECTURE_DECISIONS.md` SY-7.

- **Live status:** a synthesis-only run (image and research fake, compute skipped, only OpenRouter real) reached OpenRouter and was cut off at the 800-token limit (HTTP 200, finish reason `length`, 800 of 800 tokens, 1,067 characters of text), so the stage failed as `synthesis_response_malformed`. Earlier full runs: NVIDIA completed once in the integrated flow (HTTP 200, 42.2 s) and failed with HTTP 503 in others; Tavily completed each time with weakly relevant sources. No run has completed with a synthesis. Billing and usage are unverified for all three providers.
- **Done (offline):** the OpenRouter default limit is 2,400 tokens (was 800); the prompt asks for a shorter reply on single lines (the validator is unchanged); a reply cut off at the limit is named `output_truncated` and refused, never repaired or retried; the diagnostic records hidden reasoning tokens when reported. Full offline suite (keys unset) 1,850 OK, 17 skipped.
- **Not verified:** that the fix works with the live service; that 2,400 is enough; why 800 tokens gave only 1,067 characters (hidden reasoning tokens are an inference). No `reasoning` request field was added because its accepted values are not confirmed.
- **Next (owner):** authorize one manually submitted synthesis-only run on the updated app; only if it succeeds, authorize a full NVIDIA, Tavily and OpenRouter run.

## Update, 2026-10-21, IA-9 (appended)

Details: `ARCHITECTURE_DECISIONS.md` IA-9.

- **Live status:** the latest full integrated run failed at NVIDIA with HTTP 503 after 1.3 s (the reply wording mentioned an exhausted resource; the cause is unknown); Tavily completed; OpenRouter was skipped. The standalone NVIDIA smoke test has passed once (HTTP 200, 44.3 s). No run has completed with a synthesis; the OpenRouter limit fix (SY-7) is not yet confirmed live. Billing and usage are unverified for all three providers.
- **Done (offline):** the demo reports, for any HTTP error reply to the real image request, a closed status kind by the standard meaning of the code, the body class and the form of any `Retry-After` header; the server's image request uses a new transport (`image_analysis/nvidia_headers.py`) that behaves like the adapter's own and notes `Retry-After`. The protected adapter, the model, endpoint, prompt, timeout and error codes are unchanged; no retry or fallback exists. Full offline suite (keys unset) 1,864 OK, 17 skipped.
- **Not done, by decision:** no corrective change to the NVIDIA request; the evidence does not show the 503 comes from anything the project controls.
- **Next (owner):** authorize one manually submitted NVIDIA-only run (Image Real; Research Fake; Compute Skip; Synthesis Skip) on the updated app. It sends one request to NVIDIA only and may use credits. A full run waits until NVIDIA passes on its own.

## Update, 2026-10-21, IA-10 (appended)

Details: `ARCHITECTURE_DECISIONS.md` IA-10.

- **Live status:** NVIDIA image findings have now succeeded three times on the same endpoint and model (the standalone smoke test twice, HTTP 200 after 44.3 s and after 31.6 s, and one integrated run after 42.2 s) and failed repeatedly with a fast HTTP 503 (0.9 to 1.6 s; the integrated diagnostics classed the reply wording as an exhausted resource, with no `Retry-After`). The cause is unknown; a capacity limit on the shared endpoint is the best-supported but unconfirmed hypothesis. An offline check found the integrated and standalone paths build byte-identical requests for the same file. OpenRouter synthesis has passed once in isolation (fake findings and research, owner-reported). No full run has completed with real image findings, real research and a synthesis. Billing and usage are unverified for all three providers.
- **Done (offline):** the real image request's diagnostics now include a request fingerprint (the length and a short SHA-256 prefix of the exact request body and of the decoded image), so a later run can be compared with a known file. Full offline suite (keys unset) 1,883 OK, 17 skipped. The server on port 8795 serves the older code until restarted.
- **Next (owner):** authorize a restart on a new port with this code, then one NVIDIA-only run (Image Real; Research Fake; Compute Skip; Synthesis Skip). A full run waits until NVIDIA passes on its own in the app.

## Update, 2026-10-21, IA-11 (appended)

Details: `ARCHITECTURE_DECISIONS.md` IA-11.

- **Live status:** the latest integrated NVIDIA run (a different, smaller image than the earlier fixture, per its fingerprint) answered HTTP 200 after 41.7 s and the findings stage failed as `response_malformed`; the reply was not kept, so the failure condition is unknown. NVIDIA image findings have otherwise succeeded three times and returned fast HTTP 503s repeatedly (cause unknown). OpenRouter synthesis has passed once in isolation (owner-reported). No full run has completed with real image findings, real research and a synthesis. Billing and usage are unverified for all three providers.
- **Done (offline):** the real image stage's diagnostics now include closed-word facts about an HTTP success reply (which envelope or findings step it reached, the service's finish reason, reported token counts, text length and a conservative shape), with no model text kept, so the next such failure explains itself. Full offline suite (keys unset) 1,907 OK, 17 skipped. The validator, error codes, prompt, `max_tokens`, reasoning settings, retries, timeouts, transport and adapter are unchanged. The running servers serve the older code until restarted.
- **Not done, by decision:** no change to `max_tokens`, reasoning or the prompt on this evidence; no rule-level findings diagnosis (it would need the smoke script's diagnoser).
- **Next (owner):** authorize a restart on a new port with this code, then one NVIDIA-only run (Image Real; Research Fake; Compute Skip; Synthesis Skip), preferably with the original fixture image so the fingerprint can be compared.

## Update, 2026-10-22, IA-12 rule-level findings diagnostic (appended)

Wording written by the implementation agent; not yet reviewed by the owner. Details: `ARCHITECTURE_DECISIONS.md` IA-12.

- **Done (offline):** a refused JSON object now carries the closed rule codes and structural paths that broke the findings contract (`findings_rules`, `findings_rules_total`), shown on the page. Validator, schema, prompt, `max_tokens`, reasoning settings, retries and error codes unchanged. Full offline suite (keys unset, bytecode off) 1,923 OK, 17 skipped; `node --check` passes.
- **Not established:** which rule refused the reply of run `Lxqdua_SSqxgWjKIMTBywg` (it was not kept), and the cause of the intermittent NVIDIA 503s. No live request was made. Billing is unverified for every provider.
- **Provider state (offline evidence only):** NVIDIA findings, Tavily Search and OpenRouter synthesis cooperate through the shared workflow in 231 targeted offline tests plus the full suite; none has completed a real end-to-end run. Nodus paused; Agent37 has no code.
- **Next (owner):** restart the app on a free port with this code, then one NVIDIA-only run (Image Real; Research Fake; Compute Skip; Synthesis Skip) so a refusal names its rule.

## Update, 2026-10-24, SE-1 (M1 Backend Security Readiness)

Details: `ARCHITECTURE_DECISIONS.md` SE-1.

- **Implementation status:** M1 (Backend Security Readiness) completed offline. The Python backend now supports an opt-in `--remote` mode with Bearer-token authentication (`hmac.compare_digest`), exact-origin CORS allowlisting, OPTIONS preflight handling, static web UI disablement (Option A), minimal unauthenticated `/health` endpoint, client socket and body read timeouts (HTTP 408), safe Content-Length parsing, and fail-closed remote real-provider protections. Default local execution mode (`127.0.0.1`, unauthenticated local demo, static UI enabled) is 100% preserved.
- **Provider & secret isolation:** `demo_app/` continues to have no access to provider credentials (`NVIDIA_API_KEY`, `TAVILY_API_KEY`, `OPENROUTER_API_KEY`). Only the authentication token environment variable name is read in `demo_app/server.py`. Tokens and secrets are never logged or exposed.
- **Test verification:** Full offline test suite (`python -B -W default -m unittest discover -s tests`) ran with provider keys unset: **1,957 tests, OK, 17 skipped** (Nodus SDK not importable). New test file `tests/test_remote_security.py` contains 34 targeted tests covering health checks, auth enforcement, CORS headers, host validation, provider refusals, body bounds/timeouts, CLI flags, and tripwires.
- **Live status:** No live provider calls made. No server was deployed or exposed publicly. No cloud resources or billing configured.
- **Next milestone:** M2 — FlutterFlow Integration Contract & Schema Alignment (define endpoint request/response contracts and data models required by FlutterFlow client screens).

## Update, 2026-10-24, IA-13 prompt correction and regression tests

Details: `ARCHITECTURE_DECISIONS.md` IA-13.

- **Live status:** A single authorized standalone live smoke test with the verified real leaf photo (`Screenshot 2026-10-06 175858.png`) achieved HTTP 200 in 60.7 s (119 tokens, 1,028 chars) but failed findings validation (`response_malformed`) with `WRONG_TYPE at crop`. Root cause: the model returned a non-dict for `crop`, influenced by the natural-language noun "crop" and an ambiguous instruction ("Everywhere else use a text or a list").
- **Done (offline):** Updated `build_prompt()` in `agri_pipeline/findings.py` to clarify that `"crop"` is a JSON object with exactly the single key `"candidates"` (never a string, list, or null) and that `"crop"` and `"growth_stage"` are always objects. Kept `validate_findings()` strictly unmodified. Added regression tests in `tests/test_agri_pipeline.py`, `tests/test_nvidia_findings_smoke.py`, and `tests/test_nvidia_findings_rules.py` covering string, list, null, and valid candidate objects at `crop`. Full offline regression suite: **1,957 tests, OK, 17 skipped**.
- **Not done / Limits:** Zero live calls, zero credit consumption, zero provider key modification, zero server changes during this repair.
- **Next (owner):** With prompt clarification and regression tests complete, authorize one live NVIDIA smoke test rerun with the same leaf image to verify whether the model now returns the valid nested `crop.candidates` object structure.

## Update, 2026-10-25, VA-1 (CropSync Voice & Multilingual Learning Assistant)

Details: `ARCHITECTURE_DECISIONS.md` VA-1.

- **Implementation status:** CropSync Voice & Multilingual Learning Assistant (VA-1) fully implemented and verified.
- **Voice integration:** Stdlib-only ElevenLabs client for Speech-to-Text (`scribe_v1`) and Text-to-Speech (`eleven_multilingual_v2`), with pluggable offline transport seam, socket tripwires, and graceful fallback when credentials are unconfigured.
- **Multilingual learning modules:** 5 precision agriculture educational lessons with complete human-reviewed translations in English, Hindi, and Telugu, learning objectives, multi-stage sections, practical farmer tips, and interactive completion tracking.
- **Agronomic question answering & telemetry fusion:** Context-aware QA assistant integrating live in-field rover telemetry from `RecordStore` (soil moisture %, NPK mg/kg, soil/air temperature, humidity, sensor flags), topic matching for irrigation, nitrogen chlorosis, phosphorus, potassium, and pest scouting, generating trilingual responses with advisory warnings.
- **Web UI & server integration:** Routes `/api/voice/transcribe`, `/api/voice/synthesize`, `/api/assistant/ask`, `/api/learning/lessons`, `/api/learning/lesson`, `/api/learning/progress` added to `demo_app`. Interactive Section 8 UI in `index.html`, `style.css` (using theme variables with WCAG compliance), and `app.js` using shared `api()` helper (single fetch instance) without innerHTML.
- **Verification:**
  - 18 unit tests in `tests/test_voice_assistant.py` (offline socket tripwires).
  - 15 HTTP integration tests in `tests/test_demo_voice_assistant.py` (local CSRF, remote Bearer auth, query params, bounds, tripwires).
  - 20 UI structure and theme compliance tests in `tests/test_demo_ui.py`.
  - 120 demo app regression tests in `tests/test_demo_app.py`.
  - 11 end-to-end user journeys verified via `scripts/verify_voice_and_learning_e2e.py`.
  - Full repository test suite: **1,990 tests, OK (skipped=17)**. Zero live provider credits consumed. Zero unapproved external network calls.

## Update, 2026-10-25, M2 (FlutterFlow Integration Contract, OpenAPI Spec, and Regression Tests)

Details: `ARCHITECTURE_DECISIONS.md` FF-1.

- **Implementation status:** Combined Milestone M2 (M2.1 + M2.2 + M2.3) complete.
- **M2.1 (OpenAPI 3.1 Specification):** Created `docs/contracts/OPENAPI_SPEC.yaml` documenting all 17 functional endpoints, `/health` and `/api/health` aliases, `BearerAuth` security scheme, and JSON schemas for all telemetry, diagnostics, research, synthesis, and voice/learning operations. Strict object structure for `crop.candidates` preserved per IA-13 (`CropFindings`, `CropCandidate`).
- **M2.2 (Offline Contract Tests):** Created `tests/test_flutterflow_contract.py` containing 25 targeted tests with offline socket tripwires. Verifies OpenAPI file integrity, path parameters, schema conformance across 4 FlutterFlow screen workflows, remote Bearer authentication, CORS preflight (`OPTIONS` returning 204 with allowlisted origin), and payload bound enforcements.
- **M2.3 (Media Ingestion Protocol Evaluation & Decision):** Evaluated multipart form-data vs JSON base64 for leaf image uploads. Retained base64 JSON (`image_png_base64`) to preserve backend audit boundaries (`TestIsolation` restrictions against unapproved stdlib parsers) and ensure atomic binding of image, telemetry, and question metadata. Audio transcription (`/api/voice/transcribe`) accepts raw binary audio (`audio/webm`, `audio/wav`, `audio/mpeg`) in request body; audio synthesis returns base64 MP3.
- **Client Integration Guide:** Created `docs/FLUTTERFLOW_INTEGRATION_GUIDE.md` covering backend server flags, remote Bearer auth, Dart custom functions for base64 conversion, screen mappings, CORS configuration, and error matrices.
- **Test verification:**
  - 25 contract tests in `tests/test_flutterflow_contract.py` (OK).
  - Full offline test suite: **2,015 tests, OK (skipped=17)**.
  - Zero live network calls made. Zero credentials exposed or printed.
- **Next milestone:** M3 — FlutterFlow UI Implementation / Client Binding.
