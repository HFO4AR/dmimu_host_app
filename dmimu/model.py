"""Local, reproducible official CAD cache; source assets are never bundled."""
import json
import hashlib
import os
from pathlib import Path

from flask import Blueprint, jsonify, send_file

MODEL_BLOB = "b0ede7e62f2f40b8ad61680f821a4e8a1d280584"
MODEL_SHA256 = "a3485deed48e3324e956bc7584dd42d9abf3d2cbbc5ef9e4d02dec3c99b81ddb"
MODEL_FORMAT = "dmimu.mesh.v1"
ROOT = Path(__file__).resolve().parent.parent


def model_cache():
    return Path(os.environ.get("DMIMU_MODEL_CACHE", ROOT / "downloads" / "models")).expanduser().resolve()


def model_manifest():
    cache = model_cache()
    try:
        manifest = json.loads((cache / "dm-imu-l1.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return None
        mesh = cache / "dm-imu-l1.mesh"
        if (manifest.get("format") != MODEL_FORMAT or manifest.get("source_sha256") != MODEL_SHA256
                or not mesh.is_file() or mesh.stat().st_size != manifest.get("bytes")):
            return None
        with mesh.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != manifest.get("mesh_sha256"):
                return None
        return manifest
    except (OSError, ValueError, TypeError):
        return None


def create_model_blueprint():
    bp = Blueprint("official_model", __name__)

    @bp.get("/api/model")
    def description():
        manifest = model_manifest()
        return jsonify(ok=True, data={
            "ready": manifest is not None,
            "name": "DM-IMU-L1-V1.0",
            "source": "https://gitee.com/kit-miao/dm-imu",
            "source_blob": MODEL_BLOB,
            "prepare": "python scripts/prepare_model.py --install",
            "mesh_url": "/api/model/mesh" if manifest else None,
            "metadata": manifest,
        })

    @bp.get("/api/model/mesh")
    def mesh_file():
        if model_manifest() is None:
            return jsonify(ok=False, error={"code": "MODEL_NOT_READY", "message": "官方 CAD 模型尚未准备"}), 404
        return send_file(model_cache() / "dm-imu-l1.mesh", mimetype="application/octet-stream", conditional=True)

    return bp
