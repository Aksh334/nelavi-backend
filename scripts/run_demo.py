"""Launcher for the demo with the REAL Nodus compute stage.

    python -B -m scripts.run_demo --compute-provider nodus --i-accept-nodus-charges --port 8800 [any demo_app option]
    python -B -m scripts.run_demo --port 8800                      # same as `python -m demo_app`

This is the one place that imports the Nodus SDK. It lives in `scripts/` because the production packages are tested
never to import a provider SDK, and because the demo itself must keep running on a machine without the SDK. The
launcher builds a `NodusTaskRunner` whose handle factory imports `nodus` lazily (on the first run of the compute
stage, not at start-up) and gives the runner to `demo_app.server.main`; everything else is the normal demo.

PAUSED by the owner's no-payment decision: `--compute-provider nodus` is refused (exit code 2, nothing started) unless
`--i-accept-nodus-charges` is also given. The integration is intact; the second flag is the deliberate switch to
un-pause it after the owner has authorized a real call.

What `--compute-provider nodus` needs, and what it does NOT do
  * the SDK, importable by THIS interpreter, and a Python inside the vendor's documented range (3.10 to 3.13): checked
    before anything starts, and a failure exits with code 2 and starts nothing;
  * the remote function `cropsync-summary` / `summarize_record` ALREADY deployed and your Nodus login/credentials in
    place. This launcher does not deploy anything, does not log in and does not read any credential: the SDK resolves
    those itself. If they are missing the compute stage fails with a closed error code and the rest of the run goes on.
  * It sends, only when a request sets the compute stage to "real", the `sensor_status`, `soil`, `air` and `battery`
    sections of the (simulated) Record to Nodus. Cost is not established; the function's own settings carry a cap.
There is no fallback: a failing real compute stage stays failed and no fake takes its place.

The default (`--compute-provider none`) builds nothing and imports nothing.
"""

import argparse
import sys

from demo_app import server
from orchestration import nodus_adapter

APP_NAME = "cropsync-summary"
FUNCTION_NAME = "summarize_record"
COMPUTE_NONE = "none"
COMPUTE_NODUS = "nodus"
COMPUTE_CHOICES = (COMPUTE_NONE, COMPUTE_NODUS)
ACCEPT_FLAG = "--i-accept-nodus-charges"
PAUSED_MESSAGE = ("Error: real Nodus compute is PAUSED by the owner's no-payment decision. --compute-provider nodus "
                  "also needs %s, to be used only after the owner has authorized a real Nodus call and accepted its "
                  "cost. Nothing was started; there is no fallback to a fake runner." % ACCEPT_FLAG)
BANNER = ("The compute stage can use REAL Nodus (remote function %s/%s, which must already be deployed). Choosing "
          "'real' for it sends the sensor_status, soil, air and battery sections of the simulated Record to Nodus and "
          "may incur cost." % (APP_NAME, FUNCTION_NAME))


def nodus_handle():
    """The Nodus function handle. The SDK is imported here, on demand, and nowhere else in this project."""
    import nodus
    return nodus.Function.from_name(APP_NAME, FUNCTION_NAME)


def preflight(stderr):
    """None if this interpreter can use the SDK, else an exit code (2) after printing why. Nothing is imported."""
    if not nodus_adapter.python_is_documented():
        print("Error: --compute-provider nodus needs Python %d.%d to %d.%d (the vendor's documented range); this is "
              "Python %d.%d. Nothing was started; there is no fallback to a fake runner."
              % (nodus_adapter.DOCUMENTED_PYTHON_MIN + (nodus_adapter.DOCUMENTED_PYTHON_MAX_EXCLUSIVE[0],
                                                         nodus_adapter.DOCUMENTED_PYTHON_MAX_EXCLUSIVE[1] - 1)
                 + tuple(sys.version_info[:2])), file=stderr)
        return 2
    if not nodus_adapter.sdk_installed():
        print("Error: --compute-provider nodus needs the %s package to be importable by this Python. Nothing was "
              "started; this launcher installs nothing and has no fallback." % nodus_adapter.SDK_DISTRIBUTION,
              file=stderr)
        return 2
    return None


def main(argv=None, stdout=None, stderr=None):
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = argparse.ArgumentParser(prog="python -m scripts.run_demo", add_help=False)
    parser.add_argument("--compute-provider", choices=COMPUTE_CHOICES, default=COMPUTE_NONE)
    parser.add_argument(ACCEPT_FLAG, dest="accept_nodus_charges", action="store_true")
    own, rest = parser.parse_known_args(argv)
    if own.compute_provider == COMPUTE_NONE:
        if own.accept_nodus_charges:
            print("Error: %s has no effect without --compute-provider nodus. Nothing was started." % ACCEPT_FLAG,
                  file=stderr)
            return 2
        return server.main(rest, stdout=stdout, stderr=stderr)
    if not own.accept_nodus_charges:
        print(PAUSED_MESSAGE, file=stderr)
        return 2
    failure = preflight(stderr)
    if failure is not None:
        return failure
    runner = nodus_adapter.NodusTaskRunner(nodus_handle)
    return server.main(rest, stdout=stdout, stderr=stderr, compute_runner=runner, compute_note=BANNER)


if __name__ == "__main__":
    sys.exit(main())
