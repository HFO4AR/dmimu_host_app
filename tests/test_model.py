"""Asset cache handling independent of optional CAD/native dependencies."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from dmimu.model import MODEL_FORMAT, MODEL_SHA256, create_model_blueprint, model_manifest


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = Path(self.directory.name)
        self.environment = patch.dict("os.environ", {"DMIMU_MODEL_CACHE": str(self.cache)})
        self.environment.start()
        app = Flask(__name__)
        app.register_blueprint(create_model_blueprint())
        self.client = app.test_client()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def asset(self):
        payload = b"unit test mesh"
        (self.cache / "dm-imu-l1.mesh").write_bytes(payload)
        (self.cache / "dm-imu-l1.json").write_text(json.dumps({
            "format": MODEL_FORMAT, "source_sha256": MODEL_SHA256,
            "bytes": len(payload), "mesh_sha256": hashlib.sha256(payload).hexdigest(),
        }))

    def test_missing_asset_has_actionable_state(self):
        response = self.client.get("/api/model")
        self.assertFalse(response.json["data"]["ready"])
        self.assertIn("prepare_model.py", response.json["data"]["prepare"])
        self.assertEqual(self.client.get("/api/model/mesh").status_code, 404)

    def test_valid_asset_served(self):
        self.asset()
        self.assertTrue(self.client.get("/api/model").json["data"]["ready"])
        response = self.client.get("/api/model/mesh")
        self.assertEqual(response.data, b"unit test mesh")
        response.close()

    def test_same_size_corruption_rejected(self):
        self.asset()
        (self.cache / "dm-imu-l1.mesh").write_bytes(b"corrupt mesh!!")
        self.assertIsNone(model_manifest())
        self.assertEqual(self.client.get("/api/model/mesh").status_code, 404)

    def test_nonobject_manifest_rejected(self):
        (self.cache / "dm-imu-l1.json").write_text("[]")
        self.assertIsNone(model_manifest())

    def test_unpinned_asset_rejected(self):
        self.asset()
        manifest = json.loads((self.cache / "dm-imu-l1.json").read_text())
        manifest["source_sha256"] = "unverified"
        (self.cache / "dm-imu-l1.json").write_text(json.dumps(manifest))
        self.assertIsNone(model_manifest())


if __name__ == "__main__":
    unittest.main()
