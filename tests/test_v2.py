import struct
import unittest
from unittest.mock import patch

from dmimu.protocol import Decoder, crc16, encode_frame
from dmimu import v2


def ack(command, payload, code=0):
    body = bytes((command, code)) + struct.pack("<H", len(payload)) + payload
    return b"\xa5" + body + struct.pack("<H", crc16(body)) + b"\x5a"


class V2ProtocolTests(unittest.TestCase):
    def test_known_requests_and_subcommands(self):
        self.assertEqual(v2.request(v2.READ_VERSION).hex(), "a50e00009dd75a")
        self.assertEqual(v2.request(v2.READ_CONFIGURATION).hex(), "a5100000ff8f5a")
        self.assertEqual(v2.request(v2.SIX_FACE_CONTROL, b"\0").hex(), "a5130100008b335a")
        self.assertEqual(v2.request(v2.SIX_FACE_CONTROL, b"\1").hex(), "a513010001aa235a")

    def test_mixed_serial_stream_split_every_byte(self):
        replies = []
        decoder = Decoder(control_callback=replies.append)
        stream = encode_frame(1, [1, 2, 3]) + ack(14, bytes((2, 1, 0, 0, 2, 3, 4, 5))) + encode_frame(3, [4, 5, 6])
        frames = []
        for b in stream:
            frames.extend(decoder.feed(bytes((b,))))
        self.assertEqual([f.channel for f in frames], ["acceleration", "euler"])
        self.assertEqual(v2.versions(replies[0].payload)["app_text"], "2.3.4.5")
        self.assertEqual(decoder.crc_errors, 0)
        self.assertEqual(decoder.discarded_bytes, 0)
        self.assertEqual(decoder.control_frames, 1)

    def test_corrupt_and_oversized_ack_resync(self):
        replies = []
        decoder = Decoder(control_callback=replies.append)
        broken = bytearray(ack(14, b"12345678"));broken[-3] ^= 1
        raw = b"\xa5\x10\0\xff\xff" + broken + ack(14, bytes(8)) + encode_frame(1, [1, 2, 3])
        self.assertEqual(len(decoder.feed(raw)), 1)
        self.assertEqual(len(replies), 1)
        self.assertGreater(decoder.crc_errors, 0)

    def config(self):
        # Synthetic independent layout: flags, interface, period, heat/temp,
        # uint16 IDs, baud codes, reserved, rotation, accel/bw/gyro/bw.
        return bytes((1, 0, 1, 1, 1, 2)) + struct.pack("<H", 25) + bytes((0, 48)) + struct.pack("<HH", 25, 26) + bytes((1, 5, 0, 7, 2, 2, 4, 1))

    def test_config_layout_optional_capabilities_and_range_order(self):
        values = v2.configuration(self.config())
        self.assertEqual(values["slave_id"], 25)
        self.assertEqual(values["master_id"], 26)
        self.assertEqual(values["can_baudrate"], 500000)
        self.assertEqual(values["uart_baudrate"], 921600)
        self.assertEqual(values["communication"], 2)
        self.assertEqual((values["accel_range"], values["gyro_range"]), (2, 4))
        self.assertNotIn("installation_rotation", v2.configuration(self.config()[:17]))
        self.assertNotIn("gyro_range", v2.configuration(self.config()[:18]))

    def test_status_fixed_point_signed_values_and_results(self):
        raw = bytes((1, 3, 0, 0)) + struct.pack("<4H3h3H", 300, 300, 12, 300, -15, 10, 5, 20, 25, 30)
        status = v2.static_status(raw)
        self.assertEqual(status["bias_deg_s"], [-.015, .01, .005])
        self.assertEqual(status["noise_deg_s"], [.02, .025, .03])
        self.assertEqual(status["stability_deg_s"], .12)
        six = bytes((3, 0, 6, 255, 1)) + struct.pack("<2H3h3H", 500, 500, -5, 2, 0, 10000, 9999, 10001)
        self.assertEqual(v2.six_face_status(six)["scale"], [1, .9999, 1.0001])
        for parser in (v2.static_status, v2.six_face_status):
            with self.assertRaises(ValueError):parser(bytes(10))

    def test_patch_preserves_other_range_and_all_validation_precedes_write(self):
        self.assertEqual(v2.configuration_commands({"accel_range": 1}, {"accel_range": 3, "gyro_range": 2}), [(0x16, b"\1\2")])
        with self.assertRaises(ValueError):
            v2.configuration_commands({"acceleration_enabled": True, "interval_ms": 0}, {})
        with self.assertRaises(ValueError):
            v2.configuration_commands({"installation_rotation": 4}, {})
