"""Read cost-relevant Claude Code settings of THIS machine. Allowlisted values and file sizes only.

Never returns secrets, hook commands, file contents, or paths in the aggregate. Paths go to the
private local map. Settings describe the current machine, not the historical state of each session.
"""
import json
import os
import re
import stat
from pathlib import Path

from .audit import safe_model

ENV_KEYS = ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "CLAUDE_CODE_AUTOCOMPACT_PCT_OVERRIDE",
            "CLAUDE_CODE_PROMPT_CACHE_TTL", "ENABLE_PROMPT_CACHING_1H", "DISABLE_PROMPT_CACHING",
            "MAX_THINKING_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "BASH_MAX_OUTPUT_LENGTH")
TOP_KEYS = ("model", "effortLevel", "autoCompactWindow", "promptCacheTtl", "bashOutputMaxChars",
            "taskOutputMaxChars", "alwaysThinkingEnabled")
SAFE_VALUE = re.compile(r"[A-Za-z0-9_.:\[\]-]{1,40}")
IMPORT = re.compile(r"(?:^|[\s（(「『\"'])@((?:~|[A-Za-z]:)?[^\s)）`'\"」』]+)")
MAX_FILE = 2 * 1024 * 1024


def safe(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if abs(value) < 10**12 else "set"
    if isinstance(value, str):
        return value if SAFE_VALUE.fullmatch(value) else "set"
    return "set"


def load(path):
    checked = local_stat(path)
    if checked is None:
        return {}
    path, info = checked
    try:
        if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def settings_summary(config_dir):
    merged, env, model_effort, hooks, plugins = {}, {}, {}, {}, set()
    found = []
    for name in ("settings.json", "settings.local.json"):
        data = load(config_dir / name)
        if not data:
            continue
        found.append(name)
        for key in TOP_KEYS:
            if key in data:
                merged[key] = safe(data[key])
        raw_env = data.get("env") if isinstance(data.get("env"), dict) else {}
        for key in ENV_KEYS:
            if key in raw_env:
                env[key] = safe(raw_env[key])
        ms = data.get("modelSettings") if isinstance(data.get("modelSettings"), dict) else {}
        for model, conf in ms.items():
            if safe_model(model) != "unknown" and isinstance(conf, dict) and "effortLevel" in conf:
                model_effort[model] = safe(conf["effortLevel"])
        raw_hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        for event, groups in raw_hooks.items():
            if isinstance(event, str) and SAFE_VALUE.fullmatch(event) and isinstance(groups, list):
                hooks[event] = hooks.get(event, 0) + sum(len(g.get("hooks", [])) for g in groups
                                                         if isinstance(g, dict) and isinstance(g.get("hooks"), list))
        ep = data.get("enabledPlugins") if isinstance(data.get("enabledPlugins"), dict) else {}
        plugins |= {k for k, v in ep.items() if v is True}
    return {"files_found": found, "settings": merged, "env": env, "model_effort": model_effort,
            "hook_commands_by_event": hooks, "enabled_plugins": len(plugins)}


def sanitized_project_dir(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def file_size(path):
    checked = local_stat(path)
    if checked is not None and stat.S_ISREG(checked[1].st_mode):
        return checked[1].st_size
    return None


def within(target, roots):
    norm = os.path.normcase(os.path.abspath(str(target)))
    for root in roots:
        r = os.path.normcase(os.path.abspath(str(root)))
        try:
            if os.path.commonpath([norm, r]) == r:
                return True
        except ValueError:
            continue
    return False


def network_path(text):
    # Check before Path/stat: UNC, NT device namespaces and mixed separators.
    return text.replace("\\", "/").startswith("//") or text.startswith("\\") or "://" in text


def local_drive(anchor):
    """Windows drive letters can also refer to network shares."""
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    get_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    get_type.argtypes = [wintypes.LPCWSTR]
    get_type.restype = wintypes.UINT
    # Removable, fixed, optical and RAM drives; reject remote/unknown roots.
    return get_type(anchor) in (2, 3, 5, 6)


def local_stat(path):
    """Check each ancestor without following links or Windows reparse points.

    All later reads must use the returned, lexically normalized path. Never
    resolve an untrusted cwd: resolving a link can itself contact a share.
    This does not sandbox mounts or concurrent filesystem changes.
    """
    text = str(path)
    if not text or network_path(text) or any(ord(c) < 32 for c in text):
        return None
    path = Path(text)
    if not path.is_absolute():
        return None
    path = Path(os.path.abspath(str(path)))
    try:
        if not local_drive(path.anchor):
            return None
        for part in list(reversed(path.parents)) + [path]:
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return None
            if part != path and not stat.S_ISDIR(info.st_mode):
                return None
        return path, info
    except (OSError, ValueError):
        return None


def imports(path, base, roots, unresolved):
    """One level of @imports (Claude Code follows further levels; this is a lower bound).
    Only local paths under the allowed roots are sized; UNC/network paths are never touched."""
    out = []
    checked = local_stat(path)
    if checked is None:
        return out
    path, info = checked
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            return out
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for match in IMPORT.finditer(line):
            raw = match.group(1).rstrip(".,;:）)")
            if network_path(raw):
                unresolved.append(raw)
                continue
            target = Path(raw).expanduser() if raw.startswith("~") or re.match(r"[A-Za-z]:", raw) or raw.startswith("/") else base / raw
            if network_path(str(target)) or not within(target, roots):
                unresolved.append(raw)
                continue
            checked_target = local_stat(target)
            if checked_target is not None and stat.S_ISREG(checked_target[1].st_mode):
                out.append(checked_target[0])
            else:
                unresolved.append(raw)
    return out


def instruction_files(cwd, config_dir):
    """CLAUDE.md family that Claude Code loads at startup for this cwd, plus auto-memory index."""
    files, unresolved = [], []
    checked_cwd = local_stat(cwd) if cwd else None
    here = checked_cwd[0] if checked_cwd and stat.S_ISDIR(checked_cwd[1].st_mode) else None
    roots = [config_dir, Path.home()] + ([here] if here else [])
    def add(kind, path):
        size = file_size(path)
        if size is not None and all(p != path for _, p, _ in files):
            files.append((kind, path, size))
            if kind != "memory_index":
                for target in imports(path, path.parent, roots, unresolved):
                    tsize = file_size(target)
                    if all(p != target for _, p, _ in files):
                        files.append(("import", target, tsize))
    add("user_claude_md", config_dir / "CLAUDE.md")
    if here is not None:
        for level, directory in enumerate([here] + list(here.parents)):
            if level > 12:
                break
            add("project_claude_md", directory / "CLAUDE.md")
            if directory != Path.home():  # ~/.claude/CLAUDE.md is the user file, counted once above
                add("project_claude_md", directory / ".claude" / "CLAUDE.md")
            add("project_local_md", directory / "CLAUDE.local.md")
        add("memory_index", config_dir / "projects" / sanitized_project_dir(cwd) / "memory" / "MEMORY.md")
    return files, len(unresolved)


def audit_config(config_dir, project_cwds, session_counts=None):
    """project_cwds: {project_alias: cwd}. Returns (aggregate, private_paths)."""
    config_dir = Path(config_dir).expanduser()
    summary = settings_summary(config_dir)
    default = os.path.normcase(os.path.abspath(str(config_dir))) == os.path.normcase(os.path.abspath(str(Path.home() / ".claude")))
    home_state = load(Path.home() / ".claude.json") if default else {}
    mcp = home_state.get("mcpServers") if isinstance(home_state.get("mcpServers"), dict) else None
    summary["user_mcp_servers"] = len(mcp) if mcp is not None else None
    projects, private = [], {}
    for project, cwd in sorted(project_cwds.items()):
        files, unresolved = instruction_files(cwd, config_dir)
        kinds = {}
        for kind, path, size in files:
            kinds[kind] = kinds.get(kind, 0) + (size or 0)
            private.setdefault(project, []).append(str(path))
        checked_cwd = local_stat(cwd) if cwd else None
        cwd_found = bool(checked_cwd and stat.S_ISDIR(checked_cwd[1].st_mode))
        projects.append({"project": project, "instruction_bytes": sum(kinds.values()), "by_kind": kinds,
                         "files": len(files), "sessions": (session_counts or {}).get(project, 0),
                         "unresolved_imports": unresolved, "cwd_found": cwd_found,
                         "cwd_status": "local" if cwd_found else "not_inspected"})
    summary["projects"] = projects
    summary["scope"] = ("この端末の現在のユーザー設定と指示ファイル。各セッション当時の設定とは限らない。"
                        "プロジェクトの .claude/settings.json・実行時の環境変数・管理ポリシーは読まない。取込はcwd・設定・ホーム配下のローカルファイルだけ測る。"
                        "ネットワーク・デバイス・相対パス、リンク/ジャンクション経由や所在不明のcwdは未確認とする。")
    return summary, private
