"""Cross-provider tests: the real NVIDIA, Tavily and OpenRouter adapters cooperating through the integrated workflow.

Each real adapter is the production class; only its HTTP transport is replaced by a recording fake, so the exact
requests that WOULD be sent can be compared across providers. These tests prove how data moves and what is checked
between the stages (data boundaries, citation alignment, dependencies, honest labels). They do NOT prove that any
hosted service answers as the fakes do: nothing here has been run live. Component-level behaviour (URL rules, error
codes, reply checks) is covered in the per-provider test files and is not repeated.
"""

import base64
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import demo_app
from demo_app.app import DemoApp
from demo_app.service import DemoService
from image_analysis import nvidia
from orchestration.integrated import check_integrated_result
from research.tavily import TavilyResearch
from storage.record_store import RecordStore
from synthesis_provider.openrouter import OpenRouterSynthesisProvider

HOST = "127.0.0.1:8765"
NV_KEY, TV_KEY, OR_KEY = "nvapi-canary-0001", "tvly-canary-0002", "or-canary-0003"
MODEL = "vendor/some-model:free"
QUESTION = "how should soil moisture readings be read"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image \x00\x01\x02\xff"
PNG_B64 = base64.b64encode(PNG).decode("ascii")
AGREED = ["icar.org.in", "agritech.tnau.ac.in", "fao.org"]
GOOD = {
    "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
    "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
    "symptoms": [{"observation": "brown spots on lower leaves", "possible_interpretations": ["possible-cause-a"]}],
    "needs_more": ["a close-up of the underside of a leaf"],
}
URL_1, URL_2 = "https://fao.org/page-one", "https://www.icar.org.in/page-two"
OFF = ["http://fao.org/plain-http", "https://evil.example/x", "https://fao.org.evil.example/y"]
ROOT = Path(demo_app.__file__).resolve().parents[1]


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class Scripted:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def nv_ok():
    return 200, json.dumps({"choices": [{"message": {"content": json.dumps(GOOD)}}]}).encode("utf-8")


def tv_ok(extra=()):
    results = [{"title": "Source one", "url": URL_1, "content": "Snippet one about soil moisture.",
                "published_date": "2025-01-02", "score": 0.9}]
    results += [{"title": "Rejected", "url": u, "content": "x"} for u in OFF]
    results.append({"title": "Source two", "url": URL_2, "content": "Snippet two about sensors.", "score": 0.5})
    results += list(extra)
    return 200, json.dumps({"results": results}).encode("utf-8")


def syn_ok(cites=(("F:crop", "E1"), ("E2",))):
    statements = [{"text": "Statement %d drawn from the supplied items." % n, "cites": list(c)}
                  for n, c in enumerate(cites, start=1)]
    content = json.dumps({"statements": statements, "uncertainties": ["Nothing here has been verified."]})
    return 200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode("utf-8")


class Rig:
    """The production service with the three real adapters, each on its own recording transport."""

    def __init__(self, testcase, nv=None, tv=None, syn=None, tavily_max=5):
        self.nv, self.tv, self.syn = Scripted(nv or nv_ok()), Scripted(tv or tv_ok()), Scripted(syn or syn_ok())
        store = RecordStore(":memory:")
        testcase.addCleanup(store.close)
        analyzer = nvidia.NvidiaImageAnalyzer(transport=self.nv, environ={"NVIDIA_API_KEY": NV_KEY})
        search = TavilyResearch(AGREED, transport=self.tv, environ={"TAVILY_API_KEY": TV_KEY}, max_results=tavily_max,
                                clock=lambda: "2026-01-01T00:00:00+00:00")
        writer = OpenRouterSynthesisProvider(MODEL, "OPENROUTER_API_KEY", transport=self.syn,
                                             environ={"OPENROUTER_API_KEY": OR_KEY})
        self.app = DemoApp(DemoService(store, findings_analyzer=analyzer, research_client=search,
                                       research_domains=AGREED, synthesis_provider=writer), {HOST})

    def run(self, image="real", research="real", synthesis="real"):
        payload = {"scenario": "normal_operation", "question": QUESTION, "image_png_base64": PNG_B64,
                   "providers": {"image": image, "research": research, "compute": "fake", "synthesis": synthesis}}
        raw = json.dumps(payload).encode("utf-8")
        headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(raw)),
                   "content-type": "application/json"}
        response = self.app.handle_request("POST", "/api/run-integrated", headers, io.BytesIO(raw).read)
        self.response = response
        return response.status, json.loads(response.body.decode("utf-8"))


def stages(body):
    """The stage table keyed by the names the page uses (the result calls the image stage `image_findings`)."""
    names = {"image_findings": "image", "sensor_summary": "compute"}
    return {names.get(s["stage"], s["stage"]): s for s in body["integrated"]["integration"]["stages"]}


def entries(body):
    return body["integrated"]["report"]["sections"]["retrieved_sources"]["entries"]


def synthesis_data(call):
    """The DATA block of the synthesis request, as the model would receive it."""
    user = json.loads(call["body"])["messages"][1]["content"]
    return json.loads(user.split("\n", 1)[1])


class TestHowTheProvidersExchangeData(NoNetwork):
    def test_findings_and_evidence_flow_into_one_cited_synthesis_with_aligned_references(self):
        rig = Rig(self)
        status, body = rig.run()
        self.assertEqual(status, 200)
        self.assertEqual(body["providers"], {"image": "real", "research": "real", "compute": "fake", "synthesis": "real"})
        self.assertEqual(body["real_stages"], ["image", "research", "synthesis"])
        s = stages(body)
        for name in ("image", "research", "synthesis"):
            self.assertEqual((s[name]["status"], s[name]["provider_called"]), ("completed", True), name)
        # each provider was called exactly once, with no retry
        self.assertEqual([len(t.calls) for t in (rig.nv, rig.tv, rig.syn)], [1, 1, 1])

        # research: only the question, restricted to the agreed domains; nothing from the image or the findings
        tavily_request = json.loads(rig.tv.calls[0]["body"])
        self.assertEqual((tavily_request["query"], tavily_request["include_domains"]), (QUESTION, AGREED))
        tavily_text = rig.tv.calls[0]["body"].decode("utf-8")
        for absent in ("crop-a", PNG_B64, "brown spots"):
            self.assertNotIn(absent, tavily_text)

        # evidence: only HTTPS agreed-host results survive, in order, unreviewed, and the rejected ones are counted
        self.assertEqual([e["url"] for e in entries(body)], [URL_1, URL_2])
        self.assertEqual([e["review_status"] for e in entries(body)], ["unreviewed", "unreviewed"])
        step = {x["name"]: x for x in body["integrated"]["run"]["steps"]}["research_search"]
        self.assertEqual(step["result"]["dropped"], len(OFF))

        # synthesis input: the validated findings and the evidence text, with ids that match the report order
        data = synthesis_data(rig.syn.calls[0])
        self.assertEqual(data["findings"], GOOD)
        self.assertEqual(data["question"], QUESTION)
        self.assertEqual([e["id"] for e in data["evidence"]], ["E1", "E2"])
        for number, sent in enumerate(data["evidence"], start=1):
            entry = entries(body)[number - 1]
            self.assertEqual((sent["title"], sent["snippet"]), (entry["title"], entry["snippet"]))

        # the answer: every citation is a supplied reference and every E<n> resolves to a report entry
        result = body["integrated"]["synthesis"]
        self.assertEqual((result["kind"], result["fake"], result["model"]),
                         ("model_generated_unverified", False, "openrouter:" + MODEL))
        cited = {c for st in result["statements"] for c in st["cites"]}
        self.assertEqual(cited, {"F:crop", "E1", "E2"})
        for ref in cited:
            if ref.startswith("E"):
                self.assertLessEqual(int(ref[1:]), len(entries(body)))
        self.assertEqual(check_integrated_result(body["integrated"]), [])

    def test_the_synthesis_request_carries_no_image_record_sensor_link_score_or_credential(self):
        rig = Rig(self)
        rig.run()
        text = rig.syn.calls[0]["body"].decode("utf-8")
        for absent in (PNG_B64, URL_1, URL_2, "fao.org", "icar.org.in", "0.9", "score", NV_KEY, TV_KEY, OR_KEY,
                       "photo_id", "record_id", "sensor_status", "npk", "battery", "rover", "leaf_label", "origin"):
            self.assertNotIn(absent, text, absent)
        self.assertEqual(rig.syn.calls[0]["headers"]["Authorization"], "Bearer " + OR_KEY)
        # and each provider received only its own credential
        self.assertNotIn(OR_KEY, rig.nv.calls[0]["body"].decode("utf-8") + rig.tv.calls[0]["body"].decode("utf-8"))
        for secret in (NV_KEY, TV_KEY, OR_KEY):
            self.assertNotIn(secret, rig.response.body.decode("utf-8"))

    def test_the_image_goes_only_to_the_vision_provider(self):
        rig = Rig(self)
        rig.run()
        self.assertIn(PNG_B64, rig.nv.calls[0]["body"].decode("utf-8"))
        self.assertNotIn(PNG_B64, rig.tv.calls[0]["body"].decode("utf-8") + rig.syn.calls[0]["body"].decode("utf-8"))

    def test_evidence_is_capped_before_it_is_numbered_for_citation(self):
        many = [{"title": "T%d" % i, "url": "https://fao.org/p%d" % i, "content": "snippet %d" % i} for i in range(30)]
        rig = Rig(self, tv=(200, json.dumps({"results": many}).encode("utf-8")), tavily_max=3,
                  syn=syn_ok(cites=(("E3",),)))
        status, body = rig.run()
        self.assertEqual(len(entries(body)), 3)
        self.assertEqual([e["id"] for e in synthesis_data(rig.syn.calls[0])["evidence"]], ["E1", "E2", "E3"])
        self.assertEqual(stages(body)["synthesis"]["status"], "completed")


class TestCitationIntegrity(NoNetwork):
    def test_a_citation_of_something_never_supplied_fails_the_synthesis_and_keeps_the_other_stages(self):
        for cites in ((("E9",),), (("E3",),), (("F:symptom:5",),), (("F:crop", "E1", "E1"),)):
            with self.subTest(cites):
                rig = Rig(self, syn=syn_ok(cites=cites))
                status, body = rig.run()
                s = stages(body)
                self.assertEqual((s["synthesis"]["status"], s["synthesis"]["error_code"]),
                                 ("failed", "synthesis_response_malformed"))
                self.assertIsNone(body["integrated"]["synthesis"])
                self.assertEqual((s["image"]["status"], s["research"]["status"]), ("completed", "completed"))
                self.assertEqual([e["url"] for e in entries(body)], [URL_1, URL_2])   # evidence is still reported

    def test_links_the_model_writes_are_refused_and_the_links_shown_come_only_from_the_search_result(self):
        content = json.dumps({"statements": [{"text": "See https://evil.example/x for details.", "cites": ["E1"]}],
                              "uncertainties": []})
        reply = 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        status, body = Rig(self, syn=reply).run()
        self.assertEqual(stages(body)["synthesis"]["error_code"], "synthesis_response_malformed")
        self.assertNotIn("evil.example", json.dumps(body["integrated"]["report"]))


class TestDependenciesAndFailureVisibility(NoNetwork):
    def test_a_failed_upstream_stage_skips_synthesis_without_calling_it_and_the_independent_stage_still_runs(self):
        cases = {
            "image fails": (dict(nv=(503, b"x")), "image", "provider_unavailable", "research"),
            "research fails": (dict(tv=(429, b"x")), "research", "rate_limited", "image"),
        }
        for name, (kwargs, failed, code, other) in cases.items():
            with self.subTest(name):
                rig = Rig(self, **kwargs)
                status, body = rig.run()
                self.assertEqual(status, 200)
                s = stages(body)
                self.assertEqual((s[failed]["status"], s[failed]["error_code"]), ("failed", code))
                self.assertEqual(s[other]["status"], "completed")
                self.assertEqual((s["synthesis"]["status"], s["synthesis"]["skip_reason"], s["synthesis"]["provider_called"]),
                                 ("skipped", "dependency_not_completed", False))
                self.assertEqual(rig.syn.calls, [])
                self.assertIsNone(body["integrated"]["synthesis"])
                self.assertEqual(len(rig.nv.calls) + len(rig.tv.calls), 2)   # one request each, no retry

    def test_both_upstream_stages_failing_is_reported_twice_and_nothing_is_invented(self):
        rig = Rig(self, nv=(401, b"x"), tv=(200, b'{"results": []}'))
        status, body = rig.run()
        s = stages(body)
        self.assertEqual((s["image"]["status"], s["research"]["status"], s["research"]["error_code"]),
                         ("failed", "failed", "no_results"))
        self.assertEqual(s["synthesis"]["status"], "skipped")
        self.assertEqual(entries(body), [])
        self.assertEqual(rig.syn.calls, [])

    def test_a_failed_real_synthesis_is_a_failure_with_the_other_results_intact(self):
        for outcome, code in (((502, b"x"), "synthesis_failed"), ((401, b"x"), "synthesis_rejected"),
                              ((200, b'{"error": {"code": 402}}'), "synthesis_rejected")):
            with self.subTest(code):
                rig = Rig(self, syn=outcome)
                status, body = rig.run()
                s = stages(body)
                self.assertEqual((s["synthesis"]["status"], s["synthesis"]["error_code"], s["synthesis"]["provider_called"]),
                                 ("failed", code, True))
                self.assertIsNone(body["integrated"]["synthesis"])
                self.assertEqual((s["image"]["status"], s["research"]["status"]), ("completed", "completed"))
                self.assertEqual(len(rig.syn.calls), 1)


class TestStoredSynthesisIsRevalidated(NoNetwork):
    """`check_integrated_result` re-applies the synthesis validator to the STORED synthesis, so an edited result is noticed."""

    def result(self, **kwargs):
        status, body = Rig(self, **kwargs).run()
        return body["integrated"]

    def tamper(self, result, change):
        """Apply `change` to the stored synthesis and to the completed step's copy, so only the content differs."""
        edited = copy.deepcopy(result)
        step = {s["name"]: s for s in edited["run"]["steps"]}["synthesis"]
        change(edited["synthesis"])
        change(step["result"])
        return edited

    def test_an_untouched_real_chain_result_is_consistent(self):
        result = self.result()
        self.assertEqual(result["synthesis"]["statements"][0]["cites"], ["F:crop", "E1"])
        self.assertEqual(check_integrated_result(result), [])

    def test_an_injected_web_address_is_noticed(self):
        for text in ("See https://evil.example/x for details.", "Visit www.evil.example now."):
            with self.subTest(text):
                edited = self.tamper(self.result(), lambda s, t=text: s["statements"][0].update(text=t))
                self.assertEqual(check_integrated_result(edited), ["synthesis_invalid"])

    def test_an_extra_key_is_noticed_in_a_statement_in_the_synthesis_and_in_the_uncertainties_block(self):
        changes = (lambda s: s["statements"][0].update(extra="x"),
                   lambda s: s.update(extra="x"),
                   lambda s: s.pop("uncertainties"),
                   lambda s: s.pop("fake"))
        for number, change in enumerate(changes):
            with self.subTest(number):
                edited = self.tamper(self.result(), change)
                self.assertEqual(check_integrated_result(edited), ["synthesis_invalid"])

    def test_an_overlong_statement_or_uncertainty_is_noticed(self):
        for change in (lambda s: s["statements"][0].update(text="x" * 401),
                       lambda s: s["uncertainties"].append("u" * 301),
                       lambda s: s.update(statements=s["statements"] * 5)):
            edited = self.tamper(self.result(), change)
            self.assertEqual(check_integrated_result(edited), ["synthesis_invalid"])

    def test_a_repeated_citation_in_one_statement_is_noticed(self):
        edited = self.tamper(self.result(), lambda s: s["statements"][0].update(cites=["E1", "E1"]))
        self.assertEqual(check_integrated_result(edited), ["synthesis_invalid"])

    def test_generated_text_cannot_be_relabelled_as_something_else(self):
        for change in (lambda s: s.update(kind="verified_evidence"), lambda s: s.update(fake="no"),
                       lambda s: s.update(model="m" * 500)):
            edited = self.tamper(self.result(), change)
            self.assertEqual(check_integrated_result(edited), ["synthesis_invalid"])

    def test_an_unknown_citation_keeps_its_own_token_and_is_not_double_reported(self):
        edited = self.tamper(self.result(), lambda s: s["statements"][0].update(cites=["E99"]))
        self.assertEqual(check_integrated_result(edited), ["synthesis_unknown_citation"])

    def test_a_tampered_synthesis_of_a_step_that_did_not_complete_keeps_the_existing_token_only(self):
        result = self.result(syn=(502, b"x"))
        self.assertIsNone(result["synthesis"])
        edited = copy.deepcopy(result)
        edited["synthesis"] = {"kind": "x", "statements": [], "uncertainties": []}
        self.assertEqual(check_integrated_result(edited), ["synthesis_without_completed_step"])


class TestRealVersusSimulated(NoNetwork):
    def test_choosing_fake_never_touches_a_real_provider_and_stays_labelled_fake(self):
        rig = Rig(self)
        status, body = rig.run(image="fake", research="fake", synthesis="fake")
        self.assertEqual(body["real_stages"], [])
        self.assertEqual([len(t.calls) for t in (rig.nv, rig.tv, rig.syn)], [0, 0, 0])
        self.assertIs(body["integrated"]["synthesis"]["fake"], True)
        self.assertIn("FAKE", body["label"].upper())

    def test_a_mixed_run_names_exactly_the_real_stages_and_a_fake_synthesis_is_not_presented_as_real(self):
        rig = Rig(self)
        status, body = rig.run(image="real", research="real", synthesis="fake")
        self.assertEqual(body["real_stages"], ["image", "research"])
        self.assertEqual(rig.syn.calls, [])
        self.assertIs(body["integrated"]["synthesis"]["fake"], True)
        self.assertEqual(sorted(body["data_sent"]), ["image", "research"])

    def test_a_real_stage_that_is_not_configured_is_refused_not_faked(self):
        store = RecordStore(":memory:")
        self.addCleanup(store.close)
        app = DemoApp(DemoService(store), {HOST})
        payload = {"scenario": "normal_operation", "question": QUESTION,
                   "providers": {"image": "skip", "research": "skip", "compute": "skip", "synthesis": "real"}}
        raw = json.dumps(payload).encode("utf-8")
        response = app.handle_request("POST", "/api/run-integrated", {
            "host": HOST, "x-nelavi-demo": "1", "content-length": str(len(raw)), "content-type": "application/json"},
            io.BytesIO(raw).read)
        self.assertEqual(response.status, 400)
        self.assertIn("no real provider configured", json.loads(response.body.decode("utf-8"))["error"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestThePageResolvesCitations(NoNetwork):
    def test_every_source_card_shows_the_reference_the_synthesis_cites_it_by(self):
        rig = Rig(self)
        status, body = rig.run()
        with tempfile.TemporaryDirectory() as folder:
            response_file = Path(folder) / "response.json"
            response_file.write_text(json.dumps(body), encoding="utf-8")
            done = subprocess.run(
                ["node", str(ROOT / "tests" / "js_harness_integrated_result.js"),
                 str(ROOT / "demo_app" / "static" / "app.js"), str(response_file)],
                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr[:300])
        shown = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(shown["rendered"])
        text = shown["text"]
        for label in ("cited as E1", "cited as E2"):
            self.assertIn(label, text)
        self.assertNotIn("cited as E3", text)
        self.assertIn("it does not check that the statement is true", text)
        self.assertIn("[cites: F:crop, E1]", text)
        self.assertIn(URL_1, text)


if __name__ == "__main__":
    unittest.main()
