"""项目校验 CLI（DOM-003）。

用法::

    python -m domain_model.validate <project_dir> [--json]

- 有效：stdout 打印 ``VALID: <n> objects checked``，退出码 0；
- 无效：按可读列表（或 ``--json`` 时按 JSON 行）输出每个问题
  （文件、JSON Pointer 字段路径、规则 ID、修复提示），退出码 1。

注意：直接以 ``python -m domain_model.validate`` 运行时，需要保证仓库的
``packages/`` 目录在 ``sys.path`` 上（pytest 已通过 pyproject 注入；命令行
可 ``PYTHONPATH=packages`` 或使用 ``python packages/domain_model/validate.py``）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # 直接以脚本方式运行时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # packages/

from domain_model.validation import ValidationResult, validate_project_dir  # noqa: E402


def _configure_stdio() -> None:
    """Windows 控制台默认代码页可能不是 UTF-8，统一按 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def _report(result: ValidationResult, *, json_mode: bool) -> None:
    """把校验结果写到 stdout。"""
    if result.ok:
        print(f"VALID: {result.objects_checked} objects checked")
        return
    for issue in result.issues:
        print(issue.format(json_mode=json_mode))
    summary = f"INVALID: {len(result.issues)} issue(s) in {result.root}"
    print(summary)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；返回进程退出码。"""
    _configure_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m domain_model.validate",
        description="校验视觉自动化项目目录（Schema + 跨对象引用 + 受保护目标硬锁）",
    )
    parser.add_argument("project_dir", help="项目目录（含 project.yaml）")
    parser.add_argument("--json", action="store_true", help="以 JSON 行格式输出问题")
    args = parser.parse_args(argv)

    result = validate_project_dir(args.project_dir)
    _report(result, json_mode=args.json)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
