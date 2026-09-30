#!/usr/bin/env python3
"""Verify and tessellate the manufacturer's STEP locally, outside version control.

No CAD import is needed during normal host operation. --install bootstraps an
isolated converter environment on first run; --cad-python reuses an existing one.
"""
import argparse
import array
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import urllib.request
import venv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Keep this tool usable before Flask/host dependencies have been installed.
BLOB = "b0ede7e62f2f40b8ad61680f821a4e8a1d280584"
SHA256 = "a3485deed48e3324e956bc7584dd42d9abf3d2cbbc5ef9e4d02dec3c99b81ddb"
SIZE = 39334534
FORMAT = "dmimu.mesh.v1"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def obtain_step(path):
    if path.is_file() and path.stat().st_size == SIZE and digest(path) == SHA256:
        return
    print("Downloading pinned official DM-IMU STEP (39 MB); local use only.", flush=True)
    url = f"https://gitee.com/api/v5/repos/kit-miao/dm-imu/git/blobs/{BLOB}"
    request = urllib.request.Request(url, headers={"User-Agent": "DM-IMU-Workbench-Model-Prepare/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read(55_000_001)
    if len(raw) > 55_000_000:
        raise ValueError("Official STEP response exceeds expected size")
    blob = json.loads(raw)
    content = base64.b64decode(blob["content"])
    if len(content) != SIZE or hashlib.sha256(content).hexdigest() != SHA256:
        raise ValueError("Official STEP hash/size mismatch; refusing to convert")
    atomic_write(path, content)


def convert(step, cache):
    from OCP.STEPControl import STEPControl_Reader
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS
    from OCP.TopLoc import TopLoc_Location
    from OCP.IFSelect import IFSelect_RetDone

    reader = STEPControl_Reader()
    if reader.ReadFile(str(step)) != IFSelect_RetDone or reader.TransferRoots() < 1:
        raise ValueError("STEP reader failed")
    shape = reader.OneShape()
    BRepMesh_IncrementalMesh(shape, .055, False, .28, True)
    # CAD: +Y top, -Z USB, X long edge. Device: +X USB, +Y long
    # edge (photo silkscreen), +Z top. Rotation has determinant +1.
    groups = {"housing": array.array("f"), "usb": array.array("f"), "light-guide": array.array("f")}
    face_count = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(face, location)
        if mesh is not None:
            transform = location.Transformation()
            for i in range(1, mesh.NbTriangles() + 1):
                indices = list(mesh.Triangle(i).Get())
                if face.Orientation() == TopAbs_REVERSED:
                    indices.reverse()
                points = [mesh.Node(n).Transformed(transform) for n in indices]
                # Appearance override only. Vertices always come from the STEP.
                # The complete USB receptacle sits at CAD -Z, near X=0.
                usb = all(abs(p.X()) < 5 and p.Z() < -10 and p.Y() < 0.1 for p in points)
                guide = all(abs(p.X()) <= 1.61 and abs(p.Z()) <= 1.61 and p.Y() > -.2 for p in points)
                group = groups["usb" if usb else "light-guide" if guide else "housing"]
                for p in points:
                    group.extend((-p.Z() / 10, -p.X() / 10, (p.Y() + 2.5) / 10))
                face_count += 1
        explorer.Next()
    if face_count < 1000:
        raise ValueError("STEP conversion unexpectedly incomplete")
    payload = bytearray()
    metadata = {"format": FORMAT, "source_sha256": SHA256, "source_blob": BLOB,
                "units": "10 mm", "dimensions_mm": [26, 36, 9],
                "cad_to_device": [[0, 0, -1], [-1, 0, 0], [0, 1, 0]],
                "origin": "housing center; not the undocumented sensitive element position",
                "surface_z": .45, "groups": []}
    appearance = {"housing": [0.075, .082, .091], "usb": [.52, .55, .58], "light-guide": [.25, .78, .46]}
    for name, positions in groups.items():
        if sys.byteorder != "little":
            positions.byteswap()
        metadata["groups"].append({"name": name, "rgb": appearance[name], "offset": len(payload), "count": len(positions) // 3})
        payload.extend(positions.tobytes())
    raw = json.dumps(metadata, ensure_ascii=True, separators=(",", ":")).encode()
    blob = struct.pack("<I", len(raw)) + raw + payload
    atomic_write(cache / "dm-imu-l1.mesh", blob)
    manifest = dict(metadata, bytes=len(blob), mesh_sha256=hashlib.sha256(blob).hexdigest(), triangles=face_count)
    atomic_write(cache / "dm-imu-l1.json", json.dumps(manifest, indent=2).encode())
    print(f"Official CAD prepared: {face_count:,} triangles, {len(blob)/1e6:.2f} MB. Device +X points toward USB.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install isolated OpenCascade conversion dependencies if needed")
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("DMIMU_MODEL_CACHE", ROOT / "downloads" / "models")))
    parser.add_argument("--step", type=Path, default=ROOT / "downloads" / "official" / "DM-IMU-L1-V1.0-3d.STEP")
    parser.add_argument("--cad-python", type=Path, help="Reuse an existing Python with cadquery-ocp installed")
    parser.add_argument("--force", action="store_true", help="Rebuild an existing cache")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.cache = args.cache.expanduser().resolve()
    args.step = args.step.expanduser().resolve()
    if not args.force:
        try:
            manifest = json.loads((args.cache / "dm-imu-l1.json").read_text())
            mesh = args.cache / "dm-imu-l1.mesh"
            if manifest["format"] == FORMAT and manifest["source_sha256"] == SHA256 and mesh.stat().st_size == manifest["bytes"] and digest(mesh) == manifest["mesh_sha256"]:
                print("Official CAD cache verified.")
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
    obtain_step(args.step)
    if args.worker:
        convert(args.step, args.cache)
        return
    interpreter = args.cad_python
    if interpreter is None:
        environment = args.cache / "converter-env"
        interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not interpreter.is_file():
            if not args.install:
                raise RuntimeError("Run with --install to prepare the isolated CAD converter, or supply --cad-python")
            print("Preparing isolated OpenCascade converter (first run only).", flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        check = subprocess.run([str(interpreter), "-c", "import OCP"], capture_output=True)
        if check.returncode:
            if not args.install:
                raise RuntimeError("CAD converter not installed; run with --install")
            subprocess.run([str(interpreter), "-m", "pip", "install", "cadquery-ocp==7.9.3.1.1"], check=True)
    subprocess.run([str(interpreter), str(Path(__file__).resolve()), "--worker", "--force", "--step", str(args.step), "--cache", str(args.cache)], check=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Model preparation failed: {error}", file=sys.stderr)
        sys.exit(1)
