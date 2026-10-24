"""Tests for the crop-context schema. Crop, region and stage names are SYNTHETIC test values."""

import ast
import copy
import json
import math
import unittest
from pathlib import Path

import crop_context
from crop_context import schema as cc
from crop_context import ContextError, build_context, refresh, validate_context


def obs(parameter="soil.moisture", value=1.5, unit=None, source="sensor_observation", timestamp="sim:1",
        quality="ok", calibration="unknown"):
    return cc.observation(parameter, value, unit, source, timestamp, quality, calibration)


def codes(problems):
    return {(p["path"], p["code"]) for p in problems}


class TestIncompleteAndUnknown(unittest.TestCase):
    def test_an_empty_context_is_valid_and_lists_everything_that_is_missing(self):
        context = build_context()
        self.assertEqual(validate_context(context), [])
        self.assertEqual(context["missing"], ["crop_identity", "growth_stage", "location",
                                              "visual_observations", "soil_observations"])
        self.assertEqual((context["crop"]["status"], context["growth_stage"]["status"],
                          context["location"]["kind"]), ("unknown", "unknown", "none"))
        self.assertEqual(context["origin"], "unknown")
        self.assertEqual(context["version"], "1")

    def test_unknown_crop_is_never_filled_in_and_cannot_carry_candidates(self):
        context = build_context()
        self.assertEqual(context["crop"], {"status": "unknown", "candidates": []})
        broken = copy.deepcopy(context)
        broken["crop"]["candidates"] = [cc.candidate("crop-a", "inference")]
        self.assertIn(("crop.candidates", "must_be_empty_when_unknown"), codes(validate_context(broken)))

    def test_partial_contexts_are_valid_and_only_report_what_is_actually_missing(self):
        context = build_context(origin="simulated", observations=[obs()],
                                crop={"status": "user_provided", "candidates": [cc.candidate("crop-a", "user_provided")]})
        self.assertEqual(context["missing"], ["growth_stage", "location", "visual_observations"])

    def test_uncertain_growth_stage_keeps_its_evidence_and_uncertainty(self):
        stage = {"status": "candidate", "candidate": "stage-x", "provenance": "image_analysis",
                 "evidence": ["leaf size"], "uncertainty": "high"}
        context = build_context(growth_stage=stage)
        self.assertEqual(context["growth_stage"], stage)
        self.assertNotIn("growth_stage", context["missing"])
        for bad in (dict(stage, uncertainty="95%"), dict(stage, candidate=""), dict(stage, provenance="guess"),
                    dict(stage, status="user_provided")):
            with self.assertRaises(ContextError):
                build_context(growth_stage=bad)
        unknown = cc.empty_growth_stage()
        unknown["candidate"] = "stage-x"
        with self.assertRaises(ContextError):
            build_context(growth_stage=unknown)


class TestCropIdentityAndConflicts(unittest.TestCase):
    def test_a_detected_crop_keeps_alternatives_evidence_and_uncertainty_without_conflict(self):
        crop = {"status": "candidate", "candidates": [
            cc.candidate("crop-a", "image_analysis", ["leaf shape"], "medium"),
            cc.candidate("crop-b", "image_analysis", [], "high")]}
        context = build_context(crop=crop)
        self.assertEqual(context["crop"], crop)
        self.assertEqual(context["conflicts"], [])
        self.assertNotIn("crop_identity", context["missing"])

    def test_a_user_crop_that_differs_from_a_detected_one_is_a_conflict_not_resolved(self):
        crop = {"status": "user_provided", "candidates": [cc.candidate("Crop-A", "user_provided"),
                                                          cc.candidate("crop-b", "image_analysis")]}
        context = build_context(crop=crop)
        self.assertEqual([c["code"] for c in context["conflicts"]], ["crop_user_vs_detected"])
        self.assertEqual(context["crop"]["candidates"][0]["name"], "Crop-A")  # the user's stays primary

    def test_agreement_between_user_and_detection_is_not_a_conflict(self):
        crop = {"status": "user_provided", "candidates": [cc.candidate("Crop-A", "user_provided"),
                                                          cc.candidate("crop-a ", "image_analysis")]}
        self.assertEqual(build_context(crop=crop)["conflicts"], [])

    def test_a_detected_candidate_must_come_from_a_detecting_source(self):
        for provenance in ("user_provided", "sensor_observation"):
            with self.assertRaises(ContextError):
                build_context(crop={"status": "candidate", "candidates": [cc.candidate("crop-a", provenance)]})
        with self.assertRaises(ContextError):
            build_context(crop={"status": "user_provided", "candidates": [cc.candidate("crop-a", "inference")]})

    def test_malformed_candidates_are_rejected_without_echoing_values(self):
        bad_names = ("", "  ", "<script>", "x" * 65, "line\nbreak", 5, None, "a;b")
        for name in bad_names:
            with self.subTest(name=repr(name)):
                with self.assertRaises(ContextError) as raised:
                    build_context(crop={"status": "candidate", "candidates": [cc.candidate(name, "image_analysis")]})
                self.assertNotIn("script", json.dumps(raised.exception.problems))
        for extra in ({"status": "candidate", "candidates": [], "x": 1}, {"status": "maybe", "candidates": []},
                      {"status": "candidate", "candidates": "crop-a"}, "crop-a", None):
            if extra is None:
                continue
            with self.assertRaises(ContextError):
                build_context(crop=extra)
        too_many = {"status": "candidate", "candidates": [cc.candidate("c%d" % i, "inference") for i in range(11)]}
        with self.assertRaises(ContextError):
            build_context(crop=too_many)


class TestLocation(unittest.TestCase):
    def test_no_location_is_valid_and_listed_as_missing(self):
        self.assertIn("location", build_context()["missing"])

    def test_a_region_keeps_its_source_and_precision(self):
        location = dict(cc.empty_location(), kind="region", region="Region-One", source="user_provided",
                        precision_m=5000)
        context = build_context(location=location)
        self.assertEqual(context["location"], location)
        self.assertNotIn("location", context["missing"])

    def test_coordinates_need_a_source_and_valid_ranges_and_are_never_invented(self):
        good = dict(cc.empty_location(), kind="coordinates", latitude=12.5, longitude=77.25,
                    source="user_provided", precision_m=30)
        self.assertEqual(build_context(location=good)["location"], good)
        self.assertEqual(build_context(location=dict(good, precision_m=None))["location"]["precision_m"], None)
        for bad in (dict(good, latitude=91), dict(good, longitude=-181), dict(good, latitude=math.nan),
                    dict(good, longitude=True), dict(good, latitude="12.5"), dict(good, source=None),
                    dict(good, source="inference"), dict(good, source="image_analysis"),
                    dict(good, precision_m=0), dict(good, precision_m=-1), dict(good, region="Region-One")):
            with self.subTest(bad=str(bad)):
                with self.assertRaises(ContextError):
                    build_context(location=bad)

    def test_a_region_cannot_carry_coordinates_and_none_must_be_empty(self):
        with self.assertRaises(ContextError):
            build_context(location=dict(cc.empty_location(), kind="region", region="Region-One",
                                        source="user_provided", latitude=1.0))
        with self.assertRaises(ContextError):
            build_context(location=dict(cc.empty_location(), region="Region-One"))
        with self.assertRaises(ContextError):
            build_context(location=dict(cc.empty_location(), kind="region", source="user_provided"))


class TestSymptomsAreObservationsNotDiagnoses(unittest.TestCase):
    def test_interpretations_are_only_ever_possible(self):
        context = build_context(symptoms=[cc.symptom("brown spots on lower leaves", "photo-1", "image_analysis",
                                                     ["possible-cause-a", "possible-cause-b"])])
        entry = context["symptoms"][0]
        self.assertEqual([i["status"] for i in entry["possible_interpretations"]], ["possible", "possible"])
        self.assertIsNone(entry["confirmation"])
        self.assertNotIn("visual_observations", context["missing"])

    def test_an_interpretation_cannot_be_stated_as_confirmed_or_given_a_confidence(self):
        entry = cc.symptom("spots", None, "image_analysis", ["cause-a"])
        for status in ("confirmed", "likely", "diagnosed", ""):
            broken = copy.deepcopy(entry)
            broken["possible_interpretations"][0]["status"] = status
            with self.assertRaises(ContextError):
                build_context(symptoms=[broken])
        broken = copy.deepcopy(entry)
        broken["possible_interpretations"][0]["confidence"] = 0.9
        with self.assertRaises(ContextError):
            build_context(symptoms=[broken])

    def test_a_confirmation_needs_a_method_and_a_source(self):
        entry = cc.symptom("spots", None, "image_analysis", ["cause-a"],
                           confirmation={"method": "lab test by a pathologist", "source": "lab report 12"})
        self.assertEqual(build_context(symptoms=[entry])["symptoms"][0]["confirmation"]["method"],
                         "lab test by a pathologist")
        for confirmation in ({"method": "x"}, {"method": "", "source": "y"}, {"method": "x", "source": " "},
                             "confirmed", True):
            with self.assertRaises(ContextError):
                build_context(symptoms=[dict(entry, confirmation=confirmation)])

    def test_malformed_symptoms_are_rejected(self):
        for bad in (cc.symptom("", None, "image_analysis"), cc.symptom("x" * 301, None, "image_analysis"),
                    cc.symptom("spots", None, "made_up"), cc.symptom("spots", "r" * 65, "image_analysis"),
                    cc.symptom("spots", None, "image_analysis", ["<b>"]), "spots", {"observation": "x"}):
            with self.assertRaises(ContextError):
                build_context(symptoms=[bad])
        with self.assertRaises(ContextError):
            build_context(symptoms=[cc.symptom("s", None, "image_analysis")] * 21)


class TestObservations(unittest.TestCase):
    def test_value_unit_timestamp_quality_and_calibration_are_preserved_exactly(self):
        entries = [obs("soil.nitrogen", 39.4825, None, calibration="uncalibrated", timestamp="sim:12.667"),
                   obs("battery.voltage_v", 12.5987, "V", calibration="unknown"),
                   obs("air.humidity", None, None, quality="fault")]
        context = build_context(observations=entries)
        self.assertEqual(context["observations"], entries)
        self.assertIsNot(context["observations"][0], entries[0])  # a copy

    def test_an_uncalibrated_or_unitless_value_cannot_claim_to_be_calibrated(self):
        with self.assertRaises(ContextError):
            build_context(observations=[obs(unit=None, calibration="calibrated")])
        with self.assertRaises(ContextError):
            build_context(observations=[obs(unit="mg/kg", calibration="calibrated", quality="fault")])
        self.assertEqual(build_context(observations=[obs(unit="mg/kg", calibration="calibrated")])
                         ["observations"][0]["calibration"], "calibrated")

    def test_invalid_measurements_are_rejected(self):
        for bad in (obs(value=math.nan), obs(value=math.inf), obs(value=True), obs(value="1.5"),
                    obs(quality="good"), obs(calibration="mostly"), obs(source="guess"), obs(parameter=""),
                    obs(unit="x" * 17), obs(timestamp="t" * 65), {"parameter": "x"}, "x"):
            with self.assertRaises(ContextError):
                build_context(observations=[bad])

    def test_a_null_value_is_valid_and_soil_is_detected_by_parameter_name(self):
        context = build_context(observations=[obs("soil.nitrogen", None, quality="fault")])
        self.assertNotIn("soil_observations", context["missing"])
        self.assertIn("soil_observations", build_context(observations=[obs("air.humidity")])["missing"])

    def test_disagreeing_values_for_the_same_parameter_and_source_are_a_conflict(self):
        context = build_context(observations=[obs(value=1.0), obs(value=2.0)])
        self.assertEqual([c["code"] for c in context["conflicts"]], ["observation_values_differ"])
        self.assertEqual(build_context(observations=[obs(value=1.0), obs(value=1.0)])["conflicts"], [])
        self.assertEqual(build_context(observations=[obs(value=1.0), obs(value=2.0, source="user_provided")])
                         ["conflicts"], [])


class TestValidationAndPurity(unittest.TestCase):
    def test_shape_and_version_problems_are_reported_with_fixed_codes_only(self):
        context = build_context(origin="simulated")
        for broken, expected in ((dict(context, version="2"), ("version", "unsupported_version")),
                                 (dict(context, origin="mars"), ("origin", "unknown_value")),
                                 (dict(context, extra=1), ("", "wrong_shape")),
                                 ("text", ("", "wrong_shape")), (None, ("", "wrong_shape"))):
            self.assertIn(expected, codes(validate_context(broken)))

    def test_stale_derived_fields_are_detected(self):
        context = build_context(origin="simulated")
        context["missing"] = []
        self.assertIn(("missing", "does_not_match_the_content"), codes(validate_context(context)))
        self.assertEqual(validate_context(refresh(context)), [])

    def test_builders_do_not_modify_their_inputs_and_are_deterministic(self):
        crop = {"status": "candidate", "candidates": [cc.candidate("crop-a", "image_analysis", ["e"], "low")]}
        entries = [obs()]
        before = copy.deepcopy((crop, entries))
        first = build_context(origin="simulated", crop=crop, observations=entries)
        self.assertEqual((crop, entries), before)
        self.assertEqual(build_context(origin="simulated", crop=crop, observations=entries), first)
        first["crop"]["candidates"][0]["name"] = "changed"
        self.assertEqual(crop["candidates"][0]["name"], "crop-a")

    def test_a_valid_context_is_plain_json_and_survives_a_round_trip(self):
        context = build_context(origin="simulated", observations=[obs()],
                                symptoms=[cc.symptom("spots", None, "image_analysis", ["cause-a"])])
        self.assertEqual(json.loads(json.dumps(context)), context)
        self.assertEqual(validate_context(json.loads(json.dumps(context))), [])

    def test_the_package_exports_a_small_api_and_has_no_io_or_crop_data(self):
        self.assertEqual(set(crop_context.__all__), {"VERSION", "ContextError", "build_context", "refresh",
                                                     "validate_context"})
        source = Path(cc.__file__).read_text(encoding="utf-8")
        imports = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertEqual(imports, {"copy", "math", "unicodedata"})
        names = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
        for banned in ("open", "print", "socket", "environ"):
            self.assertNotIn(banned, names)
        code_only = "\n".join(ln for ln in source.lower().splitlines() if not ln.lstrip().startswith(("#", '"')))
        for crop in ("tomato", "wheat", "rice", "maize"):
            self.assertNotIn(crop, code_only)


if __name__ == "__main__":
    unittest.main()
