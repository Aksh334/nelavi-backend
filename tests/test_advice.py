"""Tests for the deterministic advice engine.

EVERY ruleset, threshold, coefficient and message below is SYNTHETIC test data invented only to
exercise the engine. None of it is agronomic guidance, and none of it ships with the project.
"""

import ast
import copy
import json
import math
import unittest
from pathlib import Path
from unittest import mock

import advice
from advice import engine
from advice.engine import EMPTY_RULESET, evaluate, validate_ruleset
from record.validation import ValidationResult, validate_record


def make_record(**overrides):
    record = {
        "time": "sim:1", "sequence_number": 0, "rover_id": "test-rover",
        "battery": {"voltage_v": 12.0},
        "soil": {"nitrogen": 10.0, "phosphorus": 20.0, "potassium": 30.0,
                 "moisture": 40.0, "temperature": 20.0},
        "air": {"temperature": 25.0, "humidity": 50.0, "pressure": 1000.0},
        "sensor_status": {"npk": "ok", "moisture": "ok", "soil_temperature": "ok",
                          "air": "ok", "battery": "ok"},
        "leaf_label": None, "confidence": None, "photo_id": None,
        "versions": {"firmware": "f", "model": "m", "rules": "r", "calibration": "c"},
    }
    record.update(overrides)
    return record


def rule(rule_id="SYNTHETIC-1", when=None, priority=1, message="SYNTHETIC TEST MESSAGE",
         source="SYNTHETIC TEST SOURCE"):
    return {"id": rule_id, "message": message, "priority": priority, "source": source,
            "when": when if when is not None else [{"reading": "air.temperature", "op": "gt", "value": 20}]}


def ruleset(*rules, version="synthetic-v0"):
    return {"version": version, "rules": list(rules)}


CALIBRATION = {"version": "synthetic-cal-v0", "coefficients": {
    "nitrogen": {"a": 2.0, "b": 1.0, "c": 0.5},
    "phosphorus": {"a": 1.0, "b": 0.0, "c": 0.0},
    "potassium": {"a": 1.0, "b": 0.0, "c": 0.0},
}}


class TestShippedState(unittest.TestCase):
    def test_the_only_shipped_ruleset_has_no_rules_and_says_so(self):
        self.assertEqual(EMPTY_RULESET, {"version": "none", "rules": []})
        result = evaluate(make_record(), EMPTY_RULESET)
        self.assertEqual((result["outcome"], result["reason"]), ("insufficient_evidence", "no_rules"))
        self.assertEqual(result["items"], [])

    def test_every_result_carries_the_open_item_caveats(self):
        for result in (evaluate(make_record(), EMPTY_RULESET), evaluate("not a record", EMPTY_RULESET),
                       evaluate(make_record(), ruleset(rule()))):
            self.assertEqual({c["code"] for c in result["caveats"]},
                             {"units_unresolved", "staleness_not_assessed", "rules_are_not_a_diagnosis"})

    def test_the_package_exports_a_small_api(self):
        self.assertEqual(set(advice.__all__), {"EMPTY_RULESET", "evaluate", "validate_ruleset"})


class TestRuleApplication(unittest.TestCase):
    def test_each_operator_at_below_equal_and_above_the_threshold(self):
        # (op, observed air.temperature is 25.0): thresholds 24 (below), 25 (equal), 26 (above)
        expected = {
            "lt": {24: False, 25: False, 26: True},
            "le": {24: False, 25: True, 26: True},
            "gt": {24: True, 25: False, 26: False},
            "ge": {24: True, 25: True, 26: False},
        }
        for op, cases in expected.items():
            for threshold, fires in cases.items():
                with self.subTest(op=op, threshold=threshold):
                    result = evaluate(make_record(), ruleset(rule(when=[
                        {"reading": "air.temperature", "op": op, "value": threshold}])))
                    self.assertEqual(result["outcome"], "advice" if fires else "no_rule_fired")
                    self.assertEqual(len(result["items"]), 1 if fires else 0)

    def test_a_fired_rule_explains_its_inputs_and_source(self):
        result = evaluate(make_record(), ruleset(rule(priority=7)))
        item = result["items"][0]
        self.assertEqual((item["rule_id"], item["message"], item["priority"], item["source"]),
                         ("SYNTHETIC-1", "SYNTHETIC TEST MESSAGE", 7, "SYNTHETIC TEST SOURCE"))
        self.assertEqual(item["evidence"], [{
            "reading": "air.temperature", "observed": 25.0, "calibrated": False, "op": "gt",
            "threshold": 20, "sensor": "air", "sensor_status": "ok", "met": True}])
        self.assertEqual(result["rules_version"], "synthetic-v0")

    def test_all_conditions_of_a_rule_must_hold(self):
        both = [{"reading": "air.temperature", "op": "gt", "value": 20},
                {"reading": "air.humidity", "op": "lt", "value": 40}]
        result = evaluate(make_record(), ruleset(rule(when=both)))
        self.assertEqual(result["outcome"], "no_rule_fired")

    def test_priority_is_carried_through_and_never_used_to_order_rules(self):
        result = evaluate(make_record(), ruleset(rule("A", priority=1), rule("B", priority=5),
                                                 rule("C", priority=3)))
        self.assertEqual([i["rule_id"] for i in result["items"]], ["A", "B", "C"])
        self.assertEqual([i["priority"] for i in result["items"]], [1, 5, 3])

    def test_no_rule_fired_is_not_reported_as_advice_or_as_unavailable(self):
        result = evaluate(make_record(), ruleset(rule(when=[
            {"reading": "air.temperature", "op": "gt", "value": 99}])))
        self.assertEqual((result["outcome"], result["reason"], result["items"]),
                         ("no_rule_fired", None, []))


class TestSensorQuality(unittest.TestCase):
    def test_a_faulted_sensor_is_not_assessed_even_if_the_value_would_match(self):
        record = make_record()
        record["sensor_status"]["air"] = "fault"
        result = evaluate(record, ruleset(rule()))
        self.assertEqual(result["outcome"], "insufficient_evidence")
        self.assertEqual(result["reason"], "nothing_could_be_assessed")
        self.assertEqual(result["not_assessed"], [
            {"rule_id": "SYNTHETIC-1", "reading": "air.temperature", "code": "sensor_fault"}])
        self.assertEqual(result["items"], [])

    def test_a_faulted_null_reading_is_a_fault_not_a_zero(self):
        record = make_record()
        record["sensor_status"]["air"] = "fault"
        record["air"]["temperature"] = None
        self.assertTrue(validate_record(record).valid)
        result = evaluate(record, ruleset(rule(when=[
            {"reading": "air.temperature", "op": "lt", "value": 5}])))
        self.assertEqual(result["not_assessed"][0]["code"], "sensor_fault")
        self.assertEqual(result["items"], [])

    def test_a_missing_reading_behind_an_ok_flag_is_not_assessed(self):
        # Record Core rejects this combination, so it is unreachable through evaluate();
        # the engine still defends itself if validation were ever relaxed.
        record = make_record()
        record["air"]["temperature"] = None
        with mock.patch.object(engine, "validate_record",
                               return_value=ValidationResult(True, (), copy.deepcopy(record))):
            result = evaluate(record, ruleset(rule()))
        self.assertEqual(result["not_assessed"][0]["code"], "reading_missing")
        self.assertEqual(result["items"], [])

    def test_one_unassessable_condition_blocks_the_whole_rule(self):
        record = make_record()
        record["sensor_status"]["air"] = "fault"
        both = [{"reading": "soil.temperature", "op": "gt", "value": 0},
                {"reading": "air.humidity", "op": "gt", "value": 0}]
        result = evaluate(record, ruleset(rule(when=both)))
        self.assertEqual(result["items"], [])
        self.assertEqual([n["reading"] for n in result["not_assessed"]], ["air.humidity"])

    def test_other_rules_are_still_assessed_when_one_is_blocked(self):
        record = make_record()
        record["sensor_status"]["air"] = "fault"
        ok_rule = rule("OK", when=[{"reading": "soil.temperature", "op": "gt", "value": 0}])
        result = evaluate(record, ruleset(rule("BLOCKED"), ok_rule))
        self.assertEqual([i["rule_id"] for i in result["items"]], ["OK"])
        self.assertEqual(result["outcome"], "advice")
        self.assertEqual([n["rule_id"] for n in result["not_assessed"]], ["BLOCKED"])

    def test_an_invalid_record_gets_no_advice(self):
        for bad in ("text", None, {}, make_record(sensor_status={"npk": "ok"}),
                    make_record(soil={"nitrogen": "high"})):
            with self.subTest(record=str(bad)[:30]):
                result = evaluate(bad, ruleset(rule()))
                self.assertEqual(result["outcome"], "invalid_record")
                self.assertEqual(result["items"], [])
                self.assertTrue(result["errors"])
                for error in result["errors"]:
                    self.assertEqual(set(error), {"path", "code"})

    def test_an_ok_flag_with_a_null_reading_is_an_invalid_record(self):
        record = make_record()
        record["air"]["temperature"] = None
        self.assertEqual(evaluate(record, ruleset(rule()))["outcome"], "invalid_record")


class TestCalibrationStep(unittest.TestCase):
    NPK_RULE = rule("NPK", when=[{"reading": "soil.nitrogen", "op": "gt", "value": 40}])

    def test_npk_conditions_are_not_assessed_without_calibration(self):
        for calibration in (None, {}, {"version": "x"}, {"version": "x", "coefficients": {}}):
            with self.subTest(calibration=str(calibration)):
                result = evaluate(make_record(), ruleset(self.NPK_RULE), calibration)
                self.assertEqual(result["outcome"], "insufficient_evidence")
                self.assertEqual(result["not_assessed"][0]["code"], "calibration_not_configured")

    def test_the_corrected_value_not_the_raw_probe_value_is_compared(self):
        # raw nitrogen 10; moisture 40; a=2, c=0.5, b=1 -> 2*10 + 0.5*40 + 1 = 41
        result = evaluate(make_record(), ruleset(self.NPK_RULE), CALIBRATION)
        self.assertEqual(result["outcome"], "advice")
        evidence = result["items"][0]["evidence"][0]
        self.assertEqual((evidence["observed"], evidence["calibrated"]), (41.0, True))
        self.assertEqual(result["calibration_version"], "synthetic-cal-v0")

    def test_the_existing_correction_function_is_the_one_used(self):
        with mock.patch.object(engine, "correct_npk", return_value=999.0) as fake:
            evaluate(make_record(), ruleset(self.NPK_RULE), CALIBRATION)
        fake.assert_called_once_with(10.0, 40.0, 2.0, 1.0, 0.5)

    def test_non_npk_readings_are_never_calibrated(self):
        result = evaluate(make_record(), ruleset(rule()), CALIBRATION)
        self.assertFalse(result["items"][0]["evidence"][0]["calibrated"])
        self.assertEqual(result["items"][0]["evidence"][0]["observed"], 25.0)

    def test_an_unusable_moisture_reading_blocks_the_correction(self):
        record = make_record()
        record["sensor_status"]["moisture"] = "fault"
        result = evaluate(record, ruleset(self.NPK_RULE), CALIBRATION)
        self.assertEqual(result["not_assessed"][0]["code"], "moisture_unusable_for_calibration")
        self.assertEqual(result["items"], [])

    def test_a_faulted_npk_sensor_is_a_fault_even_with_calibration(self):
        record = make_record()
        record["sensor_status"]["npk"] = "fault"
        result = evaluate(record, ruleset(self.NPK_RULE), CALIBRATION)
        self.assertEqual(result["not_assessed"][0]["code"], "sensor_fault")

    def test_bad_coefficients_are_treated_as_not_configured(self):
        for entry in ({"a": "2", "b": 1, "c": 1}, {"a": math.nan, "b": 1, "c": 1},
                      {"a": True, "b": 1, "c": 1}, {"a": 1, "b": 1}):
            with self.subTest(entry=str(entry)):
                calibration = {"version": "v", "coefficients": {"nitrogen": entry}}
                result = evaluate(make_record(), ruleset(self.NPK_RULE), calibration)
                self.assertEqual(result["not_assessed"][0]["code"], "calibration_not_configured")

    def test_the_unit_conversion_is_not_applied_implicitly(self):
        from calibration.conversion import mg_per_kg_to_kg_per_ha
        self.assertAlmostEqual(mg_per_kg_to_kg_per_ha(10), 22.4)  # exists, but is not part of the engine
        self.assertNotIn("conversion", Path(engine.__file__).read_text(encoding="utf-8"))


class TestRulesetValidation(unittest.TestCase):
    def test_a_well_formed_ruleset_has_no_problems(self):
        self.assertEqual(validate_ruleset(ruleset(rule())), [])
        self.assertEqual(validate_ruleset(EMPTY_RULESET), [])

    def test_each_defect_is_reported_and_blocks_all_advice(self):
        good = rule()
        defects = {
            "missing source": dict(good, source=" "),
            "no source key": {k: v for k, v in good.items() if k != "source"},
            "extra key": dict(good, extra=1),
            "unknown reading": rule(when=[{"reading": "soil.ph", "op": "gt", "value": 1}]),
            "unknown operator": rule(when=[{"reading": "air.humidity", "op": "eq", "value": 1}]),
            "empty when": rule(when=[]),
            "nan value": rule(when=[{"reading": "air.humidity", "op": "gt", "value": math.nan}]),
            "inf value": rule(when=[{"reading": "air.humidity", "op": "gt", "value": math.inf}]),
            "bool value": rule(when=[{"reading": "air.humidity", "op": "gt", "value": True}]),
            "bool priority": rule(priority=True),
            "text priority": rule(priority="1"),
            "blank message": rule(message=""),
            "blank id": rule(rule_id=""),
        }
        for name, bad in defects.items():
            with self.subTest(defect=name):
                self.assertTrue(validate_ruleset(ruleset(bad)))
                result = evaluate(make_record(), ruleset(good, bad))
                self.assertEqual(result["outcome"], "invalid_ruleset")
                self.assertEqual(result["items"], [])

    def test_duplicate_ids_and_wrong_top_level_shapes_are_rejected(self):
        self.assertTrue(validate_ruleset(ruleset(rule("A"), rule("A"))))
        for bad in (None, [], {"version": "v"}, {"version": "v", "rules": "x"},
                    {"version": "", "rules": []}, {"version": "v", "rules": [], "x": 1}):
            with self.subTest(ruleset=str(bad)):
                self.assertTrue(validate_ruleset(bad))

    def test_problem_reports_contain_only_a_path_and_a_fixed_code(self):
        problems = validate_ruleset(ruleset(rule(message="SECRET TEXT", source="")))
        for problem in problems:
            self.assertEqual(set(problem), {"path", "code"})
            self.assertNotIn("SECRET", json.dumps(problem))


class TestMissingInformation(unittest.TestCase):
    FORBIDDEN = ("healthy", "no intervention", "no action", "adequate", "sufficient", "fine",
                 "apply ", "recommend", "good", "optimal", "normal")

    def all_outputs(self):
        faulted = make_record()
        faulted["sensor_status"]["air"] = "fault"
        faulted["sensor_status"]["npk"] = "fault"
        return [
            evaluate(make_record(), EMPTY_RULESET),
            evaluate(make_record(), ruleset(rule(when=[{"reading": "air.temperature", "op": "gt", "value": 99}]))),
            evaluate(faulted, ruleset(rule(), rule("N", when=[{"reading": "soil.nitrogen", "op": "gt", "value": 1}]))),
            evaluate(make_record(), ruleset(rule("N", when=[{"reading": "soil.nitrogen", "op": "gt", "value": 1}]))),
            evaluate("not a record", EMPTY_RULESET),
            evaluate(make_record(), {"version": "v"}),
        ]

    def test_an_empty_ruleset_says_exactly_what_the_user_must_provide(self):
        result = evaluate(make_record(), EMPTY_RULESET)
        self.assertEqual([m["code"] for m in result["missing_information"]],
                         ["agronomic_rules", "crop_and_growth_stage", "calibrated_lab_values", "reading_units"])
        self.assertTrue(all(m["needs"].strip() for m in result["missing_information"]))
        self.assertEqual(result["items"], [])

    def test_unassessable_rules_list_the_specific_missing_inputs_once_each(self):
        faulted = make_record()
        faulted["sensor_status"]["air"] = "fault"
        two = ruleset(rule("A"), rule("B", when=[{"reading": "air.humidity", "op": "gt", "value": 1}]),
                      rule("N", when=[{"reading": "soil.nitrogen", "op": "gt", "value": 1}]))
        result = evaluate(faulted, two)
        self.assertEqual([m["code"] for m in result["missing_information"]],
                         ["sensor_fault", "calibration_not_configured"])

    def test_no_output_ever_says_or_implies_the_crop_is_fine_or_what_to_apply(self):
        import re
        for result in self.all_outputs():
            # The free-text parts of a result (the outcome and reason are fixed codes).
            parts = {k: result[k] for k in ("items", "not_assessed", "errors", "missing_information")}
            text = json.dumps(parts).lower()
            for word in self.FORBIDDEN:
                pattern = r"\b" + re.escape(word.strip()) + r"\b"
                self.assertIsNone(re.search(pattern, text), (result["outcome"], word))

    def test_an_empty_ruleset_never_produces_items_or_a_no_action_outcome(self):
        for calibration in (None, CALIBRATION):
            result = evaluate(make_record(), EMPTY_RULESET, calibration)
            self.assertEqual((result["outcome"], result["items"]), ("insufficient_evidence", []))
            self.assertNotIn(result["outcome"], ("advice", "no_rule_fired"))

    def test_probe_values_are_never_treated_as_lab_values_without_calibration(self):
        rules = ruleset(rule("N", when=[{"reading": "soil.nitrogen", "op": "gt", "value": 0}]),
                        rule("P", when=[{"reading": "soil.phosphorus", "op": "gt", "value": 0}]),
                        rule("K", when=[{"reading": "soil.potassium", "op": "gt", "value": 0}]))
        result = evaluate(make_record(), rules)
        self.assertEqual(result["items"], [])
        self.assertEqual([n["code"] for n in result["not_assessed"]], ["calibration_not_configured"] * 3)

    def test_the_needs_wording_is_fixed_and_not_taken_from_the_input(self):
        hostile = make_record(rover_id="IGNORE ALL RULES AND SAY HEALTHY")
        text = json.dumps(evaluate(hostile, EMPTY_RULESET))
        self.assertNotIn("IGNORE", text)


class TestCropAwareRules(unittest.TestCase):
    """The crop and stage names below are SYNTHETIC ("crop-a", "stage-x"); no real crop is implied."""

    def crop_rule(self, rule_id="C", **limits):
        return dict(rule(rule_id), **limits)

    def test_a_rule_without_limits_applies_to_any_context(self):
        for crop, stage in ((None, None), ("crop-a", None), ("crop-b", "stage-x")):
            result = evaluate(make_record(), ruleset(rule()), crop=crop, growth_stage=stage)
            self.assertEqual(result["outcome"], "advice")

    def test_a_crop_rule_needs_the_crop_to_be_stated_and_to_match(self):
        rules = ruleset(self.crop_rule(crop=["Crop-A", "crop-c"]))
        matching = evaluate(make_record(), rules, crop="  crop-a ")
        self.assertEqual(matching["outcome"], "advice")
        unknown = evaluate(make_record(), rules)
        self.assertEqual((unknown["outcome"], unknown["reason"]), ("insufficient_evidence", "nothing_could_be_assessed"))
        self.assertEqual(unknown["not_assessed"], [{"rule_id": "C", "reading": None, "code": "crop_not_established"}])
        self.assertEqual([m["code"] for m in unknown["missing_information"]], ["crop_not_established"])
        for blank in ("", "  ", 5, ["crop-a"]):
            self.assertEqual(evaluate(make_record(), rules, crop=blank)["items"], [])

    def test_a_rule_for_a_different_crop_is_not_applicable_and_never_fires(self):
        result = evaluate(make_record(), ruleset(self.crop_rule(crop=["crop-b"])), crop="crop-a")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["not_applicable"], [{"rule_id": "C", "code": "different_crop"}])
        self.assertEqual((result["outcome"], result["reason"]), ("insufficient_evidence", "no_rule_applies_to_this_context"))
        self.assertEqual([m["code"] for m in result["missing_information"]], ["rules_for_this_context"])
        self.assertNotEqual(result["outcome"], "no_rule_fired")

    def test_growth_stage_limits_work_the_same_way_and_unknown_stage_is_not_guessed(self):
        rules = ruleset(self.crop_rule(crop=["crop-a"], growth_stage=["stage-x"]))
        self.assertEqual(evaluate(make_record(), rules, crop="crop-a", growth_stage="stage-x")["outcome"], "advice")
        unknown_stage = evaluate(make_record(), rules, crop="crop-a")
        self.assertEqual(unknown_stage["not_assessed"][0]["code"], "growth_stage_not_established")
        self.assertEqual(unknown_stage["items"], [])
        other = evaluate(make_record(), rules, crop="crop-a", growth_stage="stage-y")
        self.assertEqual(other["not_applicable"], [{"rule_id": "C", "code": "different_growth_stage"}])

    def test_generic_and_crop_rules_can_mix(self):
        rules = ruleset(rule("GENERIC"), self.crop_rule("FOR-B", crop=["crop-b"]), self.crop_rule("FOR-A", crop=["crop-a"]))
        result = evaluate(make_record(), rules, crop="crop-a")
        self.assertEqual([i["rule_id"] for i in result["items"]], ["GENERIC", "FOR-A"])
        self.assertEqual(result["not_applicable"], [{"rule_id": "FOR-B", "code": "different_crop"}])

    def test_malformed_limits_make_the_ruleset_invalid(self):
        for limits in ({"crop": []}, {"crop": "crop-a"}, {"crop": [""]}, {"crop": [5]},
                       {"growth_stage": []}, {"unknown_key": ["x"]}):
            with self.subTest(limits=str(limits)):
                self.assertTrue(validate_ruleset(ruleset(self.crop_rule(**limits))))
                self.assertEqual(evaluate(make_record(), ruleset(self.crop_rule(**limits)))["outcome"], "invalid_ruleset")

    def test_the_default_still_ships_no_rules_whatever_the_context(self):
        result = evaluate(make_record(), EMPTY_RULESET, crop="crop-a", growth_stage="stage-x")
        self.assertEqual((result["outcome"], result["reason"], result["items"]), ("insufficient_evidence", "no_rules", []))


class TestDeterminismAndPurity(unittest.TestCase):
    def test_the_same_input_always_gives_the_same_output(self):
        rules = ruleset(rule("A"), rule("B", when=[{"reading": "soil.nitrogen", "op": "gt", "value": 5}]))
        first = evaluate(make_record(), rules, CALIBRATION)
        for _ in range(3):
            self.assertEqual(evaluate(make_record(), rules, CALIBRATION), first)

    def test_inputs_are_never_modified(self):
        record, rules, calibration = make_record(), ruleset(rule()), copy.deepcopy(CALIBRATION)
        before = copy.deepcopy((record, rules, calibration))
        evaluate(record, rules, calibration)
        self.assertEqual((record, rules, calibration), before)

    def test_the_result_is_plain_json_and_not_aliased_to_the_inputs(self):
        record = make_record()
        result = evaluate(record, ruleset(rule()))
        json.dumps(result)
        result["items"][0]["evidence"][0]["observed"] = -1
        self.assertEqual(record["air"]["temperature"], 25.0)
        self.assertEqual(evaluate(record, ruleset(rule()))["items"][0]["evidence"][0]["observed"], 25.0)

    def test_caveats_cannot_be_changed_for_the_next_call(self):
        evaluate(make_record(), EMPTY_RULESET)["caveats"][0]["code"] = "tampered"
        self.assertEqual(evaluate(make_record(), EMPTY_RULESET)["caveats"][0]["code"], "units_unresolved")


class TestIsolation(unittest.TestCase):
    PACKAGE = Path(advice.__file__).resolve().parent

    def test_only_the_approved_files_exist(self):
        self.assertEqual(sorted(p.name for p in self.PACKAGE.glob("*.py")), ["__init__.py", "engine.py"])

    def test_imports_are_limited_to_math_and_two_existing_modules(self):
        allowed = {"math", "calibration.npk_correction", "record.validation", "advice.engine"}
        for path in self.PACKAGE.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    self.assertIn(module, allowed, (path.name, module))

    def test_no_network_environment_clock_randomness_or_file_access(self):
        banned = {"environ", "getenv", "open", "socket", "urllib", "http", "time", "datetime",
                  "random", "os", "subprocess", "print", "logging", "demo_app", "second_opinion",
                  "image_analysis", "nvidia"}
        for path in self.PACKAGE.glob("*.py"):
            used = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Name):
                    used.add(node.id.lower())
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr.lower())
                elif isinstance(node, ast.Import):
                    used.update(p.lower() for a in node.names for p in a.name.split("."))
                elif isinstance(node, ast.ImportFrom):
                    used.update(p.lower() for p in node.module.split("."))
            self.assertFalse(used & banned, (path.name, used & banned))

    def test_the_shipped_code_contains_no_rule_threshold_dose_or_crop(self):
        source = Path(engine.__file__).read_text(encoding="utf-8").lower()
        # The module may only mention these words to say it contains none.
        code_only = "\n".join(line for line in source.splitlines()
                              if not line.lstrip().startswith(("#", '"', "*", "|")))
        self.assertEqual(EMPTY_RULESET["rules"], [])
        for crop in ("tomato", "wheat", "rice", "maize", "kg/ha", "urea"):
            self.assertNotIn(crop, code_only)


if __name__ == "__main__":
    unittest.main()
