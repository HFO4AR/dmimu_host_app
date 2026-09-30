import random
import struct
import unittest

from dmimu.protocol import Decoder, crc16, encode_frame


def packet(kind, payload):
    raw = b"\x55\xaa\x01" + bytes([kind]) + payload
    return raw + struct.pack("<H", crc16(raw)) + b"\x0a"


class ProtocolTests(unittest.TestCase):
    def test_crc_reference(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)

    def test_all_split_positions(self):
        for kind, values in [(1, [1, 2, 9.8]), (2, [.1, .2, .3]), (3, [20, -30, 179]), (4, [1, 0, 0, 0])]:
            raw = encode_frame(kind, values, 9)
            for split in range(len(raw) + 1):
                d = Decoder()
                frames = d.feed(raw[:split]) + d.feed(raw[split:])
                self.assertEqual(len(frames), 1)
                self.assertEqual(frames[0].slave_id, 9)

    def test_large_interleaved_stream_random_chunks(self):
        cycle = encode_frame(1, [1, 2, 9.8]) + encode_frame(4, [1, 0, 0, 0]) + encode_frame(3, [2, 3, 4])
        raw = cycle * 5000
        rng = random.Random(7)
        d = Decoder()
        kinds = []
        i = 0
        while i < len(raw):
            n = rng.randint(1, 1500)
            kinds.extend(f.kind for f in d.feed(raw[i:i+n]))
            i += n
        self.assertEqual(kinds, [1, 4, 3] * 5000)
        self.assertEqual(d.crc_errors, 0)
        self.assertFalse(d.buffer)

    def test_corrupt_frame_resynchronizes(self):
        raw = bytearray(encode_frame(1, [1, 2, 3]))
        raw[8] ^= 8
        d = Decoder()
        frames = d.feed(b"noise\x55" + raw + encode_frame(3, [4, 5, 6]))
        self.assertEqual([f.kind for f in frames], [3])
        self.assertGreater(d.crc_errors, 0)

    def test_temperature_documented_variants(self):
        payload = struct.pack("<ffH", 40, 35.5, 10) + bytes(2)
        for extra in [b"", bytes(4)]:
            d = Decoder()
            raw = packet(5, payload + extra)
            frames = d.feed(raw[:19]) + d.feed(raw[19:])
            self.assertEqual(frames[0].values["interval_ms"], 10)
            self.assertEqual(len(frames[0].raw), len(raw))

    def test_status_frame(self):
        raw = packet(7, struct.pack("<HH", 3, 17) + bytes([1, 1, 1, 1, 1, 0, 0, 0]))
        f = Decoder().feed(raw)[0]
        self.assertEqual(f.values["slave_id"], 3)
        self.assertEqual(f.values["quaternion_enabled"], 0)

    def test_nonfinite_values_are_not_published(self):
        d = Decoder()
        self.assertEqual(d.feed(encode_frame(1, [float("nan"), 0, 1])), [])

    def test_legacy_crc_is_opt_in(self):
        raw = encode_frame(1, [1, 2, 3])
        raw = raw[:-3] + struct.pack("<H", crc16(raw[2:-3])) + raw[-1:]
        self.assertFalse(Decoder().feed(raw))
        self.assertEqual(len(Decoder(legacy_crc=True).feed(raw)), 1)

    def test_buffer_is_bounded_under_noise(self):
        d = Decoder()
        for _ in range(20):
            d.feed(b"a" * 10000)
        self.assertLess(len(d.buffer), 4)


if __name__ == "__main__":
    unittest.main()
