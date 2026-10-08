# Changelog

Meaningful project changes from the start of implementation onward. No version numbers are defined by any project source, so none are used.

## Unreleased

- Added governance documentation: `PROJECT_STATUS.md`, `PROJECT_HISTORY.md`, `CHANGELOG.md`, `ARCHITECTURE_DECISIONS.md` (created, not yet committed at the time of writing).
- No application implementation has occurred.
- Recorded verified NVIDIA text-only connectivity (`ARCHITECTURE_DECISIONS.md`, PC-1); image inference remains unverified after two HTTP 503 ResourceExhausted responses; no provider integration code was added.

## Baseline

- Commit `acb1daa` "Initial CropSync frozen baseline" on `master`.
- Committed the frozen documentation and contract baseline:
  - `docs/cropsync_coding_overview.docx`
  - `docs/CROPSYNC_Rover_Build_and_Parts.docx`
  - `docs/HARDWARE_SOFTWARE_INTERFACE.md`
  - `docs/contracts/RECORD_CONTRACT.md`
  - `docs/contracts/WEBSOCKET_CONTRACT.md`
  - `docs/contracts/ARM_SERIAL_CONTRACT.md`
- Added a minimal `.gitignore` (operating-system files and Office lock files only).
- No features, source code, dependencies or tests were added.
