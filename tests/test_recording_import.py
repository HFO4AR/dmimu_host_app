import base64
from io import BytesIO
import json
import struct
import tempfile
import unittest
from pathlib import Path

from dmimu.protocol import encode_frame
from dmimu.recordings import Recordings


class RecordingImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.recordings = Recordings(self.temp.name)
        self.raw = encode_frame(1, [1, 2, 9.80665])

    def tearDown(self): self.temp.cleanup()

    def official(self, *, footer=True, count=2, backwards=False):
        metadata = json.dumps(dict(StartedUtc="2026-01-01T00:00:00Z", Source="synthetic", BaudRate=921600,
                                   Protocol="bosch-api-v1", TimeBasis="host-receive-seconds")).encode()
        content = b"IMULOG01" + struct.pack("<i", len(metadata)) + metadata
        content += struct.pack("<di", .1, len(self.raw)) + self.raw
        content += struct.pack("<di", .05 if backwards else .2, len(self.raw)) + self.raw
        if footer: content += struct.pack("<diQ", .2, -1, count)
        return content

    def test_native_import_retains_time_data_and_deduplicates(self):
        data = self.official(); result = self.recordings.import_stream(BytesIO(data))
        self.assertTrue(result["complete"])
        self.assertEqual(result["samples"], 2)
        samples = list(self.recordings.samples(result["id"]))
        self.assertEqual(samples[0][0], .1)
        self.assertAlmostEqual(samples[0][1], 1767225600.1)
        self.assertEqual(samples[0][2].raw, self.raw)
        self.assertEqual(self.recordings.import_stream(BytesIO(data))["id"], result["id"])
        self.assertEqual(len(self.recordings.listing()), 1)

    def test_native_export_round_trip_is_original_raw(self):
        result = self.recordings.import_stream(BytesIO(self.official()))
        data = b"".join(self.recordings.official_export(result["id"]))
        imported = self.recordings.import_stream(BytesIO(data))
        before = list(self.recordings.samples(result["id"]))
        after = list(self.recordings.samples(imported["id"]))
        self.assertEqual(before, after)

    def test_interrupted_native_tail_is_explicitly_incomplete(self):
        result = self.recordings.import_stream(BytesIO(self.official(footer=False) + b"\x00"))
        self.assertFalse(result["complete"])
        self.assertEqual(result["samples"], 2)
        self.assertTrue(result["warning"])

    def test_bad_count_or_interior_order_leaves_no_artifact(self):
        for data in (self.official(count=9), self.official(backwards=True), b"IMULOG01\xff\xff\xff\xff"):
            with self.assertRaises(ValueError): self.recordings.import_stream(BytesIO(data))
        self.assertEqual(list(Path(self.recordings.directory).iterdir()), [])

    def test_own_format_validates_and_imports(self):
        header = dict(format="dmimu.raw", version=1, created_at=100, source="demo", port=None)
        item = dict(elapsed=.2, time=100.2, data=base64.b64encode(self.raw).decode())
        data = (json.dumps(header) + "\n" + json.dumps(item) + "\n").encode()
        result = self.recordings.import_stream(BytesIO(data))
        self.assertEqual(list(self.recordings.samples(result["id"]))[0][2].raw, self.raw)
        with self.assertRaises(ValueError): self.recordings.import_stream(BytesIO(data), maximum=4)


if __name__ == "__main__": unittest.main()
