"""Generic, bounded evidence surfaces for a Goose runtime investigation.

The module deliberately does not name a product, choose an Agent identity, or
choose a Hook.  It exposes launch and loader evidence so the Analyst can make
those decisions from facts.  File reads are limited to roots derived from the
bound process and return redacted, bounded content.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable

import psutil


MAX_FILES = 120
MAX_FILE_BYTES = 512 * 1024
MAX_READ_BYTES = 24 * 1024
MAX_DEPTH = 5
MAX_LINE_BYTES = 4 * 1024

_TEXT_SUFFIXES = {
    ".c", ".cc", ".cfg", ".conf", ".cpp", ".css", ".go", ".h", ".hpp",
    ".ini", ".js", ".json", ".jsonc", ".md", ".mjs", ".py", ".pyw",
    ".rs", ".sh", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
_MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "cargo.toml", "go.mod", "gemfile",
    "composer.json", "mix.exs",
}
_SENSITIVE_NAME = re.compile(
    r"(^|[._-])(env|secret|secrets|credential|credentials|token|tokens|password|"
    r"passwd|private|authorized_keys|id_rsa|key)([._-]|$)", re.I
)
_SENSITIVE_SUFFIX = {".pem", ".key", ".p12", ".pfx", ".crt", ".der"}
_SECRET_KEY = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|"
    r"password|passwd|secret|private[_-]?key|bearer)", re.I
)
_SECRET_TEXT = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|"
    r"password|passwd|secret|private[_-]?key|bearer)(\s*[:=]\s*)([^\s,#;]+)"
)


def _resolve(value: str | os.PathLike[str], base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve(strict=False)


def _safe_call(fn, default):
    try:
        return fn()
    except (OSError, psutil.Error, TypeError, ValueError):
        return default


def _path_record(raw: str, source: str, base: Path | None = None) -> dict[str, Any]:
    resolved = _resolve(raw, base)
    try:
        st = resolved.stat()
        exists = True
        is_file = stat.S_ISREG(st.st_mode)
        size = st.st_size if is_file else None
    except OSError:
        exists = False
        is_file = False
        size = None
    return {
        "raw": str(raw),
        "resolved": str(resolved),
        "source": source,
        "exists": exists,
        "is_file": is_file,
        "size": size,
    }


def _looks_like_path(value: str) -> bool:
    if not value or value.startswith("-") or "\x00" in value:
        return False
    lower = value.lower()
    if "/" in value or "\\" in value:
        return True
    return Path(value).suffix.lower() in _TEXT_SUFFIXES or lower in {
        "package.json", "pyproject.toml", "cargo.toml", "go.mod",
    }


def _safe_argv(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    redact_next = False
    for value in values:
        text = str(value)
        lower = text.lower()
        if redact_next:
            result.append("[REDACTED]")
            redact_next = False
            continue
        if _SECRET_KEY.search(lower):
            if "=" in text:
                result.append(text.split("=", 1)[0] + "=[REDACTED]")
            else:
                result.append(text)
                redact_next = True
            continue
        text = re.sub(r"(?i)\b(?:sk|rk|ghp|xoxb|xoxp)-[A-Za-z0-9._-]+\b", "[REDACTED]", text)
        result.append(text[:2000])
    if redact_next:
        result.append("[REDACTED]")
    return result


def _candidate_entries(cmdline: list[str], cwd: Path | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    stop = False
    for index, value in enumerate(cmdline[1:], 1):
        if stop:
            break
        if value in {"-c", "-e", "--eval"}:
            break
        if value == "-m":
            stop = True
            continue
        if not _looks_like_path(value):
            continue
        path = _path_record(value, f"argv[{index}]", cwd)
        if path["exists"] or Path(value).is_absolute() or "/" in value or "\\" in value:
            candidates.append(path)
    return candidates


def entry_surface(process: psutil.Process) -> dict[str, Any]:
    """Return raw/resolved launch evidence without selecting an identity."""
    exe = _safe_call(process.exe, "")
    cmdline = _safe_call(process.cmdline, []) or []
    cwd_raw = _safe_call(process.cwd, "")
    cwd = _resolve(cwd_raw) if cwd_raw else None
    executable = _path_record(exe, "process.exe") if exe else None
    entries = _candidate_entries([str(x) for x in cmdline], cwd)
    roots: list[dict[str, Any]] = []

    def add_root(path: Path | None, source: str):
        if path is None:
            return
        # A process cwd can be / or the user's home.  Neither is a bounded
        # investigation root; only concrete directories near observed launch
        # paths or a non-broad workspace cwd are eligible.
        if path in {Path("/"), Path.home()}:
            return
        value = str(path)
        if any(item["path"] == value for item in roots):
            return
        roots.append({"id": f"root-{len(roots)}", "path": value, "source": source})

    add_root(cwd, "process.cwd")
    if executable and executable["exists"]:
        add_root(Path(executable["resolved"]).parent, "resolved executable directory")
    for entry in entries[:8]:
        add_root(Path(entry["resolved"]).parent, entry["source"] + " directory")
        # Include a few concrete ancestors so a nearby manifest or loader
        # source can be found without turning a home directory into a search
        # scope.  Broad roots are rejected by add_root.
        for depth, parent in enumerate(Path(entry["resolved"]).parents, 1):
            if depth > 4:
                break
            add_root(parent, entry["source"] + f" ancestor[{depth}]")

    parent_rows = []
    try:
        parent = process.parent()
        depth = 1
        while parent is not None and depth <= 4:
            parent_rows.append(_process_identity_row(parent, depth, "parent"))
            parent = parent.parent()
            depth += 1
    except psutil.Error:
        pass

    children = _safe_call(lambda: process.children(recursive=True), []) or []
    child_rows = [_process_identity_row(child, 1, "child") for child in children[:40]]
    opened_files: list[dict[str, Any]] = []
    try:
        for opened in (_safe_call(process.open_files, []) or [])[:80]:
            value = str(getattr(opened, "path", "") or "")
            if not value:
                continue
            if _skip_file(Path(value)):
                continue
            item = _path_record(value, "process.open_files")
            opened_files.append(item)
            add_root(Path(item["resolved"]).parent, "process.open_files directory")
    except (OSError, psutil.Error):
        pass
    return {
        "status": "collected",
        "source": "bound process launch snapshot",
        "target": {
            "pid": process.pid,
            "create_time": _safe_call(process.create_time, None),
        },
        "executable": executable,
        "argv": _safe_argv(cmdline[:40]),
        "cwd": str(cwd) if cwd else None,
        "entry_candidates": entries[:12],
        "parent_chain": parent_rows,
        "children": child_rows,
        "opened_files": opened_files,
        "related_roots": roots[:12],
        "uncertainty": [
            "entry_candidates are paths observed in argv; they are not an identity decision",
            "a resolved path may be a launcher or symlink target; package ownership needs corroboration",
            "process and file observations are point-in-time and may disappear during investigation",
        ],
    }


def _process_identity_row(process: psutil.Process, depth: int, relation: str) -> dict[str, Any]:
    exe = _safe_call(process.exe, "")
    cmd = _safe_call(process.cmdline, []) or []
    cwd = _safe_call(process.cwd, "")
    return {
        "pid": process.pid,
        "create_time": _safe_call(process.create_time, None),
        "relation": relation,
        "depth": depth,
        "name": _safe_call(process.name, ""),
        "exe": exe,
        "resolved_exe": str(_resolve(exe)) if exe else None,
        "argv": _safe_argv(cmd[:20]),
        "cwd": cwd,
    }


def _manifest_summary(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "format": path.suffix.lower().lstrip("."),
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.suffix.lower() == ".json" else None
    except (OSError, UnicodeError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("name", "version", "productName", "description", "type"):
            value = data.get(key)
            if isinstance(value, (str, int, float)):
                result[key] = str(value)[:160]
        bin_value = data.get("bin")
        if isinstance(bin_value, str):
            result["bin_entries"] = [bin_value[:240]]
        elif isinstance(bin_value, dict):
            result["bin_entries"] = [str(v)[:240] for v in bin_value.values() if isinstance(v, str)][:20]
        for key in ("dependencies", "optionalDependencies", "peerDependencies"):
            value = data.get(key)
            if isinstance(value, dict):
                result[key + "_names"] = sorted(str(name)[:160] for name in value)[:100]
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_READ_BYTES]
            names = []
            for line in text.splitlines():
                match = re.match(r"\s*(name|version|package|project)\s*[:=]\s*([^#]+)", line, re.I)
                if match:
                    names.append({"key": match.group(1), "value": match.group(2).strip()[:160]})
            if names:
                result["declared_fields"] = names[:20]
        except OSError:
            pass
    return result


def metadata_candidates(surface: dict[str, Any]) -> dict[str, Any]:
    """Read only bounded package metadata near observed executable/entries."""
    records: list[dict[str, Any]] = []
    paths = []
    executable = surface.get("executable") or {}
    if executable.get("resolved"):
        paths.append(Path(executable["resolved"]))
    paths.extend(Path(item["resolved"]) for item in surface.get("entry_candidates", []) if item.get("resolved"))
    seen: set[str] = set()
    for value in paths:
        for parent in [value.parent, *value.parents][:MAX_DEPTH + 1]:
            for name in _MANIFEST_NAMES:
                path = parent / name
                key = str(path)
                if key in seen or not path.is_file():
                    continue
                seen.add(key)
                try:
                    if path.stat().st_size > 256 * 1024:
                        continue
                except OSError:
                    continue
                item = _manifest_summary(path)
                item["source"] = "metadata near observed launch path"
                item["distance_from_observed_path"] = len(value.parents) - len(parent.parents)
                records.append(item)
                if len(records) >= 50:
                    break
            if len(records) >= 50:
                break
        if len(records) >= 50:
            break
    conflicts: dict[str, list[str]] = {}
    for item in records:
        for key in ("name", "version"):
            if key in item:
                conflicts.setdefault(key, []).append(str(item[key]))
    conflicts = {key: sorted(set(values)) for key, values in conflicts.items() if len(set(values)) > 1}
    return {
        "status": "collected",
        "candidates": records,
        "conflicts": conflicts,
        "uncertainty": [
            "metadata is candidate ownership evidence, not the final Agent identity",
            "a parent manifest without a matching entry or executable relationship may be unrelated",
        ],
    }


def _root_map(surface: dict[str, Any]) -> dict[str, Path]:
    result = {}
    aliases: dict[str, str] = {}
    ambiguous: set[str] = set()
    for index, item in enumerate(surface.get("related_roots", [])):
        path = item.get("path") if isinstance(item, dict) else None
        if isinstance(path, str) and path:
            token = item.get("id") if isinstance(item, dict) else None
            token = token if isinstance(token, str) and token else f"root-{index}"
            result[token] = _resolve(path)
            base = Path(path).name
            if base and base not in {".", ".."} and base not in aliases and base not in ambiguous:
                aliases[base] = token
            elif base:
                aliases.pop(base, None)
                ambiguous.add(base)
    for alias, token in aliases.items():
        result[alias] = result[token]
    return result


def _skip_file(path: Path) -> bool:
    if path.name.startswith(".") and path.name not in {".config", ".settings"}:
        return True
    if _SENSITIVE_NAME.search(path.name) or path.suffix.lower() in _SENSITIVE_SUFFIX:
        return True
    return False


def find_related_files(surface: dict[str, Any], name_pattern: str = "*", scope: str = "all",
                       limit: int = MAX_FILES) -> dict[str, Any]:
    """Enumerate names and metadata below process-derived roots only."""
    if not isinstance(name_pattern, str) or not name_pattern or len(name_pattern) > 120:
        raise ValueError("name_pattern must be a short non-empty glob")
    if any(token in name_pattern for token in ("/", "\\", "..")):
        raise ValueError("name_pattern cannot contain a path")
    roots = _root_map(surface)
    if scope != "all":
        if isinstance(scope, str) and scope.startswith(("/", "\\")):
            requested = _resolve(scope)
            matches = [token for token, root in roots.items() if root == requested]
            if len(matches) == 1:
                scope = matches[0]
        if scope not in roots:
            raise ValueError("unknown related root; use a returned root id or exact returned root path")
        roots = {scope: roots[scope]}
    try:
        limit = max(1, min(int(limit), MAX_FILES))
    except (TypeError, ValueError):
        limit = MAX_FILES
    files: list[dict[str, Any]] = []
    opened = {item.get("resolved") for item in surface.get("opened_files", []) if isinstance(item, dict)}
    for token, root in roots.items():
        if not root.is_dir() or _skip_file(root):
            continue
        try:
            iterator: Iterable[Path] = root.rglob("*")
            for path in iterator:
                try:
                    relative = path.relative_to(root)
                    if len(relative.parts) > MAX_DEPTH or not path.is_file() or _skip_file(path):
                        continue
                    if not fnmatch.fnmatch(path.name, name_pattern):
                        continue
                    size = path.stat().st_size
                    if size > MAX_FILE_BYTES:
                        continue
                    files.append({
                        "path": str(path),
                        "root": token,
                        "relative_path": str(relative),
                        "name": path.name,
                        "suffix": path.suffix.lower(),
                        "size": size,
                        "readable_text_candidate": path.suffix.lower() in _TEXT_SUFFIXES,
                        "state": "opened_by_target" if str(_resolve(path)) in opened else "search_candidate",
                    })
                    if len(files) >= limit:
                        break
                except OSError:
                    continue
        except OSError:
            continue
        if len(files) >= limit:
            break
    return {
        "status": "collected",
        "scope": scope,
        "pattern": name_pattern,
        "files": files,
        "truncated": len(files) >= limit,
        "uncertainty": ["enumeration is bounded and excludes secret-like names, hidden state and oversized files"],
    }


def _under_roots(path: Path, surface: dict[str, Any]) -> tuple[bool, str | None]:
    resolved = _resolve(path)
    for token, root in _root_map(surface).items():
        try:
            resolved.relative_to(root)
            return True, token
        except ValueError:
            continue
    return False, None


def _redact_object(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if _SECRET_KEY.search(str(key)) else _redact_object(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_object(item) for item in value[:100]]
    if isinstance(value, str):
        value = re.sub(r"(?i)\b(?:sk|rk|ghp|xoxb|xoxp)-[A-Za-z0-9._-]+\b", "[REDACTED]", value)
        return value[:2000]
    return value


def _redact_text(value: str) -> str:
    value = _SECRET_TEXT.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", value)
    return re.sub(r"(?i)\b(?:sk|rk|ghp|xoxb|xoxp)-[A-Za-z0-9._-]+\b", "[REDACTED]", value)


def read_related_file(surface: dict[str, Any], path_value: str) -> dict[str, Any]:
    """Read one previously enumerated related file, bounded and redacted."""
    if not isinstance(path_value, str) or not path_value:
        raise ValueError("path is required")
    path = _resolve(path_value)
    allowed, root = _under_roots(path, surface)
    if not allowed:
        raise ValueError("path is outside process-derived roots")
    if _skip_file(path) or not path.is_file():
        raise ValueError("file is not an allowed readable file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("file stat failed: " + type(exc).__name__) from exc
    if size > MAX_FILE_BYTES:
        raise ValueError("file exceeds bounded read size")
    raw = path.read_bytes()[:MAX_READ_BYTES]
    if b"\x00" in raw:
        return {"status": "unsupported", "path": str(path), "root": root, "reason": "binary file"}
    text = raw.decode("utf-8", errors="replace")
    parsed: Any = None
    parse_status = "text_only"
    if path.suffix.lower() in {".json", ".jsonc"}:
        try:
            parsed = _redact_object(json.loads(text))
            parse_status = "json"
        except ValueError:
            parse_status = "invalid_json"
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "status": "collected",
        "path": str(path),
        "root": root,
        "size": size,
        "sha256": digest,
        "parse_status": parse_status,
        "content": _redact_object(parsed) if parsed is not None else _redact_text(text[:MAX_READ_BYTES]),
        "truncated": size > len(raw),
        "line_limit": MAX_LINE_BYTES,
        "uncertainty": ["content is bounded and redacted; absence here does not prove absence at runtime"],
    }
