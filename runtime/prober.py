"""能力探测器: 对未知 harness 二进制跑 --help, 实测它支不支持结构化输出.

只认通用 token(json/stream/print/output-format/non-interactive/verbose),
不认任何 harness 名字与私有 flag. 命中即返回可用监督配置.
这是"自己找到接入方法"的实证步骤, 非先验知识.
"""
import re
import subprocess

TOKENS = ["stream-json", "output-format", "print", "json", "non-interactive", "verbose"]


def probe(exe_argv0, timeout=25):
    try:
        p = subprocess.run([exe_argv0, "--help"], capture_output=True,
                           text=True, timeout=timeout)
        text = (p.stdout or "") + "\n" + (p.stderr or "")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}"}
    found = sorted({t for t in TOKENS if re.search(re.escape(t), text, re.I)})
    caps = []
    if "stream-json" in found or ("output-format" in found and "json" in found):
        caps.append("supervised-stream-json")
    if "print" in found or "non-interactive" in found:
        caps.append("supervised-print")
    return {"ok": True, "tokens": found, "capabilities": caps,
            "help_bytes": len(text)}
