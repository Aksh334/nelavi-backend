"""Checks the adapter's assumptions against the REAL installed Nodus SDK (nodus-compute 1.0.23), without any network.

They run only where `nodus` can be imported (the isolated `nodus-py313` environment). In the ordinary CropSync
environment they are skipped. Importing the SDK is local (it loads submodules lazily and builds no client); sockets are
blocked while these tests run, and nothing here calls Function.from_name, spawn, remote, get or cancel on a real call.

Run them with the isolated interpreter, from the repository root:
    <env>\\Scripts\\python.exe -B -m unittest discover -s tests -p test_nodus_sdk_contract.py
"""

import asyncio
import importlib.util
import inspect
import unittest
from unittest import mock

from orchestration import nodus_adapter
from orchestration.nodus_adapter import NodusTaskRunner

SDK_AVAILABLE = importlib.util.find_spec("nodus") is not None


class Handle:
    def __init__(self, error):
        self.error = error

    def spawn(self, payload):
        outer = self

        class Call:
            def get(self, timeout=None):
                raise outer.error

        return Call()


def code_for(error):
    return NodusTaskRunner(lambda: Handle(error)).run("summarize_record", {}).error_code


@unittest.skipUnless(SDK_AVAILABLE, "the Nodus SDK is not importable in this environment")
class SdkContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch("socket.socket", side_effect=AssertionError("socket used")):
            import nodus
            import nodus.errors
            import nodus.functions
            import nodus._serialize
            import nodus._spec
            import nodus.api._transport
        cls.nodus = nodus

    def setUp(self):
        self.socket_guard = mock.patch("socket.socket", side_effect=AssertionError("socket used"))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)

    def test_the_installed_version_is_the_one_the_notes_were_written_against(self):
        self.assertEqual(self.nodus.__version__, "1.0.23")

    ALL_NAMES = None

    def real(self, name):
        """The real SDK class. Some are exported from `nodus.errors`; the rest only live in `nodus.errors._codes`."""
        errors = self.nodus.errors
        return getattr(errors, name, None) or getattr(errors._codes, name, None) or getattr(errors._base, name, None)

    def test_every_class_the_adapter_names_exists_in_the_sdk_derives_from_nodus_error_and_lives_in_nodus(self):
        names = (nodus_adapter._LIMIT + nodus_adapter._UNAVAILABLE + nodus_adapter._REJECTED
                 + ("APITimeoutError", "FunctionCallFailed", "RemoteError", "NodusError"))
        for name in names:
            with self.subTest(name=name):
                cls = self.real(name)
                self.assertIsNotNone(cls, name)
                self.assertTrue(issubclass(cls, self.nodus.errors.NodusError))
                self.assertEqual(cls.__module__.split(".")[0], "nodus")

    def test_some_mapped_classes_are_not_exported_from_the_public_errors_module_in_this_version(self):
        # Informational and pinned: the adapter matches by class name and module, so it handles them either way,
        # but a caller who wrote `except nodus.errors.<Name>` could not catch these by that spelling in 1.0.23.
        hidden = sorted(n for n in (nodus_adapter._LIMIT + nodus_adapter._UNAVAILABLE + nodus_adapter._REJECTED)
                        if not hasattr(self.nodus.errors, n))
        self.assertEqual(hidden, ["ComputeSuspended", "FunctionNotFound", "InternalError", "MethodNotAllowed",
                                  "PaymentsNotConfigured", "RequestEntityTooLarge", "TrialLimitExceeded"])

    def test_the_documented_inheritance_the_adapter_relies_on(self):
        errors = self.nodus.errors
        self.assertTrue(issubclass(errors.APITimeoutError, errors.APIConnectionError))
        self.assertFalse(issubclass(errors.NodusError, TimeoutError))
        self.assertFalse(issubclass(errors.FunctionCallFailed, errors.APIConnectionError))

    def test_each_mapped_real_class_gives_the_expected_closed_code(self):
        errors = self.nodus.errors
        expected = {}
        expected.update({n: "task_limit_reached" for n in nodus_adapter._LIMIT})
        expected.update({n: "task_runner_unavailable" for n in nodus_adapter._UNAVAILABLE})
        expected.update({n: "task_rejected" for n in nodus_adapter._REJECTED})
        expected.update({"APITimeoutError": "task_timeout", "NodusError": "task_failed"})
        for name, code in expected.items():
            with self.subTest(name=name):
                self.assertEqual(code_for(self.real(name)("secret message")), code)

    def test_function_call_failed_with_a_deadline_is_a_timeout_and_other_reasons_are_failures(self):
        failed = self.nodus.errors.FunctionCallFailed
        self.assertEqual(code_for(failed("call-1", reason="DeadlineExceeded", message="m")), "task_timeout")
        self.assertEqual(code_for(failed("call-1", reason="Evicted", message="m")), "task_failed")
        self.assertEqual(code_for(failed("call-1", reason=None)), "task_failed")

    def test_a_real_remote_timeout_error_is_chained_and_therefore_a_failure_not_a_timeout(self):
        raised = None
        try:
            self.nodus._serialize.raise_remote({"type": "TimeoutError", "message": "inside the function",
                                                "traceback": "tb"})
        except TimeoutError as error:
            raised = error
        self.assertIsNotNone(raised)
        self.assertEqual(type(raised.__cause__).__name__, "RemoteError")
        self.assertEqual(code_for(raised), "task_failed")

    def test_a_real_remote_exception_borrowing_an_sdk_error_name_is_a_failure(self):
        for type_name in ("nodus.errors.InsufficientCredits", "nodus.errors.Unavailable", "ValueError"):
            try:
                self.nodus._serialize.raise_remote({"type": type_name, "message": "m", "traceback": "tb"})
            except Exception as error:
                self.assertEqual(type(error.__cause__).__name__, "RemoteError", type_name)
                self.assertEqual(code_for(error), "task_failed", type_name)

    def test_an_unimportable_remote_type_surfaces_as_remote_error_and_is_a_failure(self):
        try:
            self.nodus._serialize.raise_remote({"type": "my_pkg.NoSuchError", "message": "m", "traceback": "tb"})
        except Exception as error:
            self.assertEqual(type(error).__name__, "RemoteError")
            self.assertEqual(code_for(error), "task_failed")

    def test_the_real_local_wait_timeout_is_a_timeout_with_no_remote_cause(self):
        # Windows' asyncio event loop needs a loopback socket pair, so the blanket socket guard is lifted for this one
        # test and replaced by guards on every way of reaching another host. The wait is a sleep: nothing is contacted.
        async def wait():
            await asyncio.wait_for(asyncio.sleep(5), 0.01)

        self.socket_guard.stop()
        try:
            with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS used")), \
                    mock.patch("socket.create_connection", side_effect=AssertionError("connection used")):
                try:
                    asyncio.run(wait())
                except Exception as error:
                    self.assertIsInstance(error, TimeoutError)
                    self.assertNotEqual(type(error.__cause__).__name__, "RemoteError")
                    self.assertEqual(code_for(error), "task_timeout")
                else:
                    self.fail("the wait should have timed out")
        finally:
            self.socket_guard.start()

    def test_the_call_objects_have_the_methods_the_adapter_uses(self):
        call = self.nodus.functions._FunctionCall
        function = self.nodus.functions._Function
        get = inspect.signature(call.get)
        self.assertEqual(list(get.parameters), ["self", "timeout"])
        self.assertIsNone(get.parameters["timeout"].default)
        self.assertEqual(list(inspect.signature(call.cancel).parameters), ["self"])
        self.assertTrue(inspect.iscoroutinefunction(function.spawn))
        self.assertEqual(list(inspect.signature(function.from_name).parameters), ["app", "name", "project"])
        self.assertIs(function.lookup.__func__, function.from_name.__func__)
        self.assertTrue(hasattr(self.nodus.Function, "from_name"))     # the public wrapper exposes it

    def test_names_are_normalised_the_same_way_for_the_app_the_function_and_the_lookup(self):
        dns = self.nodus._spec.dns_name
        self.assertEqual(dns("cropsync-summary", limit=40), "cropsync-summary")
        self.assertEqual(dns("summarize_record"), "summarize-record")
        self.assertEqual(dns("cropsync-summary", "summarize_record"), "cropsync-summary-summarize-record")

    def test_the_transport_retry_budget_that_makes_spawn_unbounded_by_the_wait_timeout(self):
        self.assertEqual(self.nodus.api._transport.MAX_RETRY_SECONDS, 300.0)

    def test_arguments_travel_as_cloudpickle_and_a_small_payload_is_sent_inline(self):
        serialize = self.nodus._serialize
        self.assertEqual(serialize.INLINE_LIMIT, 64 * 1024)
        data = serialize.encode_call(({"soil": {}},), {}, None)
        self.assertLess(len(data), serialize.INLINE_LIMIT)
        self.assertEqual(serialize.decode_call(data)[1], ({"soil": {}},))

    def test_money_is_a_usd_decimal_string_and_negative_or_overprecise_amounts_are_refused(self):
        usd = self.nodus._spec.usd
        self.assertEqual(usd(0.05), "0.05")
        self.assertEqual(usd(1), "1.00")
        for bad in (-1, "x", 0.0000001, float("nan")):
            with self.assertRaises(self.nodus.errors.Invalid):
                usd(bad)

    def test_the_adapter_still_does_not_import_the_sdk_by_itself(self):
        source = inspect.getsource(nodus_adapter)
        self.assertNotIn("import nodus", source)


if __name__ == "__main__":
    unittest.main()
