"""Standard-library-only agent client; stdout is exactly one JSON document."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid
import subprocess

from dmimu.storage import default_directory, windows_powershell

ROOT = Path(__file__).resolve().parent


def private_json(path):
    if path.is_symlink():
        raise ValueError("凭据文件不能是符号链接")
    info = path.stat()
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("凭据文件必须属于当前用户且仅本人可读写")
    if os.name == "nt":
        quoted = str(path).replace("'", "''")
        script = f"$p='{quoted}'; $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; $a=Get-Acl -LiteralPath $p; "
        script += "if($a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){throw 'Wrong owner'}; foreach($r in $a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])){if($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Value -ne $sid.Value){throw 'Shared access'}}"
        windows_powershell(script)
    if info.st_size > 16384:
        raise ValueError("接入文件过大")
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="达妙 IMU Agent CLI；全局选项放在子命令前")
    p.add_argument("--host-url")
    p.add_argument("--token-file", type=Path)
    p.add_argument("--data-dir", type=Path, default=default_directory())
    p.add_argument("--timeout", type=float, default=30)
    sub = p.add_subparsers(dest="command", required=True)
    for cmd in ("capabilities", "status", "ports", "recordings", "telemetry", "logs"):
        sub.add_parser(cmd)
    op = sub.add_parser("operation")
    op.add_argument("id")
    action = sub.add_parser("action")
    action.add_argument("name", help="例如 connect、record.start、device.configure")
    action.add_argument("--params", default="{}", help="JSON 对象")
    action.add_argument("--params-file", type=Path)
    action.add_argument("--idempotency-key")
    action.add_argument("--no-wait", action="store_true")
    download = sub.add_parser("download")
    download.add_argument("id")
    download.add_argument("--format", choices=("raw", "csv"), default="raw")
    download.add_argument("--output", type=Path, required=True)
    probe = sub.add_parser("probe-download")
    probe.add_argument("id")
    probe.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    output = None
    exit_code = 0
    try:
        if not 0 < args.timeout <= 3600:
            raise ValueError("timeout 必须为 0–3600 秒")
        discovery = private_json(args.data_dir / "connection.json") if not args.host_url else None
        url = (args.host_url or discovery["url"]).rstrip("/")
        parts = urlsplit(url)
        try:
            local = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            local = False
        if parts.scheme not in {"http", "https"} or parts.path or parts.query or parts.fragment or parts.username or (not local and parts.scheme != "https"):
            raise ValueError("地址必须是本机 HTTP(S) 或远程 HTTPS 服务根地址")
        token_file = args.token_file or (Path(discovery["token_file"]) if discovery else None)
        if token_file is None:
            raise ValueError("指定 --host-url 时也需要 --token-file")
        token = private_json(token_file)["token"]
        prefix = url + "/api/agent/v1"

        def call(path, data=None, key=None):
            headers = {"Authorization": "Bearer " + token}
            if data is not None:
                headers["Content-Type"] = "application/json"
            if key:
                headers["Idempotency-Key"] = key
            req = Request(prefix + path, data=json.dumps(data, allow_nan=False).encode() if data is not None else None, headers=headers)
            with urlopen(req, timeout=min(args.timeout, 30)) as response:
                return json.load(response)

        if args.command == "action":
            params = json.loads(args.params_file.read_text(encoding="utf-8") if args.params_file else args.params)
            key = args.idempotency_key or uuid.uuid4().hex
            # Preserve recovery information even if submission transport fails.
            output = {"ok": False, "idempotency_key": key}
            output = call("/actions", {"action": args.name, "params": params}, key)
            output["idempotency_key"] = key
            deadline = time.monotonic() + args.timeout
            while not args.no_wait and output["operation"]["state"] in {"queued", "running"}:
                if time.monotonic() >= deadline:
                    output.update(ok=False, error={"code": "WAIT_TIMEOUT", "message": "等待超时，使用 operation 查询原操作"})
                    break
                time.sleep(.15)
                output = call("/operations/" + output["operation"]["id"])
                output["idempotency_key"] = key
            state = output["operation"]["state"]
            if state in {"failed", "uncertain"}:
                output["ok"] = False
                output["error"] = output["operation"].get("error", {"code": "RESULT_UNCERTAIN", "message": "设备结果未知，请查询原操作，不要换键重试"})
        elif args.command == "operation":
            output = call("/operations/" + args.id)
        elif args.command in {"download", "probe-download"}:
            import re
            if not re.fullmatch(r"[0-9a-f]{32}", args.id):
                raise ValueError("无效制品 ID")
            path = f"/recordings/{args.id}/{args.format}" if args.command == "download" else f"/protocol-probes/{args.id}"
            req = Request(prefix + path, headers={"Authorization": "Bearer " + token})
            temporary = args.output.with_name(args.output.name + ".part")
            if args.output.exists() or temporary.exists():
                raise ValueError("输出文件已存在，使用新的路径")
            try:
                with urlopen(req, timeout=args.timeout) as response, temporary.open("xb") as dst:
                    while chunk := response.read(65536):
                        dst.write(chunk)
                temporary.rename(args.output)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            output = {"ok": True, "data": {"path": str(args.output.resolve()), "bytes": args.output.stat().st_size}}
        else:
            output = call("/" + ("status" if args.command == "telemetry" else args.command))
        exit_code = 0 if output.get("ok") else 2
    except HTTPError as exc:
        previous = output or {}
        try:
            output = json.load(exc)
        except ValueError:
            output = {"ok": False, "error": {"code": "HTTP_ERROR", "message": str(exc.code)}}
        if "idempotency_key" in previous:
            output["idempotency_key"] = previous["idempotency_key"]
        exit_code = 2
    except (OSError, URLError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        output = (output or {}) | {"ok": False, "error": {"code": "TOKEN_UNAVAILABLE" if isinstance(exc, FileNotFoundError) and args.command != "download" else "CLIENT_ERROR", "message": str(exc)}}
        exit_code = 2
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
