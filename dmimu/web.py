"""Browser session API and independently authenticated agent API."""
import argparse
import atexit
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import signal
import time
import threading
from io import BytesIO
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_file, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__
from .service import Fault, Service
from .storage import InstanceLock, Settings, atomic_json, default_directory
from .waveform_export import export as export_waveform
from .model import create_model_blueprint
from .trajectory_export import export as export_trajectory
from .spectrum_export import export as export_spectrum
from .firmware import MAX_PACKAGE_SIZE

ROOT = Path(__file__).resolve().parent.parent


def local_request():
    try:
        name = urlsplit("http://" + request.host).hostname
        return ipaddress.ip_address(request.remote_addr).is_loopback and (name == "localhost" or ipaddress.ip_address(name).is_loopback)
    except ValueError:
        return False


def create_app(settings, service):
    app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
    app.register_blueprint(create_model_blueprint())
    app.secret_key = settings.values["secret"]
    app.config.update(MAX_CONTENT_LENGTH=65536, SESSION_COOKIE_NAME="dmimu_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    attempts = {}
    import_lock = threading.Lock()

    @app.errorhandler(Fault)
    def fault(exc):
        return jsonify(ok=False, error={"code": exc.code, "message": str(exc)}), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(ok=False, error={"code": exc.name.upper().replace(" ", "_"), "message": exc.description}), exc.code

    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    @app.errorhandler(KeyError)
    def invalid(exc):
        return jsonify(ok=False, error={"code": "INVALID_REQUEST", "message": str(exc)}), 400

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise Fault("INVALID_REQUEST", "JSON 必须是对象")
        return data

    @app.before_request
    def authorize():
        if request.path in {"/api/v1/firmware/upload", "/api/agent/v1/firmware/upload"}:
            request.max_content_length = MAX_PACKAGE_SIZE
        if request.path in {"/api/v1/spectra/export", "/api/agent/v1/spectra/export"}:
            request.max_content_length = 4 * 1024 * 1024
        if request.path in {"/api/v1/recordings/import", "/api/agent/v1/recordings/import"}:
            request.max_content_length = 1024 * 1024 * 1024
        if request.path in {"/api/v1/waveforms/export", "/api/agent/v1/waveforms/export"}:
            request.max_content_length = 48 * 1024 * 1024
        if request.path in {"/api/v1/trajectories/export", "/api/agent/v1/trajectories/export"}:
            request.max_content_length = 8 * 1024 * 1024
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                raise Fault("ORIGIN_REJECTED", "不接受跨站操作", 403)
        if request.path.startswith("/api/agent/v1"):
            if not settings.values["agent_enabled"]:
                raise Fault("AGENT_DISABLED", "本机 Agent 接入已关闭", 403)
            if not local_request() and not request.is_secure:
                raise Fault("HTTPS_REQUIRED", "远程 Agent 只支持 HTTPS", 403)
            expected = "Bearer " + settings.values["agent_token"]
            if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
                raise Fault("UNAUTHORIZED", "Agent 凭据无效", 401)
            return
        if not request.path.startswith("/api/") or request.path in {"/api/session", "/api/login"}:
            return
        if not local_request() and not session.get("authenticated"):
            raise Fault("LOGIN_REQUIRED", "请先登录", 401)
        if request.method != "GET" and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "missing")):
            raise Fault("CSRF_REJECTED", "页面会话已过期，请刷新页面", 403)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/session")
    def auth_state():
        session.setdefault("csrf", secrets.token_urlsafe(24))
        return jsonify(ok=True, data={"authenticated": local_request() or bool(session.get("authenticated")), "local": local_request(), "csrf": session["csrf"], "version": __version__})

    @app.post("/api/login")
    def login():
        now = time.monotonic()
        key = request.remote_addr
        last = [t for t in attempts.get(key, []) if now - t < 60]
        attempts[key] = last
        if len(last) >= 10:
            raise Fault("RATE_LIMITED", "登录尝试过多，请一分钟后再试", 429)
        data = body()
        password = data.get("password")
        if not isinstance(password, str):
            raise Fault("INVALID_REQUEST", "密码必须是文本")
        if not settings.values["password_hash"] or not check_password_hash(settings.values["password_hash"], password):
            last.append(now)
            raise Fault("BAD_PASSWORD", "密码错误", 401)
        session.clear()
        session.update(authenticated=True, csrf=secrets.token_urlsafe(24))
        attempts.pop(key, None)
        return jsonify(ok=True, data={"csrf": session["csrf"]})

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    def result(data):
        return jsonify(ok=True, data=data)

    for prefix in ("/api/v1", "/api/agent/v1"):
        tag = "agent" if "agent" in prefix else "browser"
        app.add_url_rule(prefix + "/capabilities", tag + "_capabilities", lambda: result(service.capabilities()), methods=["GET"])
        app.add_url_rule(prefix + "/status", tag + "_status", lambda: result(service.snapshot()), methods=["GET"])
        def trajectory_data():
            try:
                after = int(request.args.get("after", "0"))
                epoch = int(request.args["epoch"]) if "epoch" in request.args else None
            except ValueError:
                raise Fault("INVALID_PARAMS", "轨迹索引必须是整数")
            return result(service.trajectory_data(after, epoch))
        app.add_url_rule(prefix + "/trajectory", tag + "_trajectory_data", trajectory_data, methods=["GET"])

        def current_trajectory_export(kind):
            content, mimetype = service.trajectory_export(kind)
            name = "dmimu-trajectory-" + time.strftime("%Y%m%d-%H%M%S") + "." + kind
            return send_file(BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=name)
        app.add_url_rule(prefix + "/trajectory/<kind>", tag + "_current_trajectory_export", current_trajectory_export, methods=["GET"])
        app.add_url_rule(prefix + "/ports", tag + "_ports", lambda: result(service.ports()), methods=["GET"])
        app.add_url_rule(prefix + "/recordings", tag + "_recordings", lambda: result(service.recordings.listing()), methods=["GET"])

        def firmware_upload():
            if request.mimetype != "application/octet-stream":
                raise Fault("INVALID_CONTENT_TYPE", "固件上传需要 application/octet-stream")
            raw = request.stream.read(MAX_PACKAGE_SIZE + 1)
            return result(service.store_firmware(raw, request.headers.get("X-Firmware-Name", "firmware.bin")))
        app.add_url_rule(prefix + "/firmware/upload", tag + "_firmware_upload", firmware_upload, methods=["POST"])
        app.add_url_rule(prefix + "/firmware", tag + "_firmware_list", lambda: result(service.firmware_listing()), methods=["GET"])
        app.add_url_rule(prefix + "/firmware/<identifier>", tag + "_firmware_inspect", lambda identifier: result(service.inspect_firmware(identifier)), methods=["GET"])

        def recording_import():
            if not import_lock.acquire(blocking=False):
                raise Fault("IMPORT_BUSY", "已有录制导入正在处理", 409)
            try:
                return result(service.recordings.import_stream(request.stream))
            finally:
                import_lock.release()
        app.add_url_rule(prefix + "/recordings/import", tag + "_recording_import", recording_import, methods=["POST"])

        def spectrum_export():
            data = body()
            content, mimetype = export_spectrum(data)
            name = "dmimu-spectrum-" + time.strftime("%Y%m%d-%H%M%S") + "." + data["format"]
            return send_file(BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=name)
        app.add_url_rule(prefix + "/spectra/export", tag + "_spectrum_export", spectrum_export, methods=["POST"])

        def samples():
            return result(service.samples_since(int(request.args.get("after", "0"))))
        app.add_url_rule(prefix + "/samples", tag + "_samples", samples, methods=["GET"])

        def logs():
            with service.lock:
                return result(list(service.logs))
        app.add_url_rule(prefix + "/logs", tag + "_logs", logs, methods=["GET"])

        def waveform_export():
            data = body()
            content, mimetype = export_waveform(data)
            name = "dmimu-waveform-" + time.strftime("%Y%m%d-%H%M%S") + "." + data["format"]
            return send_file(BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=name)
        app.add_url_rule(prefix + "/waveforms/export", tag + "_waveform_export", waveform_export, methods=["POST"])

        def trajectory_export():
            data = body()
            content, mimetype = export_trajectory(data)
            name = "dmimu-trajectory-" + time.strftime("%Y%m%d-%H%M%S") + "." + data["format"]
            return send_file(BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=name)
        app.add_url_rule(prefix + "/trajectories/export", tag + "_trajectory_export", trajectory_export, methods=["POST"])

        def analysis(identifier, kind="json"):
            import re
            import csv
            from io import StringIO
            if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                raise Fault("INVALID_ID", "无效分析 ID")
            path = settings.directory / "analyses" / (identifier + ".json")
            if not path.is_file():
                raise Fault("FILE_NOT_FOUND", "分析结果不存在", 404)
            data = json.loads(path.read_text(encoding="utf-8"))
            if kind == "json":
                return result(data)
            if kind != "csv":
                raise Fault("INVALID_FORMAT", "分析结果支持 json 或 csv")
            out = StringIO(newline="");writer = csv.writer(out)
            writer.writerow(("tau_s", "m", "pairs", "adev_x", "adev_y", "adev_z", "unit", "sample_rate_hz", "recording_id", "channel"))
            for point in data["points"]:
                writer.writerow((point["tau_s"], point["m"], point["pairs"], *(point["deviation"][axis] for axis in ("x", "y", "z")), data["unit"], data["sample_rate_hz"], data["recording_id"], data["channel"]))
            return send_file(BytesIO(b"\xef\xbb\xbf" + out.getvalue().encode()), mimetype="text/csv", as_attachment=True, download_name="dmimu-allan-" + identifier[:8] + ".csv")
        app.add_url_rule(prefix + "/analyses/<identifier>", tag + "_analysis", analysis, methods=["GET"])
        app.add_url_rule(prefix + "/analyses/<identifier>/<kind>", tag + "_analysis_download", analysis, methods=["GET"])

        def action():
            data = body()
            op = service.submit(data.get("action"), data.get("params", {}), request.headers.get("Idempotency-Key"))
            return jsonify(ok=True, operation=op), 202 if op["state"] in {"queued", "running"} else 200
        app.add_url_rule(prefix + "/actions", tag + "_action", action, methods=["POST"])

        def operation(identifier):
            return jsonify(ok=True, operation=service.operation(identifier))
        app.add_url_rule(prefix + "/operations/<identifier>", tag + "_operation", operation, methods=["GET"])

        def download(identifier, kind):
            if kind not in {"raw", "csv", "imulog"}:
                raise Fault("INVALID_FORMAT", "支持 raw、csv 和 imulog")
            if identifier == service.recordings.active:
                raise Fault("RECORDING_ACTIVE", "先停止录制，再下载")
            if kind == "imulog":
                # Validate synchronously before entering a streamed response.
                if not service.recordings.path(identifier).is_file():
                    raise Fault("FILE_NOT_FOUND", "录制文件不存在", 404)
                return Response(service.recordings.official_export(identifier), mimetype="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="' + identifier + '.imulog"'})
            path = service.recordings.path(identifier, "csv" if kind == "csv" else "dmimulog")
            if not path.is_file():
                raise Fault("FILE_NOT_FOUND", "文件不存在；CSV 需要先导出", 404)
            return send_file(path, as_attachment=True)
        app.add_url_rule(prefix + "/recordings/<identifier>/<kind>", tag + "_download", download, methods=["GET"])

        def probe_download(identifier):
            import re
            if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                raise Fault("INVALID_ID", "无效探测 ID")
            path = settings.directory / "protocol-probes" / (identifier + ".json")
            if not path.is_file():
                raise Fault("FILE_NOT_FOUND", "探测记录不存在", 404)
            return send_file(path, as_attachment=True)
        app.add_url_rule(prefix + "/protocol-probes/<identifier>", tag + "_probe_download", probe_download, methods=["GET"])

    @app.get("/api/v1/events")
    def events():
        def generate():
            while not service.stop_event.is_set():
                yield "data: " + json.dumps(service.snapshot(), ensure_ascii=False, allow_nan=False) + "\n\n"
                service.stop_event.wait(.1)
        return Response(generate(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/v1/settings")
    def get_settings():
        return result(settings.public() | {"restart_required": app.config.get("RESTART_REQUIRED", False)})

    @app.post("/api/v1/settings")
    def update_settings():
        if not local_request():
            raise Fault("LOCAL_ONLY", "接入、协议和服务器设置只允许本机页面修改", 403)
        if service.recordings.writer or service.jobs.unfinished_tasks or service.calibration_poll:
            raise Fault("BUSY", "先停止录制并等待操作完成，再修改上位机设置", 409)
        p = body()
        allowed = {"lan_enabled", "password", "agent_enabled", "regenerate_agent_token", "auto_connect", "baudrate", "protocol", "legacy_crc"}
        if set(p) - allowed:
            raise Fault("INVALID_PARAMETER", "包含未知设置")
        values = dict(settings.values)
        for key in ("lan_enabled", "agent_enabled", "auto_connect", "legacy_crc", "regenerate_agent_token"):
            if key in p and type(p[key]) is not bool:
                raise ValueError(key + " 必须是布尔值")
        if "password" in p:
            if not isinstance(p["password"], str) or not p["password"]:
                raise ValueError("局域网密码不能为空")
            values["password_hash"] = generate_password_hash(p["password"], method="scrypt")
            # Revoke browser sessions issued under the previous LAN password.
            values["secret"] = secrets.token_urlsafe(32)
        if "protocol" in p and p["protocol"] not in {"auto", "legacy-v1"}:
            raise ValueError("协议必须是 auto 或 legacy-v1")
        if "baudrate" in p and (type(p["baudrate"]) is not int or p["baudrate"] not in {115200, 460800, 921600, 1000000, 2000000}):
            raise ValueError("不支持的波特率")
        values.update({k: v for k, v in p.items() if k not in {"password", "regenerate_agent_token"}})
        if values["lan_enabled"] and not values["password_hash"]:
            raise ValueError("先设置密码，再启用局域网")
        if p.get("regenerate_agent_token"):
            values["agent_token"] = secrets.token_urlsafe(32)
        restart = any(settings.values[k] != values[k] for k in ("lan_enabled", "baudrate", "legacy_crc"))
        with service.lock:
            settings.values = values
            settings.save()
            app.secret_key = values["secret"]
            service.auto_connect = values["auto_connect"]
            app.config["RESTART_REQUIRED"] = app.config.get("RESTART_REQUIRED", False) or restart
            publish_access(settings, app.config.get("LOCAL_URL", "http://127.0.0.1:5050"))
        return result(settings.public() | {"restart_required": app.config["RESTART_REQUIRED"]})

    return app


def publish_access(settings, url):
    root = settings.directory
    token_path = root / "agent-token"
    # Atomic token update with the same private permissions as config files.
    atomic_json(token_path, {"token": settings.values["agent_token"]})
    atomic_json(root / "connection.json", {"version": 1, "url": url, "pid": os.getpid(), "token_file": str(token_path), "agent_enabled": settings.values["agent_enabled"]})


def main():
    parser = argparse.ArgumentParser(description="达妙 IMU 工作台")
    parser.add_argument("--host", help="默认本机；启用局域网后默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--data-dir", type=Path, default=default_directory())
    parser.add_argument("--dev", action="store_true", help="本机开发服务器，不启动第二个串口实例")
    parser.add_argument("--demo", action="store_true", help="显式启动演示数据")
    parser.add_argument("--no-auto-connect", action="store_true")
    parser.add_argument("--trusted-proxy", help="仅在 HTTPS 反向代理部署时指定可信代理 IP")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口须为 1–65535")
    settings = Settings(args.data_dir)
    host = "127.0.0.1" if args.dev else args.host or ("0.0.0.0" if settings.values["lan_enabled"] else "127.0.0.1")
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        parser.error("--host 只能使用 IP 地址或 localhost")
    if not local and not settings.values["password_hash"]:
        parser.error("局域网监听前请在本机网页设置密码并启用局域网")
    instance = InstanceLock(settings.directory)
    service = Service(settings)
    if args.no_auto_connect or args.demo:
        service.auto_connect = False
    if args.demo:
        service.source, service.connection = "demo", "demo"
    app = create_app(settings, service)
    url = f"http://127.0.0.1:{args.port}"
    app.config["LOCAL_URL"] = url
    # Probe availability before publishing discovery or starting the device reader.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family) as probe:
            # A recently stopped instance may leave accepted sockets in TIME_WAIT.
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, args.port))
    except OSError as exc:
        instance.close()
        parser.error(f"端口 {args.port} 不可用：{exc}")
    publish_access(settings, url)
    service.start()
    def stop_signal(signum, frame):
        service.stop_event.set()
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    def cleanup():
        service.close()
        discovery = settings.directory / "connection.json"
        try:
            if json.loads(discovery.read_text())["pid"] == os.getpid():
                discovery.unlink()
        except (OSError, ValueError, KeyError):
            pass
        instance.close()
    atexit.register(cleanup)
    print(f"DM IMU Workbench {__version__} · {url}", flush=True)
    print("Type-C 接入后自动扫描；Agent: python agent_cli.py capabilities", flush=True)
    if not local:
        print(f"局域网监听 {host}:{args.port}，其他客户端需要登录", flush=True)
    try:
        if args.dev:
            app.run(host=host, port=args.port, debug=False, use_reloader=False, threaded=True)
        else:
            from waitress import serve
            options = {"trusted_proxy": args.trusted_proxy, "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"}} if args.trusted_proxy else {}
            serve(app, host=host, port=args.port, threads=12, **options)
    except KeyboardInterrupt:
        pass
