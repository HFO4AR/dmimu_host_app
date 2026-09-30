"""Private runtime storage and atomic configuration writes."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile


def default_directory():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "DM-IMU-Workbench"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "dmimu-workbench"


def private_directory(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        # Windows chmod does not restrict other users. Remove inherited grants.
        quoted = str(path).replace("'", "''")
        script = f"$p='{quoted}'; $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
        script += "$a=Get-Acl -LiteralPath $p; $a.SetAccessRuleProtection($true,$false); foreach($r in @($a.Access)){$a.RemoveAccessRuleSpecific($r)}; $a.SetOwner($sid); $a.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')); Set-Acl -LiteralPath $p -AclObject $a"
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop';" + script], check=True, capture_output=True)
    else:
        path.chmod(0o700)
        if path.stat().st_mode & 0o077:
            raise RuntimeError("数据目录所在文件系统不能保存私有权限；请选择 Linux 本机目录，例如 ~/.local/state/dmimu-workbench")
    return path


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise RuntimeError("配置文件无法保存私有权限，请更换数据目录")
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Settings:
    def __init__(self, directory):
        self.directory = private_directory(directory)
        self.path = self.directory / "settings.json"
        self.values = {"lan_enabled": False, "password_hash": "", "secret": secrets.token_hex(32), "agent_enabled": True, "agent_token": secrets.token_urlsafe(32), "auto_connect": True, "preferred_device": None, "baudrate": 921600, "protocol": "auto", "legacy_crc": False}
        if self.path.exists():
            self.values.update(json.loads(self.path.read_text(encoding="utf-8")))
        self.save()

    def save(self):
        atomic_json(self.path, self.values)

    def public(self):
        return {k: v for k, v in self.values.items() if k not in {"secret", "password_hash", "agent_token"}} | {"password_set": bool(self.values["password_hash"])}


class InstanceLock:
    def __init__(self, directory):
        self.file = open(Path(directory) / "instance.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("此数据目录已有上位机实例运行，请使用该实例或另选 --data-dir") from None

    def close(self):
        self.file.close()
