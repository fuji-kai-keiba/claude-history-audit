"""Explicit source registry and bounded, read-only SSH snapshot collection."""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath

from .audit import discover

DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST = 8 * 1024 * 1024


def state_home():
    return Path.home() / ".claude-history-audit"


def default_config():
    return state_home() / "sources.json"


def local_source():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser() / "projects"


def validate_config(config):
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("取得先設定のversionは1が必要です。")
    if not isinstance(config.get("include_local", True), bool) or not isinstance(config.get("sources"), list):
        raise ValueError("取得先設定の形式が不正です。")
    seen = set()
    for source in config["sources"]:
        if not isinstance(source, dict):
            raise ValueError("取得先の形式が不正です。")
        name = source.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) or name in seen:
            raise ValueError("取得先名には重複しない英数字・ハイフン・下線を使ってください。")
        seen.add(name)
        if source.get("kind") not in ("ssh", "path"):
            raise ValueError("取得先kindはsshまたはpathが必要です。")
        path = source.get("path")
        if path is not None and (not isinstance(path, str) or not path or len(path) > 4096 or any(ord(c) < 32 for c in path)):
            raise ValueError("取得先pathが不正です。")
        if source["kind"] == "path" and not path:
            raise ValueError("共有フォルダのpathが必要です。")
        if source["kind"] == "ssh":
            host = source.get("host")
            if not isinstance(host, str) or not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*", host):
                raise ValueError("SSH先はHost名またはuser@hostnameで指定してください。ポート等はSSH設定を使います。")
        for key, default, low, high in [("timeout_seconds", 180, 1, 3600), ("max_bytes", DEFAULT_MAX_BYTES, 1, 20 * DEFAULT_MAX_BYTES)]:
            value = source.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("取得先の制限値が不正です: " + key)
    return config


def read_config(path):
    path = Path(path).expanduser()
    if not path.exists():
        raise ValueError("取得先設定が見つかりません。sources add-ssh または sources add-path で登録してください。")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("取得先設定が大きすぎます。")
    return validate_config(json.loads(path.read_text(encoding="utf-8")))


def save_config(path, config):
    validate_config(config)
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("設定ファイルのシンボリックリンクには書き込みません。")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".sources-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sources_main(argv):
    parser = argparse.ArgumentParser(description="監査時に自動取得する接続先を登録。登録だけでは接続しません。")
    parser.add_argument("--config", type=Path, default=default_config())
    commands = parser.add_subparsers(dest="action", required=True)
    ssh = commands.add_parser("add-ssh")
    ssh.add_argument("name")
    ssh.add_argument("host")
    ssh.add_argument("--path", help="相手側の履歴フォルダ。省略時は相手側CLAUDE_CONFIG_DIR/projectsまたは~/.claude/projects")
    folder = commands.add_parser("add-path")
    folder.add_argument("name")
    folder.add_argument("path")
    remove = commands.add_parser("remove")
    remove.add_argument("name")
    commands.add_parser("list")
    local = commands.add_parser("local")
    local.add_argument("mode", choices=["on", "off"])
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config) if args.config.exists() else {"version": 1, "include_local": True, "sources": []}
        if args.action.startswith("add-"):
            item = {"name": args.name, "kind": "ssh" if args.action == "add-ssh" else "path", "path": args.path}
            if args.action == "add-ssh":
                item["host"] = args.host
            else:
                item["path"] = str(Path(args.path).expanduser().absolute())
            if any(s["name"] == args.name for s in config["sources"]):
                raise ValueError("同名の取得先が登録済みです。変更する場合はremove後に登録してください。")
            config["sources"].append(item)
        elif args.action == "remove":
            if not any(s["name"] == args.name for s in config["sources"]):
                raise ValueError("指定した取得先は登録されていません。")
            config["sources"] = [s for s in config["sources"] if s["name"] != args.name]
        elif args.action == "local":
            config["include_local"] = args.mode == "on"
        else:
            print(json.dumps(config, ensure_ascii=False, indent=2))
            return 0
        save_config(args.config, config)
        print("取得先設定を保存しました。次の監査から自動取得します: " + str(args.config))
        return 0
    except (ValueError, OSError) as error:
        print("設定できません: " + str(error), file=sys.stderr)
        return 2


def ssh_command(host):
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", host, "python3", "-"]


def fetch_archive(source, archive_path):
    options = {"path": source.get("path"), "max_bytes": source.get("max_bytes", DEFAULT_MAX_BYTES)}
    script = ("OPTIONS = " + repr(options) + "\n" + Path(__file__).with_name("_export.py").read_text(encoding="utf-8")).encode()
    timed_out = threading.Event()
    try:
        process = subprocess.Popen(ssh_command(source["host"]), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise ValueError("ssh_unavailable") from None
    def stop():
        timed_out.set()
        try:
            process.kill()
        except ProcessLookupError:
            pass
    timer = threading.Timer(source.get("timeout_seconds", 180), stop)
    timer.daemon = True
    timer.start()
    try:
        process.stdin.write(script)
        process.stdin.close()
        total = 0
        fd = os.open(str(archive_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > options["max_bytes"] + MAX_MANIFEST + 32 * 1024 * 1024:
                    raise ValueError("transfer_limit")
                output.write(chunk)
        code = process.wait()
        if timed_out.is_set():
            raise ValueError("connection_timeout")
        if code:
            raise ValueError("ssh_or_export_failed")
    except BrokenPipeError:
        raise ValueError("ssh_or_export_failed") from None
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def extract_archive(archive_path, destination, maximum):
    """Validate the complete envelope before creating any extracted files."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(infos) > 50001 or len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("invalid_archive")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST:
                raise ValueError("manifest_limit")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
                raise ValueError("invalid_manifest")
            if not isinstance(manifest.get("root"), str) or type(manifest.get("skipped_symlinks")) is not int or manifest["skipped_symlinks"] < 0:
                raise ValueError("invalid_manifest")
            expected = {}
            for item in manifest["files"]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or type(item.get("size")) is not int or item["size"] < 0:
                    raise ValueError("invalid_manifest")
                name = item["name"]
                parts = PurePosixPath(name).parts
                if (len(parts) < 2 or parts[0] != "files" or ".." in parts or "\\" in name or ":" in name
                        or any(ord(c) < 32 for c in name) or name != "/".join(parts)
                        or not name.endswith(".jsonl") or parts[-1] == "history.jsonl" or name in expected):
                    raise ValueError("invalid_archive_path")
                expected[name] = item["size"]
            if set(names) != set(expected) | {"manifest.json"} or sum(expected.values()) > maximum:
                raise ValueError("archive_limit_or_members")
            for info in infos:
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError("archive_nonregular")
                if info.filename != "manifest.json" and info.file_size != expected[info.filename]:
                    raise ValueError("archive_size_mismatch")
            for name, size in expected.items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output, archive.open(name) as incoming:
                    copied = 0
                    while True:
                        chunk = incoming.read(65536)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > size:
                            raise ValueError("archive_size_mismatch")
                        output.write(chunk)
                    if copied != size:
                        raise ValueError("archive_size_mismatch")
            return manifest
    except (zipfile.BadZipFile, KeyError, TypeError, UnicodeError, RuntimeError):
        raise ValueError("invalid_archive") from None


def collect(config, snapshot):
    """Fresh per-run snapshots only. Never silently reuse an earlier success."""
    validate_config(config)
    snapshot.mkdir(parents=True, exist_ok=False, mode=0o700)
    requested = ([{"kind": "path", "name": "this-device", "path": str(local_source())}] if config.get("include_local", True) else []) + config["sources"]
    sources, results, private = [], [], {}
    for index, source in enumerate(requested, 1):
        sid = f"source-{index:03d}"
        result = {"id": sid, "kind": source["kind"], "status": "failed", "files": 0, "skipped_symlinks": 0, "error": None}
        detail = dict(source)
        try:
            if source["kind"] == "ssh":
                folder = snapshot / sid
                folder.mkdir(mode=0o700)
                archive = folder / "snapshot.zip"
                fetch_archive(source, archive)
                manifest = extract_archive(archive, folder, source.get("max_bytes", DEFAULT_MAX_BYTES))
                archive.unlink()
                path = folder / "files"
                detail["remote_root"] = manifest["root"]
                detail["snapshot"] = str(path)
                result["files"] = len(manifest["files"])
                result["skipped_symlinks"] = manifest["skipped_symlinks"]
            else:
                path = Path(source["path"]).expanduser().absolute()
                files, skipped, errors = discover([path])
                if errors:
                    raise ValueError("source_unavailable")
                result["files"] = len(files)
                result["skipped_symlinks"] = skipped
            if result["skipped_symlinks"]:
                result["status"], result["error"] = "partial", "symlinks_skipped"
            else:
                result["status"] = "ok" if result["files"] else "empty"
            if result["files"]:
                sources.append(path)
        except (OSError, ValueError) as error:
            allowed = {"ssh_unavailable", "connection_timeout", "ssh_or_export_failed", "transfer_limit", "source_unavailable"}
            result["error"] = str(error) if str(error) in allowed else "collection_failed"
            folder = snapshot / sid
            if folder.exists():
                shutil.rmtree(folder)
        results.append(result)
        private[sid] = detail
    status = {"mode": "registered", "requested": len(results),
              "successful": sum(r["status"] in ("ok", "empty") for r in results),
              "failed": sum(r["status"] in ("failed", "partial") for r in results), "sources": results}
    fd = os.open(str(snapshot / "collection.json"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"status": status, "private_sources": private}, stream, ensure_ascii=False, indent=2)
    return sources, status, private


def collection_notice(status):
    if status["mode"] == "registered":
        return (f"登録した取得先 {status['requested']}件中 {status['successful']}件で取得処理が完了、"
                f"{status['failed']}件が失敗または一部除外。未登録の端末・アカウント全体を網羅する保証はありません。")
    return "指定したローカル履歴のみの集計です。別PC・サーバー・アカウント全体の利用は含みません。"
