"""Portable recipe bundles: move a learned recipe between machines.

A fingerprint one machine learned is the basis for reuse elsewhere, but "one
user learned it and the other ninety-nine reuse it" needs more than a local JSON
file: the recipe, the build constraints it was validated against, and the
evidence that it worked have to travel together, and the receiving side has to
re-check compatibility and re-verify activation for its own instance.

This module packages those three things and checks them on arrival.  It never
installs and never claims activation: importing produces a plan that still needs
authorization, installation and fresh verification on the target.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "asg-recipe-bundle.v1"
REQUIRED_TOP = ("schema", "created_at", "fingerprint", "recipe", "constraints", "verification")

# Machine-specific parameters are replaced by placeholders on export and
# resolved by the receiver, so a bundle never needs manual path edits.
PLACEHOLDER_WORKSPACE = "${TARGET_WORKSPACE}"
PLACEHOLDER_ROOT = "${ASG_ROOT}"
PLACEHOLDER_PYTHON = "${PYTHON_EXECUTABLE}"
PLACEHOLDERS = (PLACEHOLDER_WORKSPACE, PLACEHOLDER_ROOT, PLACEHOLDER_PYTHON)
_SECRET_KEY = ("api_key", "apikey", "authorization", "access_token", "refresh_token",
               "auth_token", "secret", "password", "passwd", "credential", "cookie")
_SECRET_VALUE = ("sk-", "ghp_", "Bearer ", "eyJ")
_CHAT_HINTS = ("assistant.output", "\"role\": \"assistant\"", "\"role\":\"assistant\"")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def portable_recipe(recipe: dict, *, asg_root, target_workspace,
                    python_executable=None) -> tuple[dict, dict]:
    """Replace machine-specific absolute paths with receiver-resolved placeholders."""
    text = json.dumps(recipe, ensure_ascii=False)
    counts = {}
    if python_executable is None:
        python_executable = sys.executable
    for placeholder, value in ((PLACEHOLDER_WORKSPACE, target_workspace),
                               (PLACEHOLDER_ROOT, asg_root),
                               (PLACEHOLDER_PYTHON, python_executable)):
        if isinstance(value, str) and value:
            hits = text.count(value)
            if hits:
                text = text.replace(value, placeholder)
                counts[placeholder] = hits
    # A verified recipe can outlive the checkout that learned it. Recognize
    # ASG-owned runtime paths by their stable suffix so packaging from an
    # installed release can migrate a recipe learned in a developer checkout.
    # Target-owned paths are never rewritten this way.
    owned = {
        r'/Users/[^"\n]+?/runtime/hook_control_client\.py':
            PLACEHOLDER_ROOT + '/runtime/hook_control_client.py',
        r'/Users/[^"\n]+?/artifacts/(?:stage1/dashboard|autonomous-service)/hook-control-client\.json':
            PLACEHOLDER_ROOT + '/artifacts/autonomous-service/hook-control-client.json',
    }
    for pattern, replacement in owned.items():
        text, hits = re.subn(pattern, replacement, text)
        if hits:counts[PLACEHOLDER_ROOT] = counts.get(PLACEHOLDER_ROOT, 0) + hits
    return json.loads(text), counts


def resolve_bundle(bundle: dict, *, asg_root, target_workspace) -> dict:
    """Resolve placeholders for this receiver. No manual edits are required."""
    text = json.dumps(bundle, ensure_ascii=False)
    text = text.replace(PLACEHOLDER_WORKSPACE, str(target_workspace or ""))
    text = text.replace(PLACEHOLDER_ROOT, str(asg_root or ""))
    text = text.replace(PLACEHOLDER_PYTHON, sys.executable)
    resolved = json.loads(text)
    # The receiver transformation is sanctioned, so its own digest is recomputed;
    # the received bundle must be validated (transit integrity) before this.
    if isinstance(resolved.get("integrity"), dict):
        resolved["integrity"] = {"algorithm": "sha256", "digest": digest(resolved),
                                 "resolved_for_receiver": True}
    return resolved


def scan_portability(bundle: dict, *, asg_root=None, target_workspace=None) -> dict:
    """Structural scan: machine install paths, credential keys, chat transcripts.

    It walks the decoded structure rather than the raw text, so a Hook's own
    redaction regex or its event names are not mistaken for leaked secrets or
    chat content.
    """
    import re
    secrets, chat = set(), set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if any(part in str(key).lower() for part in _SECRET_KEY):
                    secrets.add(str(key))
                if str(key).lower() in ("messages", "conversation") and isinstance(value, list):
                    if any(isinstance(x, dict) and str(x.get("role", "")).lower() in ("user", "assistant", "system")
                           for x in value):
                        chat.add(str(key))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            for value in _SECRET_VALUE:
                if re.search(re.escape(value) + r"[A-Za-z0-9._-]{16,}", node):
                    secrets.add(value)

    walk(bundle)
    text = json.dumps(bundle, ensure_ascii=False)
    absolute = sorted(set(re.findall(r"/Users/[^\"\'\s,)}]+", text)))
    roots = [str(r) for r in (asg_root, target_workspace) if r]
    machine = sorted(p for p in absolute if any(p.startswith(r) for r in roots))
    target_env = sorted(p for p in absolute if p not in machine)
    return {"machine_paths": machine, "target_env_paths": target_env,
            "credential_hits": sorted(secrets), "chat_hits": sorted(chat),
            "placeholders": [p for p in PLACEHOLDERS if p in text]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(bundle: dict) -> str:
    """Content digest that ignores the integrity block itself."""
    body = {key: value for key, value in bundle.items() if key != "integrity"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _db(path: Path | None = None) -> dict:
    if path is not None:
        return json.loads(Path(path).read_text())
    from runtime import matcher
    return matcher.load()


def find(fingerprint_id: str, *, db: dict | None = None) -> dict | None:
    for entry in (db or _db()).get("fingerprints", []) or []:
        if entry.get("id") == fingerprint_id:
            return entry
    return None


def export_bundle(fingerprint_id: str, *, db: dict | None = None, note: str | None = None,
                  asg_root=None, target_workspace=None) -> dict:
    """Package one fingerprint's recipe, constraints and verification state.

    Raises ``LookupError`` when the id is unknown and ``ValueError`` when the
    entry carries no recipe — an empty bundle would look like a successful
    hand-off while containing nothing reusable.
    """
    entry = find(fingerprint_id, db=db)
    if entry is None:
        raise LookupError("unknown fingerprint id: %s" % fingerprint_id)
    recipe = entry.get("deployment_recipe") or entry.get("hook_recipe")
    if not isinstance(recipe, dict) or not recipe:
        raise ValueError("fingerprint %s has no hook recipe to export" % fingerprint_id)
    if asg_root is None:
        asg_root = str(_repo_root())
    if target_workspace is None:
        hook_section = recipe.get("hook")
        target_workspace = hook_section.get("workspace") if isinstance(hook_section, dict) else None
    portable, counts = portable_recipe(recipe, asg_root=asg_root, target_workspace=target_workspace)
    revisions = entry.get("revisions") or []
    latest = revisions[-1] if revisions and isinstance(revisions[-1], dict) else {}
    portable_compatibility = copy.deepcopy(latest.get("compatibility"))
    if isinstance(portable_compatibility, dict):
        # These describe the exporting machine's launch location, not the
        # executable/entry bytes used for compatibility. Keeping them would
        # leak a local path and make a portable package look machine-bound.
        portable_compatibility.pop("entry_path", None)
        portable_compatibility.pop("launch", None)
    bundle = {
        "schema": SCHEMA,
        "created_at": _now(),
        "created_by": {"tool": "asg", "host": platform.node(),
                       "platform": platform.system(), "architecture": platform.machine()},
        "fingerprint": {"id": entry.get("id"), "name": entry.get("name"),
                        "revision": entry.get("revision"), "features": entry.get("features")},
        "recipe": portable,
        "machine_parameters": {
            "placeholders": sorted(counts.keys()),
            "substitutions": counts,
            "resolved_on_import": True,
            "note": "接收端按本机解析占位符，不需要手工修改配方中的路径",
        },
        "constraints": {
            "revision": latest.get("revision", entry.get("revision")),
            "compatibility": portable_compatibility,
            "observed": copy.deepcopy(latest.get("observed") or entry.get("features")),
            "covers": ["可执行文件内容", "入口（脚本/模块）内容", "运行时与平台", "启动参数与工作目录"],
            "not_covers": ["配置文件内容", "依赖版本", "MCP/插件集合", "模型路由", "Hook 是否已生效"],
        },
        "verification": {
            "investigation_verified": entry.get("investigation_verified") is True,
            "hook_verified": entry.get("hook_verified") is True,
            "revision_count": len(revisions),
            "recipe_source": entry.get("recipe_source"),
            "local_scope": "验证发生在导出方的实例上；接收方实例必须重新验证",
        },
        "notes": note,
        "limitations": [
            "导入不代表已安装、已加载或已生效",
            "exact 兼容只覆盖上列范围，不覆盖配置、依赖与 Hook 效果",
            "接收方必须完成授权、安装与独立验收后才能声称接通",
        ],
    }
    bundle["integrity"] = {"algorithm": "sha256", "digest": digest(bundle)}
    return bundle


def validate_bundle(bundle: dict) -> dict:
    """Structural check.  Returns warnings; raises ``ValueError`` when unusable."""
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be a JSON object")
    missing = [key for key in REQUIRED_TOP if key not in bundle]
    if missing:
        raise ValueError("bundle missing required keys: %s" % ", ".join(missing))
    if bundle.get("schema") != SCHEMA:
        raise ValueError("unsupported bundle schema: %r" % bundle.get("schema"))
    if not isinstance(bundle.get("recipe"), dict) or not bundle["recipe"]:
        raise ValueError("bundle has no recipe")
    warnings = []
    integrity = bundle.get("integrity")
    if isinstance(integrity, dict) and isinstance(integrity.get("digest"), str):
        if digest(bundle) != integrity["digest"]:
            raise ValueError("bundle digest mismatch: content changed after creation")
    else:
        warnings.append("bundle has no integrity digest; content cannot be checked")
    if not bundle.get("constraints", {}).get("compatibility"):
        warnings.append("bundle carries no build compatibility; only structure can be reused")
    if not bundle.get("verification", {}).get("hook_verified"):
        warnings.append("exporting side never verified the Hook effect")
    return {"ok": True, "warnings": warnings}


def compatibility(bundle: dict, observed_build: dict | None) -> dict:
    """Compare the bundle's validated build against a freshly observed build."""
    stored = (bundle.get("constraints") or {}).get("compatibility")
    if not isinstance(observed_build, dict) or not observed_build:
        return {"status": "unknown", "reason": "no_observed_build",
                "detail": "没有目标构建观测，无法判断兼容性", "differences": []}
    if not isinstance(stored, dict) or not stored:
        return {"status": "unknown", "reason": "bundle_without_compatibility",
                "detail": "该 bundle 未记录构建约束，只能复用结构", "differences": []}
    differences = [key for key in sorted(set(stored) | set(observed_build))
                   if stored.get(key) != observed_build.get(key)]
    if not differences:
        return {"status": "exact", "reason": "observed_build_identical", "differences": [],
                "detail": "构建与启动约束一致；仍未安装、未验证"}
    return {"status": "differs", "reason": "observed_build_changed", "differences": differences,
            "detail": "构建已变化（%s）；应重新调查而不是直接复用" % "、".join(differences)}


def import_bundle(bundle: dict, *, observed_build: dict | None = None,
                  workspace: str | None = None) -> dict:
    """Validate and turn a bundle into a plan that still needs authorization."""
    check = validate_bundle(bundle)
    compat = compatibility(bundle, observed_build)
    ready = compat["status"] == "exact"
    return {
        "status": "ready_for_authorization" if ready else "needs_review",
        "bundle": {"schema": bundle.get("schema"), "created_at": bundle.get("created_at"),
                   "fingerprint_id": (bundle.get("fingerprint") or {}).get("id"),
                   "created_by": bundle.get("created_by"),
                   "digest": (bundle.get("integrity") or {}).get("digest")},
        "warnings": check["warnings"],
        "compatibility": compat,
        "plan": {
            "action": "install_recipe_from_bundle",
            "workspace": workspace,
            "recipe": copy.deepcopy(bundle.get("recipe")),
            "requires": ["本机授权", "写入 Hook 文件（含备份/回滚）", "目标加载", "该实例独立验收"],
            "not_proven_by_import": ["已安装", "已加载", "输入输出可见", "可阻断"],
        },
        "notes": ["导入只证明包结构可用与构建是否一致", "接收方必须用自己的实例独立验收"],
    }


def write(bundle: dict, path: Path | str) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
    return target


def read(path: Path | str) -> dict:
    return json.loads(Path(path).expanduser().read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出／导入可移植配方包")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="列出本机可导出的指纹")
    listing.add_argument("--db")
    export = sub.add_parser("export", help="导出配方包")
    export.add_argument("--fingerprint-id", required=True)
    export.add_argument("--out", required=True)
    export.add_argument("--note")
    export.add_argument("--db")
    inspect = sub.add_parser("inspect", help="校验并查看配方包")
    inspect.add_argument("--file", required=True)
    check = sub.add_parser("check", help="用目标构建观测判断兼容性")
    check.add_argument("--file", required=True)
    check.add_argument("--exe", required=True)
    check.add_argument("--cwd", default="")
    check.add_argument("--argv", nargs="*", default=[])
    args = parser.parse_args(argv)

    if args.command == "list":
        db = _db(Path(args.db)) if args.db else _db()
        for entry in db.get("fingerprints", []) or []:
            print("%s\t%s\trecipe=%s\trev=%s" % (entry.get("id"), entry.get("name"),
                                                  bool(entry.get("hook_recipe")), entry.get("revision")))
        return 0
    if args.command == "export":
        db = _db(Path(args.db)) if args.db else _db()
        bundle = export_bundle(args.fingerprint_id, db=db, note=args.note)
        print(write(bundle, args.out))
        return 0
    bundle = read(args.file)
    if args.command == "inspect":
        check = validate_bundle(bundle)
        print(json.dumps({"ok": check["ok"], "warnings": check["warnings"],
                          "fingerprint": bundle.get("fingerprint"),
                          "verification": bundle.get("verification"),
                          "covers": (bundle.get("constraints") or {}).get("covers")},
                         ensure_ascii=False, indent=2))
        return 0
    from runtime import compatibility as build
    observed = build.observe(args.exe, [args.exe, *args.argv], args.cwd or str(Path.cwd()))
    result = import_bundle(bundle, observed_build=observed, workspace=args.cwd or None)
    print(json.dumps({"status": result["status"], "warnings": result["warnings"],
                      "compatibility": result["compatibility"],
                      "requires": result["plan"]["requires"],
                      "not_proven_by_import": result["plan"]["not_proven_by_import"]},
                     ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready_for_authorization" else 1


__all__ = ["SCHEMA", "compatibility", "digest", "export_bundle", "find", "import_bundle",
           "portable_recipe", "read", "resolve_bundle", "scan_portability", "validate_bundle", "write"]


if __name__ == "__main__":
    sys.exit(main())
