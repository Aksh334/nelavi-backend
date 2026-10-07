import unittest
from dataclasses import replace

from mock_rover.camera import CameraModel, CameraNotHomed
from mock_rover.config import RoverConfig
from mock_rover.simulator import MockRover


def make_config(**overrides):
    return replace(RoverConfig(), **overrides)


class TestCameraModel(unittest.TestCase):
    def test_initial_state(self):
        camera = CameraModel(make_config())
        self.assertIsNone(camera.pan_deg)
        self.assertEqual(camera.tilt_deg, 0.0)
        self.assertFalse(camera.homed)
        self.assertFalse(camera.led_on)
        self.assertEqual(camera.last_move_duration_s, 0.0)

    def test_default_simulator_tilt_convention(self):
        config = RoverConfig()  # defaults, nothing overridden
        self.assertEqual(config.tilt_initial_deg, 0.0)
        self.assertEqual((config.tilt_min_deg, config.tilt_max_deg), (-60.0, 45.0))
        camera = CameraModel(config)
        self.assertEqual(camera.tilt_deg, 0.0)
        camera.move_to(tilt_deg=-60.0)
        camera.move_to(tilt_deg=45.0)
        for outside in (-60.1, 45.1):
            with self.assertRaises(ValueError):
                camera.move_to(tilt_deg=outside)

    def test_homing_sets_the_simulator_defined_home(self):
        camera = CameraModel(make_config(pan_home_deg=5.0))
        duration = camera.home()
        self.assertTrue(camera.homed)
        self.assertEqual(camera.pan_deg, 5.0)
        self.assertGreater(duration, 0.0)
        self.assertEqual(camera.last_move_duration_s, duration)

    def test_homing_duration_depends_on_the_distance(self):
        camera = CameraModel(make_config(pan_speed_deg_s=10.0))
        camera.home()
        camera.move_to(pan_deg=100.0)
        self.assertAlmostEqual(camera.home(), 10.0)

    def test_pan_requires_homing(self):
        camera = CameraModel(make_config())
        with self.assertRaises(CameraNotHomed):
            camera.move_to(pan_deg=10.0)

    def test_pan_movement_and_duration(self):
        camera = CameraModel(make_config(pan_speed_deg_s=30.0))
        camera.home()
        duration = camera.move_to(pan_deg=90.0)
        self.assertEqual(camera.pan_deg, 90.0)
        self.assertAlmostEqual(duration, 3.0)
        self.assertAlmostEqual(camera.last_move_duration_s, 3.0)

    def test_tilt_state_duration_and_independence_from_homing(self):
        camera = CameraModel(make_config(tilt_speed_deg_s=20.0))
        duration = camera.move_to(tilt_deg=-40.0)
        self.assertEqual(camera.tilt_deg, -40.0)
        self.assertAlmostEqual(duration, 2.0)
        self.assertIsNone(camera.pan_deg)

    def test_combined_move_takes_the_longer_duration(self):
        camera = CameraModel(make_config(pan_speed_deg_s=30.0, tilt_speed_deg_s=30.0))
        camera.home()
        duration = camera.move_to(pan_deg=60.0, tilt_deg=15.0)
        self.assertAlmostEqual(duration, 2.0)

    def test_ranges_are_configurable_and_enforced(self):
        camera = CameraModel(make_config(pan_max_deg=100.0, tilt_min_deg=-10.0, tilt_max_deg=10.0))
        camera.home()
        for kwargs in ({"pan_deg": 101.0}, {"pan_deg": -1.0}, {"tilt_deg": 11.0}, {"tilt_deg": -11.0}):
            with self.assertRaises(ValueError):
                camera.move_to(**kwargs)
        camera.move_to(pan_deg=100.0, tilt_deg=10.0)

    def test_led_state(self):
        camera = CameraModel(make_config())
        camera.set_led(True)
        self.assertTrue(camera.led_on)
        camera.set_led(False)
        self.assertFalse(camera.led_on)


class TestCapture(unittest.TestCase):
    def test_capture_is_deterministic_and_cycles_fixtures(self):
        def run():
            camera = CameraModel(make_config(seed=4))
            return [camera.capture(0.0) for _ in range(4)]

        first, second = run(), run()
        self.assertEqual(first, second)
        fixtures = [c.fixture for c in first]
        self.assertEqual(fixtures[0], fixtures[3])
        self.assertEqual(len(set(fixtures)), 3)
        self.assertEqual(
            [c.photo_id for c in first],
            ["mock-photo-4-0001", "mock-photo-4-0002", "mock-photo-4-0003", "mock-photo-4-0004"],
        )

    def test_capture_records_led_and_pose(self):
        camera = CameraModel(make_config())
        camera.home()
        camera.move_to(pan_deg=45.0, tilt_deg=-20.0)
        camera.set_led(True)
        capture = camera.capture(7.5)
        self.assertTrue(capture.led_on)
        self.assertEqual((capture.pan_deg, capture.tilt_deg), (45.0, -20.0))
        self.assertEqual(capture.sim_time, 7.5)

    def test_unclear_switch(self):
        camera = CameraModel(make_config(unclear_confidence=40.0))
        camera.force_unclear = True
        capture = camera.capture(0.0)
        self.assertEqual(capture.label, "unclear")
        self.assertEqual(capture.confidence, 40.0)

    def test_custom_fixtures(self):
        camera = CameraModel(make_config(image_fixtures=(("only", "mock_x", 77.0),)))
        capture = camera.capture(0.0)
        self.assertEqual((capture.fixture, capture.label, capture.confidence), ("only", "mock_x", 77.0))


class TestCameraInRover(unittest.TestCase):
    def test_startup_homes_the_camera_and_takes_simulated_time(self):
        rover = MockRover(make_config())
        duration = rover.startup()
        self.assertTrue(rover.camera.homed)
        self.assertAlmostEqual(rover.sim_time, duration)
        self.assertGreater(duration, 0.0)

    def test_camera_movement_advances_time_and_exposes_duration(self):
        rover = MockRover(make_config(pan_speed_deg_s=30.0))
        rover.startup()
        before = rover.sim_time
        duration = rover.move_camera(pan=60.0)
        self.assertAlmostEqual(duration, 2.0)
        self.assertAlmostEqual(rover.sim_time - before, 2.0)
        self.assertEqual(rover.telemetry()["camera"]["last_move_duration_s"], duration)

    def test_capture_takes_simulated_time_and_stores_the_result(self):
        rover = MockRover(make_config(capture_duration_s=2.0))
        before = rover.sim_time
        capture = rover.capture_image()
        self.assertAlmostEqual(rover.sim_time - before, 2.0)
        self.assertIs(rover.last_capture, capture)
        rover.clear_capture()
        self.assertIsNone(rover.last_capture)

    def test_shutdown_turns_the_led_off(self):
        rover = MockRover(make_config())
        rover.set_led(True)
        rover.shutdown()
        self.assertFalse(rover.camera.led_on)
        self.assertFalse(rover.started)


if __name__ == "__main__":
    unittest.main()
