"""Nelavi local web demo: a simulated, offline vertical slice.

Start it from the repository root:

    python -m demo_app            # then open http://127.0.0.1:8765/
    python -m demo_app --port 9000
    python -m demo_app --db C:\\path\\to\\history.sqlite3   # keep history between runs

Everything on the page is simulated by local code: the rover (mock_rover), the
readings, the photo reference and the image "analysis" (a deterministic demo
routine, not an AI model). No network request is made, no provider or credential is
used, and the server only listens on 127.0.0.1. Standard library only.
"""
