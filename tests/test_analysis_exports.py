"""Exercise browser/Agent export contracts with synthetic data only."""
import csv
from io import BytesIO, StringIO
import tempfile
import unittest

from openpyxl import load_workbook
from dmimu.service import Service
from dmimu.storage import Settings, atomic_json
from dmimu.trajectory_export import export as trajectory_export
from dmimu.web import create_app


class AnalysisExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(self.temp.name)
        self.service = Service(self.settings, port_provider=lambda: [])
        self.client = create_app(self.settings, self.service).test_client()
        self.headers = {"Authorization": "Bearer " + self.settings.values["agent_token"]}

    def tearDown(self):
        self.service.close(); self.temp.cleanup()

    def trajectory(self, kind):
        return dict(format=kind, estimate=True, source="demo", generation=7, timing="recorded_time",
                    gravity_reference=[0, 0, 9.80665], points=[dict(time=100, host_time=200, elapsed=0,
                    segment=1, position=[.5, 0, 0], velocity=[1, 0, 0], acceleration=[1, 0, 0], distance=.5,
                    stationary=False, orientation_kind="quaternion", quaternion=[1, 0, 0, 0])])

    def test_trajectory_files_preserve_estimate_metadata_and_units(self):
        raw, _ = trajectory_export(self.trajectory("xlsx"))
        book = load_workbook(BytesIO(raw), read_only=True)
        rows = list(book["trajectory"].values)
        self.assertEqual(rows[0][4], "position_x_m")
        self.assertEqual(rows[1][4], .5)
        self.assertEqual(rows[1][1:3], (100, 200))
        info = dict(list(book["metadata"].values)[1:])
        self.assertEqual(info["timing"], "recorded_time")
        self.assertIn("estimate", info["estimate"])
        raw, _ = trajectory_export(self.trajectory("csv"))
        row = next(csv.DictReader(StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(row["estimate"], "True")
        self.assertEqual(row["gravity_reference_z"], "9.80665")
        with self.assertRaises(ValueError):
            trajectory_export(self.trajectory("mat") | {"estimate": False})

    def test_export_endpoints_require_separate_auth_and_browser_csrf(self):
        payloads = {
            "waveforms": dict(format="csv", source="demo", channels={"angular_velocity": [[100, 1, 2, 3]]}),
            "trajectories": self.trajectory("csv"),
            "spectra": dict(format="csv", source="demo", channel="angular_velocity", samples=16,
                            sample_rate_hz=160, window="hann", start_time_unix_s=100, end_time_unix_s=100.1,
                            frequency_hz=list(range(0, 81, 10)), amplitude=[[0] * 9 for _ in range(3)]),
        }
        for name, data in payloads.items():
            with self.subTest(name=name):
                path = "/api/agent/v1/" + name + "/export"
                self.assertEqual(self.client.post(path, json=data).status_code, 401)
                response = self.client.post(path, json=data, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertEqual(self.client.post("/api/v1/" + name + "/export", json=data).status_code, 403)
                csrf = self.client.get("/api/session").json["data"]["csrf"]
                response = self.client.post("/api/v1/" + name + "/export", json=data, headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 200)

    def test_allan_download_is_private_and_retains_points(self):
        directory = self.settings.directory / "analyses"; directory.mkdir(exist_ok=True)
        identifier = "a" * 32
        data = dict(recording_id="b" * 32, channel="angular_velocity", unit="rad/s", sample_rate_hz=100,
                    points=[dict(tau_s=.01, m=1, pairs=99, deviation=dict(x=.1, y=.2, z=.3))])
        atomic_json(directory / (identifier + ".json"), data)
        self.assertEqual(self.client.get("/api/agent/v1/analyses/" + identifier).status_code, 401)
        response = self.client.get("/api/agent/v1/analyses/" + identifier + "/csv", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        row = next(csv.DictReader(StringIO(response.data.decode("utf-8-sig"))))
        self.assertEqual(row["adev_x"], "0.1")
        self.assertEqual(row["pairs"], "99")
        self.assertEqual(self.client.get("/api/agent/v1/analyses/invalid", headers=self.headers).status_code, 400)


if __name__ == "__main__": unittest.main()
