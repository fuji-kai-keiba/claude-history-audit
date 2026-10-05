"""Standalone read-only exporter. Caller prepends a literal OPTIONS dictionary."""
import json
import os
import stat
import sys
import zipfile
from pathlib import Path


def export(options, output):
    source = options.get("path")
    root = Path(source).expanduser() if source else Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser() / "projects"
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source_unavailable")
    maximum = options["max_bytes"]
    files, skipped, total, seen = [], 0, 0, set()
    def walk_error(error):
        raise error
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            for name in list(dirs):
                if (Path(directory) / name).is_symlink():
                    dirs.remove(name)
                    skipped += 1
            for name in sorted(names):
                if not name.endswith(".jsonl") or name == "history.jsonl":
                    continue
                path = Path(directory) / name
                before = path.lstat()
                if stat.S_ISLNK(before.st_mode):
                    skipped += 1
                    continue
                if not stat.S_ISREG(before.st_mode):
                    continue
                identity = (before.st_dev, before.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    current = os.fstat(stream.fileno())
                    if (current.st_dev, current.st_ino) != identity:
                        raise ValueError("source_changed")
                    size = current.st_size
                    total += size
                    if total > maximum or len(files) >= 50000:
                        raise ValueError("source_limit")
                    relative = path.relative_to(root).as_posix()
                    with archive.open("files/" + relative, "w", force_zip64=True) as target:
                        remaining = size
                        while remaining:
                            chunk = stream.read(min(65536, remaining))
                            if not chunk:
                                raise ValueError("source_truncated")
                            target.write(chunk)
                            remaining -= len(chunk)
                    files.append({"name": "files/" + relative, "size": size})
        manifest = json.dumps({"version": 1, "root": str(root), "files": files,
                               "skipped_symlinks": skipped, "bytes": total}).encode()
        if len(manifest) > 8 * 1024 * 1024:
            raise ValueError("manifest_limit")
        archive.writestr("manifest.json", manifest)


if __name__ == "__main__":
    try:
        export(OPTIONS, sys.stdout.buffer)
    except Exception:
        sys.exit(2)
