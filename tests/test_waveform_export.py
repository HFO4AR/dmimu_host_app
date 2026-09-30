import csv
from io import BytesIO, StringIO
import struct
import unittest

from openpyxl import load_workbook
from dmimu.waveform_export import export


class WaveformExportTests(unittest.TestCase):
    def data(self, kind):
        return {"format": kind, "source": "demo", "channels": {
            "angular_velocity": [[1700000000, .1, .2, .3], [1700000000.25, -.1, -.2, -.3]],
            "quaternion": [[1700000000.1, 1, 0, 0, 0]],
        }}

    def test_excel_reader_preserves_original_points_units_and_timestamps(self):
        content, mime = export(self.data("xlsx"))
        wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
        rows = list(wb["angular_velocity"].values)
        self.assertEqual(rows[0], ("time_s", "host_time_unix_s", "x [rad/s]", "y [rad/s]", "z [rad/s]"))
        self.assertEqual(rows[1], (0, 1700000000, .1, .2, .3))
        self.assertEqual(rows[2], (.25, 1700000000.25, -.1, -.2, -.3))
        self.assertEqual(list(wb["quaternion"].values)[0][2:], ("w [dimensionless]", "x [dimensionless]", "y [dimensionless]", "z [dimensionless]"))
        wb.close()

    def test_csv_no_synthetic_values_or_shared_sample_rate(self):
        content, mime = export(self.data("csv"))
        rows = list(csv.DictReader(StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["unit"], "rad/s")
        self.assertEqual(rows[1]["x"], "-0.1")
        self.assertEqual(rows[2]["w"], "1")
        self.assertEqual(rows[2]["roll"], "")

    def test_mat_level5_header_and_column_major_numeric_contract(self):
        raw, mime = export(self.data("mat"))
        self.assertTrue(raw.startswith(b"MATLAB 5.0 MAT-file,"))
        self.assertEqual(raw[124:128], b"\0\x01IM")
        pos = 128
        variables = {}
        while pos < len(raw):
            kind, size = struct.unpack_from("<II", raw, pos)
            self.assertEqual(kind, 14)
            end = pos + 8 + size
            elements = []
            cursor = pos + 8
            while cursor < end:
                dtype, n = struct.unpack_from("<II", raw, cursor)
                elements.append((dtype, raw[cursor + 8:cursor + 8 + n]))
                cursor += 8 + n + (-n % 8)
            variables[elements[2][1].decode()] = elements
            pos = end + (-size % 8)
        dims = struct.unpack("<ii", variables["angular_velocity"][1][1])
        self.assertEqual(dims, (2, 5))
        values = struct.unpack("<10d", variables["angular_velocity"][3][1])
        self.assertEqual(values, (0, .25, 1700000000, 1700000000.25, .1, -.1, .2, -.2, .3, -.3))

    def test_invalid_samples_rejected_before_file_generation(self):
        for channel in [{}, {"oops": [[1, 2]]}, {"euler": [[1, 2, 3, float("nan")]]},
                        {"euler": [[2, 1, 2, 3], [1, 1, 2, 3]]}, {"euler": [[1, True, 2, 3]]}]:
            with self.assertRaises(ValueError):
                export({"format": "csv", "channels": channel})
