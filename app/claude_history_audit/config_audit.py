"""Read cost-relevant Claude Code settings of THIS machine. Allowlisted values and file sizes only.

Never returns secrets, hook commands, file contents, or paths in the aggregate. Paths go to the
private local map. Settings describe the current machine, not the historical state of each session.
"""
import json
import os
import re
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
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_FILE:
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
    try:
        if path.is_file() and not path.is_symlink():
            return path.stat().st_size
    except OSError:
        pass
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
    return text.startswith(("//", "\\\\")) or "://" in text


def imports(path, base, roots, unresolved):
    """One level of @imports (Claude Code follows further levels; this is a lower bound).
    Only local paths under the allowed roots are sized; UNC/network paths are never touched."""
    out = []
    try:
        if file_size(path) is None or path.stat().st_size > MAX_FILE:
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
            if file_size(target) is not None:
                out.append(target)
    return out


def instruction_files(cwd, config_dir):
    """CLAUDE.md family that Claude Code loads at startup for this cwd, plus auto-memory index."""
    files, unresolved = [], []
    roots = [config_dir, Path.home()] + ([Path(cwd)] if cwd else [])
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
    if cwd:
        here = Path(cwd)
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
        projects.append({"project": project, "instruction_bytes": sum(kinds.values()), "by_kind": kinds,
                         "files": len(files), "sessions": (session_counts or {}).get(project, 0),
                         "unresolved_imports": unresolved, "cwd_found": Path(cwd).is_dir() if cwd else False})
    summary["projects"] = projects
    summary["scope"] = ("この端末の現在のユーザー設定と指示ファイル。各セッション当時の設定とは限らない。"
                        "プロジェクトの .claude/settings.json・実行時の環境変数・管理ポリシーは読まない。取込はcwd・設定・ホーム配下のローカルファイルだけ測る。")
    return summary, private
