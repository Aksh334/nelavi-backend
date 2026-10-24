"""Offline tests of the OpenRouter synthesis provider and its start-up flags.

The contract under test was read from OpenRouter's official documentation on 2026-10-20 (see the module docstring of
`synthesis_provider/openrouter.py`). Only fake transports are used and sockets and DNS are patched to fail, so these
tests show how the code behaves against stand-ins for the documented shapes; they do NOT show that the live service
behaves as documented. No live request has ever been made.
"""

import io
import json
import unittest
from unittest import mock

from demo_app import server as server_module
from image_analysis.nvidia import ResponseTooLarge, TransportError, TransportTimeout
from orchestration import synthesis
from synthesis_provider import chat, openrouter
from synthesis_provider.openrouter import OpenRouterSynthesisProvider

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "vendor/some-model:free"
KEY_ENV = "OPENROUTER_API_KEY"
KEY = "or-canary-not-a-real-key-0008"
SECRET_TEXT = "secret-provider-detail-5521"
ITEM_URL = "https://fao.org/a"
FINDINGS = {
    "crop": {"candidates": [{"name": "crop-a", "evidence": ["leaf shape"], "uncertainty": "medium"}]},
    "growth_stage": {"candidate": "stage-x", "evidence": ["plant height"], "uncertainty": "high"},
    "symptoms": [{"observation": "brown spots on lower leaves", "possible_interpretations": ["possible-cause-a"]}],
    "needs_more": ["a close-up of the underside of a leaf"],
}


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


def good():
    return {"statements": [{"text": "The source discusses soil moisture.", "cites": ["F:crop", "E1"]}],
            "uncertainties": ["The image findings are unverified."]}


def reply(content, finish="stop", **extra):
    document = {"id": "gen-1", "model": MODEL, "object": "chat.completion",
                "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    document.update(extra)
    return 200, json.dumps(document).encode("utf-8")


def inputs():
    research = {"items": [{"title": "A title", "url": ITEM_URL, "snippet": "A snippet.",
                           "published_date": "2025-01-02", "score": 0.91}]}
    return synthesis.inputs_from_results({"findings": FINDINGS}, research, "how should soil moisture be read")


def make(transport, **kwargs):
    kwargs.setdefault("environ", {KEY_ENV: KEY})
    return OpenRouterSynthesisProvider(kwargs.pop("model", MODEL), kwargs.pop("key_env_var", KEY_ENV),
                                       transport=transport, **kwargs)


class TestConfiguration(NoNetwork):
    def test_the_model_is_required_has_no_default_and_must_carry_an_organisation_prefix(self):
        for model in (None, "", "gpt", "a b", "/x", "vendor/", "vendor//x", "-v/x", 5, "v/" + "m" * 120):
            with self.subTest(str(model)[:20]):
                transport = Scripted(reply(json.dumps(good())))
                result = make(transport, model=model).synthesize(inputs())
                self.assertEqual((result.status, result.error_code, result.output, result.fake),
                                 ("error", "synthesis_unavailable", None, False))
                self.assertEqual(transport.calls, [])
        for model in ("openai/gpt-5.2", "anthropic/claude-sonnet-4.6", "vendor/some-model:free", "a.b/c_d-e+f"):
            self.assertTrue(openrouter.model_is_valid(model), model)

    def test_a_missing_key_sends_nothing_and_the_default_variable_name_is_documented_one(self):
        self.assertEqual(openrouter.DEFAULT_KEY_ENV_VAR, "OPENROUTER_API_KEY")
        for environ in ({}, {KEY_ENV: " "}, {KEY_ENV: "a\nb"}, {"OTHER": KEY}):
            transport = Scripted(reply(json.dumps(good())))
            result = make(transport, environ=environ).synthesize(inputs())
            self.assertEqual(result.error_code, "synthesis_unavailable")
            self.assertEqual(transport.calls, [])
            self.assertNotIn(KEY, repr(result))
        transport = Scripted(reply(json.dumps(good())))
        result = make(transport, key_env_var="MY_ROUTER_KEY", environ={"MY_ROUTER_KEY": KEY}).synthesize(inputs())
        self.assertEqual(result.status, "ok")
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer " + KEY)

    def test_the_endpoint_is_fixed_to_the_documented_one_and_cannot_be_configured(self):
        self.assertEqual(openrouter.ENDPOINT, ENDPOINT)
        with self.assertRaises(TypeError):
            OpenRouterSynthesisProvider(MODEL, KEY_ENV, endpoint="https://evil.example/v1")
        transport = Scripted(reply(json.dumps(good())))
        make(transport).synthesize(inputs())
        self.assertEqual(transport.calls[0]["url"], ENDPOINT)


class TestRequest(NoNetwork):
    def test_the_request_follows_the_documented_fields_and_carries_only_permitted_data(self):
        transport = Scripted(reply(json.dumps(good())))
        result = make(transport).synthesize(inputs())
        self.assertEqual((result.status, result.fake, result.model), ("ok", False, "openrouter:" + MODEL))
        (call,) = transport.calls
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + KEY)
        body = json.loads(call["body"])
        self.assertEqual(set(body), {"model", "messages", "max_tokens", "stream"})   # no temperature: see SEND_TEMPERATURE
        self.assertEqual((body["model"], body["stream"], body["max_tokens"]),
                         (MODEL, False, openrouter.DEFAULT_MAX_TOKENS))
        self.assertEqual(openrouter.DEFAULT_MAX_TOKENS, 2400)    # SY-7: was 800 until the live run that was cut off
        self.assertTrue(1 <= body["max_tokens"] <= 4096)
        self.assertNotIn("response_format", body)
        self.assertNotIn("provider", body)
        text = call["body"].decode("utf-8")
        for absent in (ITEM_URL, "fao.org", "0.91", "score", KEY, "record", "sensor"):
            self.assertNotIn(absent, text, absent)

    def test_structured_output_is_opt_in_strict_and_still_checked_locally(self):
        transport = Scripted(reply(json.dumps(good())))
        result = make(transport, structured_output=True).synthesize(inputs())
        self.assertEqual(result.status, "ok")
        body = json.loads(transport.calls[0]["body"])
        spec = body["response_format"]
        self.assertEqual(spec["type"], "json_schema")
        self.assertEqual((spec["json_schema"]["name"], spec["json_schema"]["strict"]), ("synthesis", True))
        schema = spec["json_schema"]["schema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["required"], ["statements", "uncertainties"])
        enum = schema["properties"]["statements"]["items"]["properties"]["cites"]["items"]["enum"]
        self.assertEqual(enum, sorted(synthesis.allowed_refs(inputs())))
        self.assertEqual(body["provider"], {"require_parameters": True})
        # a reply that breaks the rules is refused even when strict output was requested
        bad = reply(json.dumps({"statements": [{"text": "x y", "cites": ["E99"]}], "uncertainties": []}))
        self.assertEqual(make(Scripted(bad), structured_output=True).synthesize(inputs()).error_code,
                         "synthesis_response_malformed")

    def test_only_an_explicit_true_turns_structured_output_on(self):
        for value in (False, None, 1, "yes"):
            transport = Scripted(reply(json.dumps(good())))
            make(transport, structured_output=value).synthesize(inputs())
            self.assertNotIn("response_format", json.loads(transport.calls[0]["body"]))


class TestFailures(NoNetwork):
    def run_reply(self, outcome):
        transport = Scripted(outcome)
        result = make(transport).synthesize(inputs())
        self.assertEqual(len(transport.calls), 1)   # never a retry
        self.assertNotIn(KEY, repr(result))
        self.assertNotIn(SECRET_TEXT, repr(result))
        return result

    def error_body(self, code):
        return json.dumps({"error": {"code": code, "message": SECRET_TEXT + KEY,
                                     "metadata": {"raw": SECRET_TEXT}}}).encode("utf-8")

    def test_documented_http_statuses_map_to_the_closed_codes(self):
        expected = {400: "synthesis_rejected", 401: "synthesis_rejected", 402: "synthesis_rejected",
                    403: "synthesis_rejected", 429: "synthesis_rejected", 408: "synthesis_failed",
                    502: "synthesis_failed", 503: "synthesis_failed"}
        for status, code in expected.items():
            with self.subTest(status):
                result = self.run_reply((status, self.error_body(status)))
                self.assertEqual((result.status, result.error_code, result.output, result.fake),
                                 ("error", code, None, False))
                self.assertIn(code, synthesis.ERROR_CODES)

    def test_an_error_inside_an_http_200_reply_is_a_failure_not_a_success(self):
        cases = {
            "no output, top-level error 402": ((200, self.error_body(402)), "synthesis_rejected"),
            "no output, top-level error 502": ((200, self.error_body(502)), "synthesis_failed"),
            "top-level error without a code": ((200, b'{"error": {"message": "x"}}'), "synthesis_failed"),
            "error that is not an object": ((200, b'{"error": "boom"}'), "synthesis_failed"),
            "partial output with choice error": (reply(json.dumps(good()), "error",
                                                       choices=[{"finish_reason": "error",
                                                                 "error": {"code": 502, "message": SECRET_TEXT},
                                                                 "message": {"content": json.dumps(good())}}]),
                                                 "synthesis_failed"),
            "finish_reason error with content": (reply(json.dumps(good()), "error"), "synthesis_failed"),
        }
        for name, (outcome, code) in cases.items():
            with self.subTest(name):
                result = self.run_reply(outcome)
                self.assertEqual((result.status, result.error_code, result.output), ("error", code, None))

    def test_a_truncated_or_filtered_reply_is_malformed_and_never_repaired(self):
        truncated = json.dumps(good())[:40]
        self.assertEqual(self.run_reply(reply(truncated, "length")).error_code, "synthesis_response_malformed")
        self.assertEqual(self.run_reply(reply("", "content_filter")).error_code, "synthesis_response_malformed")

    def test_malformed_and_fabricated_replies_are_refused(self):
        cases = ("not json", "[]", json.dumps({"statements": [], "uncertainties": []}),
                 json.dumps({"statements": [{"text": "See https://evil.example", "cites": ["E1"]}], "uncertainties": []}),
                 json.dumps({"statements": [{"text": "x y", "cites": ["E2"]}], "uncertainties": []}),
                 json.dumps(dict(good(), extra=1)), "x" * (chat.MAX_REPLY_CHARS + 1))
        for text in cases:
            with self.subTest(text[:30]):
                self.assertEqual(self.run_reply(reply(text)).error_code, "synthesis_response_malformed")
        for body in (b"{no", b"[]", b"{}", b'{"choices": []}', b'{"choices": [{"message": {"content": null}}]}'):
            with self.subTest(body[:20]):
                self.assertEqual(self.run_reply((200, body)).error_code, "synthesis_response_malformed")

    def test_transport_failures_are_one_closed_code_with_no_retry_no_fallback_and_no_leak(self):
        for error in (TransportTimeout(), TimeoutError(KEY), TransportError(), OSError(KEY), ResponseTooLarge(),
                      RuntimeError(SECRET_TEXT + KEY)):
            with self.subTest(type(error).__name__):
                result = self.run_reply(error)
                self.assertEqual((result.status, result.error_code, result.output, result.fake),
                                 ("error", "synthesis_failed", None, False))

    def test_an_oversized_request_is_refused_before_anything_is_sent(self):
        big = inputs()
        big["evidence"][0]["snippet"] = "s" * (chat.MAX_REQUEST_BYTES + 1)
        transport = Scripted(reply(json.dumps(good())))
        result = make(transport).synthesize(big)
        self.assertEqual(result.error_code, "synthesis_response_malformed")
        self.assertEqual(transport.calls, [])


class TestBoundary(NoNetwork):
    def test_a_provider_reported_date_is_bounded_before_it_can_reach_a_provider(self):
        research = {"items": [{"title": "t" * 900, "url": ITEM_URL, "snippet": "s" * 3000,
                               "published_date": "2025-01-02" + "x" * 500, "score": 1.0}]}
        built = synthesis.inputs_from_results({"findings": FINDINGS}, research, "q q")
        (item,) = built["evidence"]
        self.assertIsNone(item["published_date"])
        self.assertEqual((len(item["title"]), len(item["snippet"])), (300, 1000))
        research["items"][0]["published_date"] = "2025-01-02"
        self.assertEqual(synthesis.inputs_from_results({"findings": FINDINGS}, research, "q q")
                         ["evidence"][0]["published_date"], "2025-01-02")


class TestStartupFlags(NoNetwork):
    BASE = ["--synthesis-provider", "openrouter", "--synthesis-model", MODEL]

    def run_main(self, argv, key=True):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server_module, "HTTPServer") as fake_server, \
                mock.patch.object(server_module, "DemoApp") as fake_app, \
                mock.patch.object(server_module, "synthesis_key_present", return_value=key) as present:
            instance = fake_server.return_value
            instance.server_address = ("127.0.0.1", 12345)
            instance.serve_forever.side_effect = KeyboardInterrupt
            code = server_module.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), fake_server, fake_app, present

    def test_a_valid_configuration_starts_and_states_where_data_goes_without_the_key(self):
        code, out, err, _, fake_app, present = self.run_main(self.BASE)
        self.assertEqual((code, err), (0, ""))
        self.assertIs(fake_app.call_args.args[0].state()["integrated"]["real_available"]["synthesis"], True)
        present.assert_called_once_with(KEY_ENV)
        for expected in ("REAL OpenRouter chat-completions service at openrouter.ai", MODEL, KEY_ENV, "not the links",
                         "unverified model text", "no fallback"):
            self.assertIn(expected, out)
        self.assertNotIn("strict structured output", out)
        self.assertNotIn(KEY, out + err)

    def test_without_a_model_flag_the_owner_selected_model_is_used_and_no_fallback_exists(self):
        self.assertEqual(openrouter.SELECTED_MODEL, "anthropic/claude-haiku-5.5")
        self.assertTrue(openrouter.model_is_valid(openrouter.SELECTED_MODEL))
        transport = Scripted(reply(json.dumps(good())))
        built = []
        real = server_module.OpenRouterSynthesisProvider

        def spy(*args, **kwargs):
            provider = real(*args, transport=transport, environ={KEY_ENV: KEY}, **kwargs)
            built.append(provider)
            return provider

        with mock.patch.object(server_module, "OpenRouterSynthesisProvider", spy):
            code, out, err, _, fake_app, present = self.run_main(["--synthesis-provider", "openrouter"])
        self.assertEqual((code, err), (0, ""))
        present.assert_called_once_with(KEY_ENV)
        self.assertIn("(model anthropic/claude-haiku-5.5,", out)
        self.assertNotIn(KEY, out)
        (provider,) = built
        self.assertEqual(provider.label, "openrouter:anthropic/claude-haiku-5.5")
        provider.synthesize(inputs())
        body = json.loads(transport.calls[0]["body"])
        self.assertEqual(body["model"], "anthropic/claude-haiku-5.5")
        self.assertNotIn("models", body)                  # a single model: no fallback list
        self.assertEqual(body["stream"], False)
        self.assertNotIn("response_format", body)         # structured output is not assumed for this model
        self.assertEqual(transport.calls[0]["url"], ENDPOINT)
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer " + KEY)

    def test_the_model_flag_overrides_the_default_and_the_provider_class_still_has_no_default(self):
        code, out, err, _, _, _ = self.run_main(["--synthesis-provider", "openrouter", "--synthesis-model", MODEL])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("(model %s," % MODEL, out)
        self.assertNotIn("claude-haiku-5.5", out)
        unconfigured = OpenRouterSynthesisProvider(environ={KEY_ENV: KEY}, transport=Scripted())
        self.assertEqual(unconfigured.synthesize(inputs()).error_code, "synthesis_unavailable")

    def test_the_default_model_does_not_enable_structured_output_or_change_validation(self):
        code, out, err, _, _, _ = self.run_main(["--synthesis-provider", "openrouter"])
        self.assertNotIn("structured output", out)
        transport = Scripted(reply(json.dumps({"statements": [{"text": "x y", "cites": ["E9"]}], "uncertainties": []})))
        result = make(transport, model=openrouter.SELECTED_MODEL).synthesize(inputs())
        self.assertEqual(result.error_code, "synthesis_response_malformed")
        self.assertEqual(len(transport.calls), 1)

    def test_the_key_variable_name_and_structured_output_are_reported_when_chosen(self):
        code, out, err, _, _, present = self.run_main(
            self.BASE + ["--synthesis-key-env", "MY_ROUTER_KEY", "--synthesis-structured-output"])
        self.assertEqual((code, err), (0, ""))
        present.assert_called_once_with("MY_ROUTER_KEY")
        self.assertIn("MY_ROUTER_KEY", out)
        self.assertIn("strict structured output requested", out)

    def test_every_misconfiguration_refuses_to_start_without_binding_and_prints_no_secret(self):
        cases = {
            "model without prefix": (["--synthesis-provider", "openrouter", "--synthesis-model", "gpt"], True,
                                     "organisation prefix"),
            "endpoint not allowed": (self.BASE + ["--synthesis-endpoint", "https://openrouter.ai/x"], True, "fixed"),
            "bad key name": (self.BASE + ["--synthesis-key-env", "bad name"], True, "variable NAME"),
            "no key": (self.BASE, False, KEY_ENV),
            "structured without provider": (["--synthesis-structured-output"], True, "only apply"),
            "structured with chat": (["--synthesis-provider", "chat", "--synthesis-endpoint",
                                      "https://chat.example.org/v1/chat", "--synthesis-model", "m1",
                                      "--synthesis-structured-output"], True, "only applies with"),
        }
        for name, (argv, key, expected) in cases.items():
            with self.subTest(name):
                code, out, err, fake_server, fake_app, _ = self.run_main(argv, key=key)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(expected, err)
                self.assertIn("Nothing was started", err)
                self.assertNotIn(KEY, err)
                fake_server.assert_not_called()
                fake_app.assert_not_called()


class TestNoTemperature(NoNetwork):
    """`temperature` is absent from the supported parameters of every listed endpoint of the selected model."""

    def sent_body(self, **kwargs):
        transport = Scripted(reply(json.dumps(good())))
        result = make(transport, **kwargs).synthesize(inputs())
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(transport.calls), 1)
        return json.loads(transport.calls[0]["body"]), transport.calls[0]["body"].decode("utf-8")

    def test_no_openrouter_request_carries_temperature_in_any_mode_or_for_any_model(self):
        for kwargs in ({}, {"structured_output": True}, {"model": openrouter.SELECTED_MODEL},
                       {"model": openrouter.SELECTED_MODEL, "structured_output": True}, {"model": "other/model"}):
            with self.subTest(kwargs):
                body, text = self.sent_body(**kwargs)
                self.assertNotIn("temperature", body)
                self.assertNotIn('"temperature"', text)
                self.assertEqual((body["stream"], body["max_tokens"]), (False, openrouter.DEFAULT_MAX_TOKENS))
        self.assertIs(OpenRouterSynthesisProvider.SEND_TEMPERATURE, False)

    def test_other_providers_still_send_temperature_zero(self):
        self.assertIs(chat.ChatSynthesisProvider.SEND_TEMPERATURE, True)
        transport = Scripted(reply(json.dumps(good())))
        generic = chat.ChatSynthesisProvider("https://chat.example.org/v1/chat/completions", "m1",
                                             "SYNTHESIS_API_KEY", transport=transport,
                                             environ={"SYNTHESIS_API_KEY": KEY})
        self.assertEqual(generic.synthesize(inputs()).status, "ok")
        body = json.loads(transport.calls[0]["body"])
        self.assertEqual((body["temperature"], body["stream"]), (0, False))
        self.assertEqual(set(body), {"model", "messages", "max_tokens", "temperature", "stream"})

    def test_structured_output_still_builds_its_request_without_temperature_and_is_still_validated(self):
        body, _ = self.sent_body(structured_output=True)
        self.assertEqual(set(body), {"model", "messages", "max_tokens", "stream", "response_format", "provider"})
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertIs(body["response_format"]["json_schema"]["strict"], True)
        self.assertEqual(body["provider"], {"require_parameters": True})
        bad = reply(json.dumps({"statements": [{"text": "x y", "cites": ["E9"]}], "uncertainties": []}))
        result = make(Scripted(bad), structured_output=True).synthesize(inputs())
        self.assertEqual(result.error_code, "synthesis_response_malformed")

    def test_the_request_is_otherwise_unchanged_and_validation_is_enforced(self):
        body, _ = self.sent_body()
        self.assertEqual((body["model"], [m["role"] for m in body["messages"]]), (MODEL, ["system", "user"]))
        for text in ("not json", json.dumps({"statements": [{"text": "See https://evil.example", "cites": ["E1"]}],
                                             "uncertainties": []}),
                     json.dumps({"statements": [{"text": "x y", "cites": ["E9"]}], "uncertainties": []})):
            transport = Scripted(reply(text))
            result = make(transport).synthesize(inputs())
            self.assertEqual((result.status, result.error_code), ("error", "synthesis_response_malformed"))
            self.assertEqual(len(transport.calls), 1)   # still no retry


if __name__ == "__main__":
    unittest.main()
