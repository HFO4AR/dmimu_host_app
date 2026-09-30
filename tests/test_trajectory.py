"""Synthetic physical contracts for Agent trajectory estimation; no hardware."""
import math
import unittest

from dmimu.trajectory import GRAVITY, TrajectoryEstimator, euler_quaternion, normalize_quaternion, rotate_vector
from dmimu.trajectory_export import export

IDENTITY = [1., 0., 0., 0.]


def samples(stamp, acceleration=None, quaternion=None, gyro=None, measurement_time=None, slave_id=1):
    acceleration = [0, 0, GRAVITY] if acceleration is None else acceleration
    quaternion = IDENTITY if quaternion is None else quaternion
    gyro = [0, 0, 0] if gyro is None else gyro
    channels = [("acceleration", dict(zip("xyz", acceleration))),
                ("angular_velocity", dict(zip("xyz", gyro))),
                ("quaternion", dict(zip("wxyz", quaternion)))]
    extra = {} if measurement_time is None else {"measurement_time": measurement_time}
    return [{"channel": channel, "values": value, "time": stamp, "slave_id": slave_id, **extra} for channel, value in channels]


class TrajectoryTests(unittest.TestCase):
    def vector_close(self, actual, expected, places=8):
        self.assertEqual(len(actual), len(expected))
        for a, b in zip(actual, expected):
            self.assertAlmostEqual(a, b, places=places)

    def calibrated(self, quaternion=None, acceleration=None, source="live", options=None):
        engine = TrajectoryEstimator(options)
        engine.set_source(source, 1)
        engine.begin_reference()
        for i in range(211):
            t = 1000 + i * .01
            engine.consume(samples(t if source == "live" else 4000 + i * .0025, acceleration, quaternion,
                                   measurement_time=t if source == "playback" else None))
        self.assertIsNotNone(engine.reference)
        self.assertFalse(engine.active)
        return engine

    def test_proper_rotation_and_quaternion_validation(self):
        self.vector_close(rotate_vector([1, 0, 0], euler_quaternion({"roll": 0, "pitch": 0, "yaw": 90})), [0, 1, 0])
        self.vector_close(rotate_vector([0, 1, 0], euler_quaternion({"roll": 90, "pitch": 0, "yaw": 0})), [0, 0, 1])
        self.vector_close(rotate_vector([0, 0, 1], euler_quaternion({"roll": 0, "pitch": 90, "yaw": 0})), [1, 0, 0])
        self.assertIsNone(normalize_quaternion({"w": 0, "x": 0, "y": 0, "z": 0}))
        self.assertIsNone(normalize_quaternion({"w": math.nan, "x": 0, "y": 0, "z": 0}))
        self.assertIsNone(normalize_quaternion({"w": True, "x": 0, "y": 0, "z": 0}))

    def test_level_and_tilted_gravity_removal(self):
        # Closed-form body specific force for a +90-degree world-Y pitch.
        for q, a in [(IDENTITY, [0, 0, GRAVITY]), ([math.sqrt(.5), 0, math.sqrt(.5), 0], [-GRAVITY, 0, 0])]:
            engine = self.calibrated(q, a)
            self.assertTrue(engine.start())
            for i in range(401):
                engine.consume(samples(1003 + i * .01, a, q))
            self.vector_close(engine.position, [0, 0, 0])
            self.vector_close(engine.velocity, [0, 0, 0])
            self.assertTrue(engine.stationary)

    def test_constant_acceleration_physical_solution(self):
        engine = self.calibrated()
        engine.set_options({"zupt": False})
        engine.start()
        for i in range(201):
            engine.consume(samples(1003 + i * .01, [1, -.5, GRAVITY + .25]))
        self.vector_close(engine.velocity, [2, -1, .5])
        self.vector_close(engine.position, [2, -1, .5])
        self.assertAlmostEqual(engine.distance, math.hypot(2, -1, .5))

    def test_rotated_world_acceleration_matches_level_motion(self):
        q = [math.sqrt(.5), 0, math.sqrt(.5), 0]
        engine = self.calibrated(q, [-GRAVITY, 0, 0])
        engine.set_options({"zupt": False})
        engine.start()
        # +X world acceleration of 1 maps to +Z body at pitch +90.
        for i in range(201):
            engine.consume(samples(1003 + i * .01, [-GRAVITY, 0, 1], q))
        self.vector_close(engine.position, [2, 0, 0])
        self.vector_close(engine.velocity, [2, 0, 0])

    def test_receive_chunk_duplicates_are_averaged_once(self):
        engine = self.calibrated()
        engine.zupt = False
        engine.start()
        for i in range(201):
            batch = samples(1003 + i * .01, [2, 0, GRAVITY])
            batch.append({**batch[0], "values": {"x": 0, "y": 0, "z": GRAVITY}})
            engine.consume(batch)
        self.vector_close(engine.position, [2, 0, 0])
        self.assertEqual(engine.coalesced, 201)

    def test_four_times_playback_uses_recorded_time(self):
        engine = self.calibrated(source="playback")
        engine.set_options({"zupt": False})
        engine.start()
        for i in range(201):
            engine.consume(samples(4001 + i * .0025, [1, 0, GRAVITY], measurement_time=1003 + i * .01))
        self.vector_close(engine.position, [2, 0, 0])
        self.assertEqual(engine.snapshot()["timeBasis"], "recorded_time")
        self.assertEqual(engine.points[0]["host_time"], 4001)
        self.assertEqual(engine.points[0]["time"], 1003)

    def test_missing_playback_time_is_never_replaced_with_send_time(self):
        engine = TrajectoryEstimator()
        engine.set_source("playback", 1)
        engine.begin_reference()
        engine.consume(samples(4000))
        self.assertFalse(engine.calibrating)
        self.assertIn("原始采样时间", engine.message)

    def test_orientation_expiry_and_foreign_slave_pause(self):
        engine = self.calibrated()
        engine.start()
        engine.consume(samples(1003))
        engine.consume([samples(1003.06, [1, 0, GRAVITY])[0]])
        self.assertFalse(engine.active)
        self.assertIn("姿态", engine.message)
        self.vector_close(engine.velocity, [0, 0, 0])
        engine.start()
        foreign = samples(1004)
        foreign[-1]["slave_id"] = 2
        engine.consume(foreign)
        self.assertFalse(engine.active)
        self.assertIn("从机", engine.message)

    def test_gap_pause_source_and_reference_clear(self):
        engine = self.calibrated()
        engine.start()
        engine.consume(samples(1003, [1, 0, GRAVITY]))
        engine.consume(samples(1003.02, [1, 0, GRAVITY]))
        previous = engine.position.copy()
        engine.consume(samples(1004, [1, 0, GRAVITY]))
        self.assertFalse(engine.active)
        self.vector_close(engine.position, previous)
        self.vector_close(engine.velocity, [0, 0, 0])
        engine.start()
        engine.consume(samples(1005, [1, 0, GRAVITY]))
        self.vector_close(engine.position, previous)
        self.assertEqual(engine.segment, 2)
        engine.begin_reference()
        self.assertIsNone(engine.reference)
        self.assertEqual(engine.points, [])
        engine.set_source("demo", 2)
        self.assertFalse(engine.active)
        self.assertFalse(engine.calibrating)
        self.assertEqual(engine.source, "demo")

    def test_euler_fallback_and_quaternion_preference(self):
        engine = self.calibrated()
        engine.start()
        batch = [s for s in samples(1003) if s["channel"] != "quaternion"]
        batch.append({"channel": "euler", "values": {"roll": 0, "pitch": 0, "yaw": 0}, "time": 1003, "slave_id": 1})
        engine.consume(batch)
        self.assertEqual(engine.orientation_kind, "euler_zyx")
        both = samples(1003.01)
        both.append({"channel": "euler", "values": {"roll": 70, "pitch": 40, "yaw": 10}, "time": 1003.01, "slave_id": 1})
        engine.consume(both)
        self.assertEqual(engine.orientation_kind, "quaternion")
        self.vector_close(engine.linear, [0, 0, 0])

    def test_moving_or_wrong_gravity_cannot_capture_reference(self):
        for variant in ("moving", "no_gyro", "negative_gravity"):
            engine = TrajectoryEstimator()
            engine.set_source("live", 1)
            engine.begin_reference()
            for i in range(251):
                batch = samples(1000 + i * .01, [0, 0, -GRAVITY] if variant == "negative_gravity" else None,
                                gyro=[0, 0, .5] if variant == "moving" else None)
                if variant == "no_gyro":
                    batch = [s for s in batch if s["channel"] != "angular_velocity"]
                engine.consume(batch)
            self.assertIsNone(engine.reference)

    def test_zero_velocity_assumption_can_be_disabled(self):
        engine = self.calibrated()
        engine.start()
        for i in range(101):
            engine.consume(samples(1003 + i * .01, [.5, 0, GRAVITY]))
        self.assertGreater(engine.velocity[0], .4)
        for i in range(101):
            engine.consume(samples(1004.01 + i * .01))
        self.assertTrue(engine.stationary)
        self.vector_close(engine.velocity, [0, 0, 0])
        engine.set_options({"zupt": False})
        self.assertFalse(engine.stationary)
        with self.assertRaises(ValueError):
            engine.set_options({"zupt": 1})

    def test_monotonic_watchdog_and_point_duration_bounds(self):
        clock = [0.]
        engine = TrajectoryEstimator(clock=lambda: clock[0])
        engine.set_source("live", 1)
        engine.begin_reference()
        # Pose-only traffic must not indefinitely extend acceleration freshness.
        clock[0] = 1.3
        engine.consume([samples(1001)[2]])
        self.assertTrue(engine.check_idle())
        self.assertFalse(engine.calibrating)
        bounded = self.calibrated(options={"maxPoints": 5, "pointPeriod": 0})
        bounded.start()
        for i in range(10):
            bounded.consume(samples(1003 + i * .01, [1, 0, GRAVITY]))
        self.assertEqual(len(bounded.points), 5)
        self.assertFalse(bounded.active)
        self.vector_close(bounded.position, bounded.points[-1]["position"])
        duration = self.calibrated()
        duration.start()
        duration.consume(samples(1003))
        duration.pause()
        duration.start()
        duration.consume(samples(1130))
        self.assertFalse(duration.active)
        self.assertEqual(len(duration.points), 1)

    def test_high_rate_heavy_tailed_static_noise_and_first_sample_spike(self):
        engine = TrajectoryEstimator()
        engine.set_source("live", 1)
        engine.begin_reference()
        # Fully synthetic 2 kHz USB-group timestamps with a noisy tail and 1%
        # large bursts. Do not include captured device measurements in tests.
        for i in range(5001):
            tail = .8 * math.sin(i * 1.7) if i % 20 == 0 else 0
            a = [.13 * math.sin(i * .7) + tail, .09 * math.cos(i * .9), GRAVITY + .08 * math.sin(i * .3)]
            g = [.025 + .05 * math.sin(i * .4) ** 2, 0, 0]
            if i % 101 == 0:
                a[0], g[0] = 23, .39
            engine.consume(samples(1000 + i * .0005, a, gyro=g))
        self.assertIsNotNone(engine.reference)
        self.vector_close(engine.reference, [0, 0, GRAVITY], places=2)
        quality = engine.snapshot()["referenceQuality"]
        self.assertTrue(quality["ready"])
        self.assertGreater(quality["samples"], 3900)
        self.assertLess(quality["outlierFraction"], .10)
        self.assertLess(quality["accelerationRms"], .35)
        self.assertEqual(engine.options["stillGyro"], .035)
        self.assertEqual(engine.options["stillAcceleration"], .12)

    def test_reference_motion_noise_and_contamination_rejected_then_recovers(self):
        for kind in ("rotation", "shake", "slow_trend", "bursts"):
            engine = TrajectoryEstimator()
            engine.set_source("live", 1)
            engine.begin_reference()
            for i in range(251):
                t = i * .01
                x = .8 * math.sin(t * 12) if kind == "shake" else .6 * t if kind == "slow_trend" else 23 if kind == "bursts" and i % 3 == 0 else 0
                engine.consume(samples(1000 + t, [x, 0, GRAVITY], gyro=[.5 if kind == "rotation" else 0, 0, 0]))
            self.assertIsNone(engine.reference, kind)
            self.assertFalse(engine.snapshot()["referenceQuality"]["ready"])
            # A rolling window heals after motion leaves it, without re-clicking.
            for i in range(231):
                engine.consume(samples(1002.51 + i * .01))
            self.assertIsNotNone(engine.reference, kind)

    def test_reference_options_ranges_partial_updates_reset_and_private_quality(self):
        engine = self.calibrated()
        engine.start()
        engine.consume(samples(1003))
        for invalid in ({}, {"other": 1}, {"referenceGyroMax": True}, {"referenceAccelerationStd": math.nan},
                        {"referenceOutlierFraction": .21}, {"referenceSeconds": .9}, {"referenceSeconds": 11},
                        {"referenceGyroMax": .009}, {"referenceAccelerationStd": 3.01}, {"zupt": 1}):
            before = engine.snapshot()
            with self.assertRaises(ValueError):
                engine.set_options(invalid)
            self.assertEqual(engine.snapshot()["referenceOptions"], before["referenceOptions"])
            self.assertTrue(engine.active)
        engine.set_options({"zupt": False})
        self.assertTrue(engine.active)
        engine.set_options({"referenceGyroMax": .2, "referenceAccelerationStd": .5, "referenceOutlierFraction": .15, "referenceSeconds": 1})
        self.assertIsNone(engine.reference)
        self.assertFalse(engine.active)
        self.assertEqual(engine.points, [])
        engine.begin_reference()
        for i in range(111):
            engine.consume(samples(1004 + i * .01))
        self.assertIsNotNone(engine.reference)
        snapshot = engine.snapshot()
        snapshot["referenceQuality"]["reason"] = "changed"
        snapshot["referenceOptions"]["referenceSeconds"] = 99
        self.assertEqual(engine.snapshot()["referenceQuality"]["reason"], "ready")
        self.assertEqual(engine.options["referenceSeconds"], 1)
        self.assertFalse(engine.zupt)

    def test_export_payload_is_private_copy_and_three_formats(self):
        engine = self.calibrated()
        engine.start()
        engine.consume(samples(1003))
        for kind in ("csv", "xlsx", "mat"):
            payload = engine.export_payload(kind)
            self.assertTrue(payload["estimate"])
            self.assertEqual(payload["source"], "live")
            content, mimetype = export(payload)
            self.assertGreater(len(content), 100)
            self.assertTrue(mimetype)
        payload = engine.export_payload("csv")
        payload["points"][0]["position"][0] = 999
        self.assertNotEqual(engine.points[0]["position"][0], 999)
        with self.assertRaises(ValueError):
            engine.export_payload("png")


if __name__ == "__main__":
    unittest.main()
