"""自动化验证: 跑完演示后检查 events.jsonl 是否符合预期.

断言:
  1. evil 进程至少触发 R1 (画像上线)
  2. evil 至少触发 R2/R3/R4 其中之一 (行为检出)
  3. good 进程绝不能有 BLOCK (误报=0, 呼应 Guard 评测铁律)
  4. 每个 BLOCK 都有对应 rule + disposition 字段齐全

用法: python verify.py [--events events.jsonl]
退出码 0=全过, 1=有失败.
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load_events(p):
    rows = []
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(BASE / "events.jsonl"))
    a = ap.parse_args()
    rows = load_events(Path(a.events))
    print(f"事件总数: {len(rows)}", flush=True)

    def has(pred):
        return [r for r in rows if pred(r)]

    def is_py(r, script):
        c = r.get("cmdline") or ""
        return script in c and "bash.exe -c" not in c and (r.get("pname") or "").lower().startswith("python")

    evil = [r for r in rows if is_py(r, "evil_agent.py")]
    good = [r for r in rows if is_py(r, "good_agent.py")]
    print(f"evil相关事件: {len(evil)}, good相关事件: {len(good)}", flush=True)

    fails = []

    def check(name, cond, hint=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {hint}", flush=True)
        if not cond:
            fails.append(name)

    check("evil触发R1画像", any(r.get("rule") == "R1" for r in evil))
    check("evil触发行为检出(R2/R3/R4)",
          any(r.get("rule") in ("R2", "R3", "R4") for r in evil))
    check("good零BLOCK(误报=0)",
          not any(r.get("disposition") == "BLOCK" for r in good),
          f"(good事件{len(good)}条)")
    check("BLOCK事件字段齐全",
          all(r.get("rule") and r.get("pid") and r.get("ts")
              for r in rows if r.get("disposition") == "BLOCK"))

    print("规则分布:", {q: len(has(lambda r, q=q: r.get('rule') == q))
                      for q in ("R1", "R2", "R3", "R4")}, flush=True)
    print("处置分布:", {q: len(has(lambda r, q=q: r.get('disposition') == q))
                      for q in ("ALERT", "BLOCK")}, flush=True)

    if fails:
        print(f"验证失败: {fails}", flush=True)
        return 1
    print("全部验证通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
