"""Offline tests of the rule-level findings diagnostic (IA-12).

When the reply to an image-findings request is a JSON object that `validate_findings` refuses, `reply_facts` now also
names WHICH rules it broke, as closed codes plus structural paths (for example `symptoms[0].observation`). Nothing here
opens a socket or reads a key from the environment: the "network" is a scripted fake transport and socket, DNS and
urllib tripwires are active. Every reply is synthetic. A canary string stands in for model text and for key names the
model invented, and it must never reach a fact, a response or the page. The diagnostic changes no request, no error
code and no validation: `validate_findings` stays the only judge, and the rules below must agree with it.
"""

import base64
import copy
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import demo_app
from agri_pipeline.findings import (
    MAX_CROP_CANDIDATES,
    MAX_INTERPRETATIONS,
    MAX_NEEDS,
    MAX_SYMPTOMS,
    validate_findings,
)
from agri_pipeline.reply_facts import (
    FACT_KEYS,
    MAX_RULES_SHOWN,
    RULE_CODES,
    clean_reply_facts,
    findings_rules,
    reply_facts_of,
)
from demo_app.app import DemoApp
from demo_app.server import ObservingTransport
from demo_app.service import DemoService
from image_analysis import nvidia
from scripts import nvidia_findings_smoke_test as smoke
from storage.record_store import RecordStore

HOST = "127.0.0.1:54321"
KEY = "nvapi-canary-not-a-real-key-0012"
CANARY = "model-text-canary-7733"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic bytes, not a real image \x00\x01\x02\xff"
PNG_B64 = base64.b64encode(PNG).decode("ascii")
SCRIPT = (Path(demo_app.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")


def good():
    return {
        "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
        "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
        "symptoms": [{"observation": "brown spots", "possible_interpretations": ["cause-a", "cause-b"]}],
        "needs_more": ["a close-up photo"],
    }


def edited(path, value, delete=False):
    document = copy.deepcopy(good())
    node = document
    for step in path[:-1]:
        node = node[step]
    if delete:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return document


def envelope(text, finish="stop"):
    return json.dumps({"choices": [{"message": {"content": text}, "finish_reason": finish}]}).encode("utf-8")


def facts_for(document):
    return reply_facts_of(envelope(json.dumps(document)))


def battery():
    """Synthetic documents that break (or keep) each rule of the contract, one or two at a time."""
    bad_texts = ["", "   ", "x" * 301, "tab\there", "new\nline", "zero​width", 5, None, True, [], {}, 1.5]
    bad_names = bad_texts + ["x" * 65, "<b>bold</b>", "leaf spot (fungal)", "a;b", "a/b", "a:b", 'say "hi"', "50%",
                             "it’s", "a—b", "10°"]
    bad_enums = ["95%", "HIGH", "", None, 5, True, [], {}, "medium ", "very high"]
    wrong_types = [None, 5, "text", [], {}, True, 1.5]
    documents = [good()]
    for key in ("crop", "growth_stage", "symptoms", "needs_more"):
        documents.append(edited((key,), None, delete=True))
        documents += [edited((key,), value) for value in wrong_types]
    documents += [dict(good(), extra=1), edited(("crop", "extra"), 1), edited(("crop", "candidates"), None, delete=True),
                  edited(("crop", "candidates", 0, "extra"), 1)]
    for key in ("name", "evidence", "uncertainty"):
        documents.append(edited(("crop", "candidates", 0, key), None, delete=True))
    documents += [edited(("crop", "candidates", 0, "name"), v) for v in bad_names]
    documents += [edited(("crop", "candidates", 0, "uncertainty"), v) for v in bad_enums]
    documents += [edited(("crop", "candidates", 0, "evidence"), v)
                  for v in wrong_types[:4] + [["x"] * 6, [5], [""], ["x" * 201], ["a\nb"]]]
    documents += [edited(("crop", "candidates"), v) for v in wrong_types + [[1], ["x"], [None]]]
    documents.append(edited(("crop", "candidates"), [good()["crop"]["candidates"][0]] * (MAX_CROP_CANDIDATES + 1)))
    for key in ("candidate", "evidence", "uncertainty"):
        documents.append(edited(("growth_stage", key), None, delete=True))
    documents.append(edited(("growth_stage", "extra"), 1))
    documents += [edited(("growth_stage", "candidate"), v) for v in bad_names if v is not None]
    documents += [edited(("growth_stage", "uncertainty"), v) for v in bad_enums]
    documents += [edited(("growth_stage", "evidence"), v) for v in [None, 5, "x", {}, ["x"] * 6, [5], [""]]]
    documents.append(edited(("growth_stage",), {"candidate": None, "evidence": ["seen"], "uncertainty": "low"}))
    documents.append(edited(("growth_stage",), {"candidate": None, "evidence": [], "uncertainty": "unspecified"}))
    for key in ("observation", "possible_interpretations"):
        documents.append(edited(("symptoms", 0, key), None, delete=True))
    documents.append(edited(("symptoms", 0, "extra"), 1))
    documents += [edited(("symptoms", 0, "observation"), v) for v in bad_texts]
    documents += [edited(("symptoms", 0, "possible_interpretations"), v)
                  for v in [None, 5, "x", {}, ["x"] * (MAX_INTERPRETATIONS + 1), [5], [""], ["(x)"], []]]
    documents += [edited(("symptoms",), v)
                  for v in [None, "x", {}, [], [1], [None], [good()["symptoms"][0]] * (MAX_SYMPTOMS + 1)]]
    documents += [edited(("needs_more",), v)
                  for v in [None, "x", {}, [], [1], [""], ["x" * 301], ["x"] * (MAX_NEEDS + 1), ["a\nb"]]]
    return documents


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch("socket.socket", side_effect=AssertionError("socket used")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("urllib used"))):
            patcher.start()
            self.addCleanup(patcher.stop)


class TestTheRulesAgreeWithTheValidator(unittest.TestCase):
    def test_a_check_names_rules_exactly_when_the_validator_refuses_the_object(self):
        documents = battery()
        self.assertGreater(len(documents), 150)
        accepted = refused = 0
        for document in documents:
            with self.subTest(document=json.dumps(document)[:80]):
                valid = validate_findings(document)
                facts = facts_for(document)
                self.assertEqual(facts["findings_check"], "accepted" if valid else "contract_failed")
                if valid:
                    accepted += 1
                    self.assertEqual((facts["findings_rules"], facts["findings_rules_total"]), (None, None))
                    continue
                refused += 1
                rules = facts["findings_rules"]
                self.assertTrue(1 <= len(rules) <= MAX_RULES_SHOWN)
                self.assertTrue(all(set(r) == {"code", "path"} and r["code"] in RULE_CODES for r in rules))
                self.assertNotIn("UNDIAGNOSED_REJECTION", [r["code"] for r in rules])   # every refusal is explained
                self.assertNotIn("DIAGNOSTIC_ERROR", [r["code"] for r in rules])
                self.assertGreaterEqual(facts["findings_rules_total"], len(rules))
                self.assertEqual(clean_reply_facts(facts), facts)
        self.assertGreater(accepted, 3)
        self.assertGreater(refused, 150)

    def test_the_rules_equal_those_of_the_smoke_scripts_diagnostic(self):
        """Two implementations of the same rules (this one for the app, the script's for the standalone test) must
        name the same rules in the same order on every document, so neither can drift from the other."""
        for document in battery():
            with self.subTest(document=json.dumps(document)[:80]):
                self.assertEqual(findings_rules(document), smoke.diagnose_findings(document))

    def test_the_other_findings_steps_carry_no_rules(self):
        for text, check in ((json.dumps(good()), "accepted"), ("not json", "not_json"), ("[1]", "not_an_object"),
                            (json.dumps(good())[:-9], "not_json"), ("x" * 20001, "too_long")):
            facts = reply_facts_of(envelope(text))
            self.assertEqual(facts["findings_check"], check)
            self.assertEqual((facts["findings_rules"], facts["findings_rules_total"]), (None, None))
        for body in (b"{", envelope(""), json.dumps({"choices": [{"message": {"content": None}}]}).encode()):
            facts = reply_facts_of(body)
            self.assertEqual((facts["findings_rules"], facts["findings_rules_total"]), (None, None))
        self.assertEqual(set(FACT_KEYS) - set(facts), set())


class TestEachRuleIsNamedByCodeAndPath(unittest.TestCase):
    def rules(self, document):
        return [(r["code"], r["path"]) for r in facts_for(document)["findings_rules"]]

    def test_each_rule_has_its_code_and_structural_path(self):
        cases = (
            (edited(("crop",), None, delete=True), ("MISSING_REQUIRED_KEY", "crop")),
            (edited(("crop",), "crop-a"), ("WRONG_TYPE", "crop")),
            (edited(("crop",), ["crop-a"]), ("WRONG_TYPE", "crop")),
            (edited(("crop",), None), ("WRONG_TYPE", "crop")),
            (dict(good(), extra=1), ("EXTRA_FIELD", "")),
            (edited(("crop", "candidates", 0, "uncertainty"), "95%"), ("INVALID_ENUM_VALUE", "crop.candidates[0].uncertainty")),
            (edited(("growth_stage", "uncertainty"), "HIGH"), ("INVALID_ENUM_VALUE", "growth_stage.uncertainty")),
            (edited(("crop", "candidates", 0, "name"), "leaf spot (fungal)"),
             ("DISALLOWED_CHARACTER", "crop.candidates[0].name")),
            (edited(("growth_stage", "candidate"), "a/b"), ("DISALLOWED_CHARACTER", "growth_stage.candidate")),
            (edited(("symptoms", 0, "possible_interpretations", 1), "a:b"),
             ("DISALLOWED_CHARACTER", "symptoms[0].possible_interpretations[1]")),
            (edited(("symptoms", 0, "observation"), "x" * 301), ("TEXT_TOO_LONG", "symptoms[0].observation")),
            (edited(("needs_more",), ["ok", "a\nb"]), ("CONTROL_CHARACTER", "needs_more[1]")),
            (edited(("crop", "candidates", 0, "evidence"), ["x" * 201]), ("TEXT_TOO_LONG", "crop.candidates[0].evidence[0]")),
            (edited(("growth_stage", "evidence"), [""]), ("EMPTY_TEXT", "growth_stage.evidence[0]")),
            (edited(("needs_more",), ["x"] * (MAX_NEEDS + 1)), ("TOO_MANY_ITEMS", "needs_more")),
            (edited(("symptoms", 0, "possible_interpretations"), ["x"] * (MAX_INTERPRETATIONS + 1)),
             ("TOO_MANY_ITEMS", "symptoms[0].possible_interpretations")),
            (edited(("growth_stage",), {"candidate": None, "evidence": ["seen"], "uncertainty": "low"}),
             ("EVIDENCE_WITHOUT_CANDIDATE", "growth_stage")),
            (edited(("symptoms", 0, "observation"), 5), ("WRONG_TYPE", "symptoms[0].observation")),
            (edited(("crop", "candidates"), "x"), ("WRONG_TYPE", "crop.candidates")),
        )
        for document, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self.rules(document))

    def test_a_format_or_punctuation_character_a_model_might_use_is_named_by_its_class(self):
        # Characters that look harmless in prose but are refused: the diagnostic says which class, not which character.
        for value, code in (("zero​width", "CONTROL_CHARACTER"), ("it’s", "DISALLOWED_CHARACTER"),
                            ("a—b", "DISALLOWED_CHARACTER"), ("10°", "DISALLOWED_CHARACTER"),
                            ("a\tb", "CONTROL_CHARACTER")):
            with self.subTest(value=value):
                self.assertIn((code, "crop.candidates[0].name"),
                              self.rules(edited(("crop", "candidates", 0, "name"), value)))

    def test_several_broken_rules_are_all_listed_up_to_the_cap_and_counted(self):
        many = edited(("needs_more",), ["a\nb"] * MAX_NEEDS)
        many["symptoms"] = [{"observation": 5, "possible_interpretations": []}] * MAX_SYMPTOMS
        facts = facts_for(many)
        self.assertEqual(len(facts["findings_rules"]), MAX_RULES_SHOWN)
        self.assertEqual(facts["findings_rules_total"], MAX_NEEDS + MAX_SYMPTOMS)
        self.assertEqual(len({(r["code"], r["path"]) for r in facts["findings_rules"]}), MAX_RULES_SHOWN)

    def test_a_refusal_the_rules_cannot_explain_is_called_undiagnosed_not_guessed(self):
        with mock.patch("agri_pipeline.reply_facts.findings_rules", return_value=[]):
            facts = facts_for(dict(good(), extra=1))
        self.assertEqual(facts["findings_rules"], [{"code": "UNDIAGNOSED_REJECTION", "path": ""}])
        with mock.patch("agri_pipeline.reply_facts.findings_rules", side_effect=RuntimeError(CANARY)):
            facts = facts_for(dict(good(), extra=1))
        self.assertEqual(facts["findings_rules"], [{"code": "DIAGNOSTIC_ERROR", "path": ""}])
        self.assertNotIn(CANARY, json.dumps(facts))
        self.assertEqual(facts["findings_check"], "contract_failed")      # the verdict is unchanged either way


class TestNothingTheModelWroteIsKept(unittest.TestCase):
    def test_invented_keys_values_and_names_never_reach_the_facts(self):
        hostile = {
            CANARY: CANARY,
            "crop": {"candidates": [{"name": CANARY + " (x)", "evidence": [CANARY * 50, CANARY + "\n"],
                                     "uncertainty": CANARY, CANARY: 1}], CANARY + "2": 1},
            "growth_stage": {"candidate": None, "evidence": [CANARY], "uncertainty": CANARY, "diagnosis": CANARY},
            "symptoms": [{"observation": CANARY * 100, "possible_interpretations": [CANARY + "/"], CANARY: 1}, CANARY],
            "needs_more": [CANARY * 100, 5],
        }
        facts = facts_for(hostile)
        self.assertEqual(facts["findings_check"], "contract_failed")
        self.assertNotIn(CANARY, json.dumps(facts))
        self.assertEqual(clean_reply_facts(facts), facts)
        for entry in facts["findings_rules"]:
            self.assertRegex(entry["path"], r"^[a-z_]*(\.[a-z_]+|\[\d{1,2}\])*$")

    def test_the_extra_field_rule_names_only_the_object_it_was_found_in(self):
        facts = facts_for(edited(("crop", "candidates", 0, CANARY), 1))
        self.assertIn({"code": "EXTRA_FIELD", "path": "crop.candidates[0]"}, facts["findings_rules"])
        self.assertNotIn(CANARY, json.dumps(facts))


class TestTheAllowlist(unittest.TestCase):
    def facts(self):
        return facts_for(dict(good(), extra=1))

    def test_a_clean_copy_is_equal_but_independent(self):
        facts = self.facts()
        cleaned = clean_reply_facts(facts)
        self.assertEqual(cleaned, facts)
        self.assertIsNot(cleaned["findings_rules"], facts["findings_rules"])
        self.assertIsNot(cleaned["findings_rules"][0], facts["findings_rules"][0])

    def test_anything_outside_the_closed_form_drops_every_fact(self):
        facts = self.facts()
        entry = {"code": "EXTRA_FIELD", "path": ""}
        bad_entries = (
            {"code": CANARY, "path": ""}, {"code": "EXTRA_FIELD", "path": CANARY}, {"code": "EXTRA_FIELD", "path": "crop.x"},
            {"code": "EXTRA_FIELD", "path": "crop.candidates[]"}, {"code": "EXTRA_FIELD", "path": "symptoms[123]"},
            {"code": "EXTRA_FIELD", "path": "crop.candidates[0].name.x"}, {"code": "EXTRA_FIELD", "path": "x" * 200},
            {"code": "EXTRA_FIELD", "path": None}, {"code": None, "path": ""}, {"code": "EXTRA_FIELD"},
            dict(entry, extra=CANARY), "EXTRA_FIELD", None, 5, [])
        for bad in bad_entries:
            with self.subTest(bad=repr(bad)[:50]):
                self.assertIsNone(clean_reply_facts(dict(facts, findings_rules=[bad])))
        for rules in ("EXTRA_FIELD", {}, [], [entry] * (MAX_RULES_SHOWN + 1), 5):
            with self.subTest(rules=repr(rules)[:40]):
                self.assertIsNone(clean_reply_facts(dict(facts, findings_rules=rules)))
        for total in (None, -1, True, 1.5, "1", 10 ** 8, 0):
            with self.subTest(total=total):
                self.assertIsNone(clean_reply_facts(dict(facts, findings_rules_total=total)))
        # Rules are allowed only on a contract_failed check, and only together with their total.
        self.assertIsNone(clean_reply_facts(dict(facts, findings_check="accepted")))
        self.assertIsNone(clean_reply_facts(dict(facts, findings_rules=None)))
        accepted = reply_facts_of(envelope(json.dumps(good())))
        self.assertIsNone(clean_reply_facts(dict(accepted, findings_rules=[entry], findings_rules_total=1)))

    def test_every_path_the_diagnostic_can_produce_passes_the_allowlist(self):
        for document in battery():
            facts = facts_for(document)
            if facts["findings_check"] == "contract_failed":
                self.assertIsNotNone(clean_reply_facts(facts), json.dumps(document)[:80])


def build(testcase, body):
    store = RecordStore(":memory:")
    testcase.addCleanup(store.close)
    calls = []

    def transport(url, headers, request, timeout):
        calls.append(request)
        return 200, body

    observer = ObservingTransport(transport)
    analyzer = nvidia.NvidiaImageAnalyzer(transport=observer, environ={"NVIDIA_API_KEY": KEY})
    service = DemoService(store, findings_analyzer=analyzer, findings_observer=observer)
    return DemoApp(service, {HOST}), calls


def post(app):
    payload = {"scenario": "normal_operation", "image_png_base64": PNG_B64,
               "providers": {"image": "real", "research": "skip", "compute": "skip", "synthesis": "skip"}}
    body = json.dumps(payload).encode("utf-8")
    headers = {"host": HOST, "x-nelavi-demo": "1", "content-length": str(len(body)), "content-type": "application/json"}
    response = app.handle_request("POST", "/api/run-integrated", headers, io.BytesIO(body).read)
    return json.loads(response.body.decode("utf-8")), response.body.decode("utf-8")


class TestThroughTheRoute(NoNetwork):
    def test_a_refused_object_reports_its_rules_and_keeps_its_public_code(self):
        document = edited(("crop", "candidates", 0, "name"), "leaf spot (fungal)")
        document[CANARY] = CANARY
        app, calls = build(self, envelope(json.dumps(document)))
        out, text = post(app)
        stage = {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}["image_findings"]
        self.assertEqual((stage["status"], stage["error_code"]), ("failed", "response_malformed"))
        facts = out["findings_reply_facts"]
        self.assertEqual(facts["findings_check"], "contract_failed")
        self.assertEqual({(r["code"], r["path"]) for r in facts["findings_rules"]},
                         {("EXTRA_FIELD", ""), ("DISALLOWED_CHARACTER", "crop.candidates[0].name")})
        self.assertEqual(facts["findings_rules_total"], 2)
        self.assertEqual(len(calls), 1)                                         # no retry
        for forbidden in (CANARY, KEY, "Bearer", "fungal"):
            self.assertNotIn(forbidden, text)

    def test_an_accepted_object_has_no_rules_and_still_completes(self):
        app, calls = build(self, envelope(json.dumps(good())))
        out, _ = post(app)
        stage = {s["stage"]: s for s in out["integrated"]["integration"]["stages"]}["image_findings"]
        self.assertEqual(stage["status"], "completed")
        self.assertEqual((out["findings_reply_facts"]["findings_rules"],
                          out["findings_reply_facts"]["findings_rules_total"]), (None, None))
        self.assertEqual(len(calls), 1)


class TestThePage(unittest.TestCase):
    def function_text(self, name):
        body = SCRIPT[SCRIPT.index("function %s(" % name):]
        return body[:body.index("\n}\n")]

    def test_the_page_knows_exactly_the_servers_rule_codes(self):
        block = SCRIPT[SCRIPT.index("const IW_RULE_CODE = {"):]
        block = block[:block.index("};")]
        self.assertEqual(set(re.findall(r"\b([A-Z][A-Z_]{3,}):", block)), set(RULE_CODES))

    def test_the_page_shows_a_rule_only_if_its_code_and_path_have_the_closed_form(self):
        text = self.function_text("replyFactsLines")
        for fragment in ("IW_RULE_PATH.test", "hasOwnProperty", "slice(0, 12)", "Rule broken:", "Rules broken in all"):
            self.assertIn(fragment, text if fragment != "hasOwnProperty" else text)
        self.assertNotIn("innerHTML", SCRIPT)
        self.assertIn("IW_RULE_PATH", SCRIPT)
        self.assertNotIn("(which rule is not recorded)", SCRIPT)


if __name__ == "__main__":
    unittest.main()
