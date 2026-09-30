"""CLI HTTP smoke tests use a private, synthetic-only service on a random port."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from werkzeug.serving import make_server
from dmimu.protocol import encode_frame
from dmimu.service import Service
from dmimu.storage import Settings, atomic_json
from dmimu.web import create_app

ROOT = Path(__file__).resolve().parent.parent


class AgentCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.settings = Settings(self.directory)
        self.service = Service(self.settings, port_provider=lambda: [])
        self.server = make_server("127.0.0.1", 0, create_app(self.settings, self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        token = self.directory / "agent.json"
        atomic_json(token, dict(token=self.settings.values["agent_token"]))
        atomic_json(self.directory / "connection.json", dict(url=f"http://127.0.0.1:{self.server.server_port}", token_file=str(token)))

    def tearDown(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.service.close(); self.temp.cleanup()

    def cli(self, *args):
        completed = subprocess.run([sys.executable, "-S", str(ROOT / "agent_cli.py"), "--data-dir", str(self.directory), *args], capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_private_discovery_streaming_import_and_native_download(self):
        self.assertTrue(self.cli("status")["ok"])
        header = dict(format="dmimu.raw", version=1, created_at=100, source="demo", port=None)
        item = dict(time=100.1, elapsed=.1, data=base64.b64encode(encode_frame(2, [1, 2, 3])).decode())
        source = self.directory / "input.dmimulog"
        source.write_text(json.dumps(header) + "\n" + json.dumps(item) + "\n")
        result = self.cli("recording-import", "--input", str(source))
        destination = self.directory / "official.imulog"
        self.cli("download", result["data"]["id"], "--format", "imulog", "--output", str(destination))
        self.assertTrue(destination.read_bytes().startswith(b"IMULOG01"))
        self.assertEqual(self.service.serial, None)

    def test_export_command_writes_mat_file_with_no_site_packages(self):
        source = self.directory / "waveform.json"
        source.write_text(json.dumps(dict(source="demo", channels={"angular_velocity": [[100, 1, 2, 3]]})))
        destination = self.directory / "waveform.mat"
        result = self.cli("waveform-export", "--input", str(source), "--format", "mat", "--output", str(destination))
        self.assertTrue(result["ok"])
        self.assertEqual(destination.read_bytes()[126:128], b"IM")

    def test_downloads_current_shared_trajectory(self):
        self.service.source = "demo"
        self.service.trajectory.set_source("demo", self.service.generation)
        self.service.trajectory.points.append({"time": 100., "host_time": 100., "elapsed": 0., "segment": 0,
            "position": [0., 0., 0.], "velocity": [0., 0., 0.], "acceleration": [0., 0., 0.],
            "distance": 0., "stationary": True, "orientation_kind": "quaternion", "quaternion": [1., 0., 0., 0.]})
        destination = self.directory / "trajectory.mat"
        result = self.cli("trajectory-download", "--format", "mat", "--output", str(destination))
        self.assertTrue(result["ok"])
        self.assertEqual(destination.read_bytes()[126:128], b"IM")


if __name__ == "__main__": unittest.main()
