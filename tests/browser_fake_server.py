"""TEST-ONLY launcher for the real-browser check of Section 6 (Research Pro). Not part of the application.

It starts the real demo (real DemoApp, real DemoService, real TavilyResearchPro, real static page) on
127.0.0.1 with an OS-assigned port, but the Tavily transport is a FAKE that answers from this file. It never
reads the real TAVILY_API_KEY (it passes a made-up string as the environment). It also installs a Python-level
guard that refuses connections and name lookups to anything but loopback, and checks before serving that five
such attempts are refused. That guard covers only this Python process and only Python-level socket calls; it is
a safeguard, not independent proof that nothing external can be reached.

Run by tests/browser_check_research_pro.js:  python -B tests/browser_fake_server.py --log <file>
Prints "ISOLATION_SELFTEST_OK" and then "READY port=<n>" on stdout. It stops when its stdin closes.

All provider answers are SYNTHETIC. The fake picks a scenario from the first word of the question.
"""

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
FORBIDDEN_PORT = 8765


# ------------------------------------------------------------------ network isolation
def _host_of(address):
    return address[0] if isinstance(address, (tuple, list)) else address


def install_network_guard():
    """After this, connecting to or resolving anything but loopback raises OSError before anything is sent."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_gethostbyname = socket.gethostbyname
    real_gethostbyaddr = socket.gethostbyaddr

    def guard_name(name):
        if name not in LOOPBACK:
            raise OSError("blocked by the test launcher: only loopback is allowed")

    def getaddrinfo(host, *args, **kwargs):
        guard_name(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        guard_name(_host_of(address))
        return real_connect(self, address)

    def connect_ex(self, address):
        guard_name(_host_of(address))
        return real_connect_ex(self, address)

    def gethostbyname(name):
        guard_name(name)
        return real_gethostbyname(name)

    def gethostbyaddr(name):
        guard_name(name)
        return real_gethostbyaddr(name)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.gethostbyname = gethostbyname
    socket.gethostbyaddr = gethostbyaddr


def isolation_selftest():
    """Every one of these must be refused by the guard, so no packet is ever sent."""
    probes = (
        lambda: socket.create_connection(("api.tavily.com", 443), timeout=1),
        lambda: socket.getaddrinfo("api.tavily.com", 443),
        lambda: socket.socket().connect(("192.0.2.1", 80)),            # TEST-NET-1, documentation range
        lambda: socket.socket().connect_ex(("api.tavily.com", 443)),
        lambda: socket.gethostbyname("api.tavily.com"),
    )
    for probe in probes:
        try:
            probe()
        except OSError as exc:
            if "blocked by the test launcher" not in str(exc):
                return False
        else:
            return False
    return True


# ------------------------------------------------------------------ the fake provider
APPROVED = [
    {"title": "ICAR guide", "url": "https://www.icar.org.in/guide-1"},
    {"title": "Regional page", "url": "https://krishi.icar.org.in/page"},
]
REJECTED = [
    {"title": "Look-alike", "url": "https://icar.org.in.evil.example/x"},
    {"title": "Not ICAR", "url": "https://noticar.org.in/x"},
    {"title": "Credentials", "url": "https://icar.org.in@evil.example/x"},
    {"title": "Plain http", "url": "http://icar.org.in/plain"},
    {"title": "Script", "url": "javascript:alert(1)"},
    {"title": "Path trick", "url": "https://evil.example/icar.org.in"},
]
COMPLETE_TEXT = "SYNTHETIC mocked research text.\nSecond line."
HTML_TEXT = ('<b>BOLD</b> <img src="x" onerror="window.__xss=1"> '
             '<script>window.__xss=1</script> &amp; end')
JUMP_SECONDS = 50.0


class JumpClock:
    """Real monotonic time, plus a jump that ONLY the fake 'timeout' scenario ever adds."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


class FakeProvider:
    def __init__(self, log_path, clock):
        self._log_path = log_path
        self._clock = clock
        self._tasks = {}
        self._count = 0
        self._lock = threading.Lock()

    def _log(self, entry):
        with open(self._log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def __call__(self, method, url, headers, body, timeout):
        with self._lock:
            self._count += 1
            entry = {"seq": self._count, "method": method, "url": url, "timeout": timeout,
                     "authorization_is_bearer": str(headers.get("Authorization", "")).startswith("Bearer ")}
            if method == "POST":
                payload = json.loads(body)
                scenario = (payload.get("input") or "x").split()[0].lower()
                request_id = "fake-%s-%d" % (scenario, self._count)
                self._tasks[request_id] = {"scenario": scenario, "polls": 0}
                entry["body"] = payload
                self._log(entry)
                return 201, json.dumps({"request_id": request_id, "created_at": "2026-01-02T03:04:05Z",
                                        "status": "pending", "input": payload.get("input"), "model": "pro",
                                        "response_time": 1.23}).encode(), None
            request_id = url.rsplit("/", 1)[-1]
            task = self._tasks.get(request_id)
            if task is None:
                self._log(entry)
                return 404, b'{"detail": {"error": "Research task not found"}}', None
            task["polls"] += 1
            scenario = task["scenario"]
            waiting = (202, json.dumps({"request_id": request_id, "status": "in_progress",
                                        "response_time": 2}).encode(), None)
            entry["scenario"], entry["poll"] = scenario, task["polls"]
            if scenario == "timeout":
                self._clock.offset += JUMP_SECONDS
                entry["clock_jump_seconds"] = JUMP_SECONDS
                reply = waiting
            elif scenario == "ratelimit" and task["polls"] == 1:
                reply = (429, b'{"detail": {"error": "Too many requests"}}', "6")
            elif scenario in ("complete", "html", "fail", "malformed") and task["polls"] == 1:
                reply = waiting
            elif scenario == "fail":
                reply = (200, json.dumps({"request_id": request_id, "status": "failed",
                                          "response_time": 3}).encode(), None)
            elif scenario == "malformed":
                reply = (200, json.dumps({"request_id": request_id, "status": "completed",
                                          "content": {"x": 1}, "sources": "no"}).encode(), None)
            else:
                text = HTML_TEXT if scenario == "html" else COMPLETE_TEXT
                reply = (200, json.dumps({"request_id": request_id, "created_at": "2026-01-02T03:04:05Z",
                                          "status": "completed", "content": text,
                                          "sources": APPROVED + REJECTED, "response_time": 40}).encode(), None)
            entry["reply_status"] = reply[0]
            self._log(entry)
            return reply


# ------------------------------------------------------------------ serve
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True, help="file that receives one JSON line per fake-provider call")
    args = parser.parse_args()

    install_network_guard()
    if not isolation_selftest():
        print("ISOLATION_SELFTEST_FAILED", flush=True)
        return 3
    print("ISOLATION_SELFTEST_OK", flush=True)

    from http.server import HTTPServer

    from demo_app.app import DemoApp
    from demo_app.server import HOST, _AppHolder, make_handler
    from demo_app.service import DemoService
    from research.tavily_research import TavilyResearchPro
    from storage.record_store import RecordStore

    clock = JumpClock()
    provider = FakeProvider(args.log, clock)
    client = TavilyResearchPro(transport=provider, environ={"TAVILY_API_KEY": "fake-key-not-real"}, clock=clock)
    store = RecordStore(":memory:")
    holder = _AppHolder()
    server = HTTPServer((HOST, 0), make_handler(holder))
    port = server.server_address[1]
    if port == FORBIDDEN_PORT:
        print("REFUSING port %d" % FORBIDDEN_PORT, flush=True)
        return 3
    holder.app = DemoApp(DemoService(store, research_pro_client=client),
                         {"%s:%d" % (HOST, port), "localhost:%d" % port})

    def stop_when_stdin_closes():
        try:
            sys.stdin.read()
        finally:
            server.shutdown()

    threading.Thread(target=stop_when_stdin_closes, daemon=True).start()
    lifetime = threading.Timer(600, server.shutdown)  # never outlive ten minutes
    lifetime.daemon = True
    lifetime.start()
    print("READY port=%d" % port, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
