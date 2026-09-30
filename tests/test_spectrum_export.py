import csv
from io import BytesIO, StringIO
import unittest

from openpyxl import load_workbook
from dmimu.spectrum_export import export


class SpectrumExportTests(unittest.TestCase):
    def data(self, kind):
        return dict(format=kind, channel="angular_velocity", source="demo", sample_rate_hz=160,
                    window="hann", samples=16, start_time_unix_s=100, end_time_unix_s=100.09375,
                    frequency_hz=list(range(0, 81, 10)), amplitude=[[0, 0, 2.5, 0, 0, 0, 0, 0, 0]] * 3)

    def test_excel_and_csv_preserve_frequency_amplitude_and_settings(self):
        raw, _ = export(self.data("xlsx"))
        book = load_workbook(BytesIO(raw), read_only=True)
        rows = list(book["spectrum"].values)
        self.assertEqual(rows[3], (20, 2.5, 2.5, 2.5))
        self.assertEqual(dict(list(book["metadata"].values)[1:])["unit"], "rad/s")
        raw, _ = export(self.data("csv"))
        rows = list(csv.DictReader(StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(float(rows[2]["amplitude_x"]), 2.5)
        self.assertEqual(rows[2]["source"], "demo")
        self.assertEqual(rows[2]["sample_rate_hz"], "160")

    def test_mat_header_and_raw_values(self):
        raw, _ = export(self.data("mat"))
        self.assertEqual(raw[126:128], b"IM")
        self.assertIn(b"spectrum_columns", raw)
        self.assertIn(b"sample_rate_hz", raw)

    def test_rejects_invalid_bins_and_nonfinite_amplitudes(self):
        data = self.data("mat"); data["frequency_hz"][1] = 9
        with self.assertRaises(ValueError): export(data)
        data = self.data("csv"); data["amplitude"][0][2] = float("nan")
        with self.assertRaises(ValueError): export(data)


if __name__ == "__main__": unittest.main()
