import csv
import json
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import struct

from dmimu.protocol import encode_frame, crc16
from dmimu.service import Fault, Service
from dmimu.storage import Settings


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.data = bytearray()
        self.writes = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, n):
        raw = bytes(self.data[:n])
        del self.data[:n]
        if not raw:
            time.sleep(.001)
        return raw

    def write(self, raw):
        self.writes.append(raw)
        return len(raw)

    def flush(self):
        pass

    def close(self):
        self.closed = True


def port(device="/fake/imu", manufacturer="DM-IMU", description="DM-IMU USB CDC", serial_number="ABC"):
    return SimpleNamespace(device=device, manufacturer=manufacturer, description=description, serial_number=serial_number, vid=1155, pid=22336)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(self.temp.name)
        self.service = Service(self.settings, serial_factory=FakeSerial, port_provider=lambda: [port()])
        self.service.auto_connect = False

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_connect_never_writes_configuration(self):
        self.service._perform("connect", {"port": "/fake/imu"})
        self.assertEqual(self.service.serial.writes, [])
        self.assertEqual(self.service.connection, "connected")

    def test_auto_detection_does_not_select_generic_ports(self):
        self.service.port_provider = lambda: [port(manufacturer="", description="Generic serial")]
        self.service.auto_connect = True
        self.service.start()
        time.sleep(.15)
        self.assertIsNone(self.service.serial)

    def test_ambiguous_devices_are_not_auto_selected(self):
        self.service.port_provider = lambda: [port(), port("/fake/imu2", serial_number="DEF")]
        self.service.auto_connect = True
        self.service.start()
        time.sleep(.15)
        self.assertIsNone(self.service.serial)
        self.assertIn("多个", self.service.error)

    def test_old_link_data_is_not_published_after_switch(self):
        old = FakeSerial()
        self.service.serial = FakeSerial()
        self.service._ingest(encode_frame(1, [1, 2, 3]), time.time(), old)
        self.assertFalse(self.service.latest)

    def test_stale_channels_are_independent(self):
        self.service.source = "live"
        self.service._ingest(encode_frame(1, [1, 2, 3]), time.time() - 3)
        self.service._ingest(encode_frame(3, [10, 20, 30]), time.time())
        channels = self.service.snapshot()["channels"]
        self.assertTrue(channels["acceleration"]["stale"])
        self.assertFalse(channels["euler"]["stale"])

    def test_idempotency_and_restart_preserve_unknown_operations(self):
        first = self.service.submit("demo", {}, "same-key")
        second = self.service.submit("demo", {}, "same-key")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(Fault):
            self.service.submit("disconnect", {}, "same-key")
        restarted = Service(self.settings, port_provider=lambda: [])
        self.assertEqual(restarted.operation(first["id"])["state"], "uncertain")

    def test_worker_reports_real_failure(self):
        self.service.start()
        op = self.service.submit("connect", {"port": "missing"}, "fail-connect")
        deadline = time.monotonic() + 2
        while self.service.operation(op["id"])["state"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(.01)
        result = self.service.operation(op["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "PORT_NOT_FOUND")

    def test_raw_recording_export_and_playback_preserve_units_and_values(self):
        self.service.source = "demo"
        raw = encode_frame(2, [.1, .2, .3]) + encode_frame(3, [10, 20, 30])
        self.service._ingest(raw, time.time())
        identifier = self.service._perform("record.start", {})["id"]
        self.service._ingest(raw[:7], time.time())
        self.service._ingest(raw[7:], time.time())
        self.service._perform("record.stop", {})
        result = self.service.recordings.export(identifier)
        self.assertEqual(result["samples"], 2)
        with self.service.recordings.path(identifier, "csv").open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["unit"], "rad/s")
        self.assertAlmostEqual(float(rows[0]["x"]), .1, places=6)
        self.service._perform("playback.open", {"id": identifier})
        self.service._perform("playback.control", {"position": self.service.playback["duration"]})
        self.assertEqual(self.service.snapshot()["source"], "playback")
        self.assertEqual(self.service.latest["euler"]["values"]["yaw"], 30)
        self.assertIsNone(self.service.serial)

    def test_recording_disallows_source_switch(self):
        self.service.source = "demo"
        self.service._ingest(encode_frame(1, [1, 2, 3]), time.time())
        self.service._perform("record.start", {})
        with self.assertRaises(Fault):
            self.service._perform("disconnect", {})

    def test_new_firmware_control_does_not_send_legacy_bytes(self):
        self.service._perform("connect", {"port": "/fake/imu"})
        with self.assertRaises(Fault) as fault:
            self.service._perform("device.configure", {"interval_ms": 10})
        self.assertEqual(fault.exception.code, "UNSUPPORTED_PROTOCOL")
        self.assertEqual(self.service.serial.writes, [])

    def test_invalid_legacy_parameters_are_rejected_before_any_write(self):
        self.service._perform("connect", {"port": "/fake/imu"})
        self.settings.values["protocol"] = "legacy-v1"
        with self.assertRaises(ValueError):
            self.service._perform("device.configure", {"acceleration_enabled": True, "interval_ms": 0})
        self.assertEqual(self.service.serial.writes, [])

    def test_legacy_calibration_is_not_claimed_completed(self):
        self.service._perform("connect", {"port": "/fake/imu"})
        self.settings.values["protocol"] = "legacy-v1"
        result = self.service._perform("device.calibrate", {"kind": "gyro", "acknowledged": True})
        self.assertTrue(result["uncertain"])
        self.assertIn(b"\xaa\x03\x02\x0d", self.service.serial.writes)

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.recordings.path("../../settings")

    def test_probe_requires_acknowledgement_and_real_device(self):
        with self.assertRaises(Fault) as error:
            self.service._perform("protocol.probe", {})
        self.assertEqual(error.exception.code, "ACKNOWLEDGEMENT_REQUIRED")
        with self.assertRaises(Fault) as error:
            self.service._perform("protocol.probe", {"acknowledged": True})
        self.assertEqual(error.exception.code, "NOT_LIVE")

    def test_probe_is_bounded_and_preserves_exact_response(self):
        self.service._perform("connect", {"port": "/fake/imu"})
        link = self.service.serial
        write = link.write
        data = b"\x55\xaa\x01\x07" + struct.pack("<HH", 1, 17) + bytes([1, 1, 1, 1, 1, 0, 0, 0])
        raw = data + struct.pack("<H", crc16(data)) + b"\x0a"
        def respond(packet):
            if packet == b"\xaa\x06\x01\x0d":
                self.service._ingest(raw, time.time())
            return write(packet)
        link.write = respond
        with patch.object(self.service.stop_event, "wait", return_value=False):
            result = self.service._perform("protocol.probe", {"acknowledged": True})
        self.assertEqual(link.writes, [b"\xaa\x06\x01\x0d"] * 3 + [b"\xaa\x06\x00\x0d"])
        self.assertTrue(result["state_report_observed"])
        self.assertFalse(result["configuration_write_supported"])
        path = self.settings.directory / "protocol-probes" / (result["id"] + ".json")
        self.assertEqual(len(json.loads(path.read_text())["chunks"]), 3)


if __name__ == "__main__":
    unittest.main()
