"""诊断包 CLI 包装（REL-004）：security_kit.sanitize.diagnostic_bundle。

用法::

    python tools/release_packager/diagnostics.py --project <项目目录> --out <diag.zip>

行为：
- 收集版本信息、脱敏后的配置（*.yaml/*.json）与最近日志（*.log）；
- **不含任何原图/截图**（png/jpg/...一律排除并计数）；
- 成员名与内容均过脱敏与路径规则；附成员 sha256 清单（bundle_manifest.json）；
- 写包后自检发现图片成员会抛错（防御性，正常不应发生）。

退出码：0 成功；1 参数/生成失败。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# python -m 直跑同款搜索路径（ENG-002）；本脚本直接跑也需要
for _entry in ("packages", "services", "apps"):
    _path = str(ROOT / _entry)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from security_kit.sanitize import diagnostic_bundle  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diagnostics.py",
        description="生成脱敏诊断包（REL-004：版本信息+脱敏配置+脱敏日志，无原图）",
    )
    parser.add_argument("--project", required=True, help="项目/工作区目录")
    parser.add_argument("--out", required=True, help="输出 zip 路径")
    args = parser.parse_args(argv)

    project = Path(args.project)
    if not project.is_dir():
        print(f"FAIL: 项目目录不存在：{project}", file=sys.stderr)
        return 1

    report = diagnostic_bundle(project, args.out)
    print(f"诊断包已生成：{report.output_path}")
    print(f"成员数：{len(report.members)}；排除原图/截图：{report.excluded_images} 个")
    for member in report.members:
        print(f"  - {member}")
    if report.has_images:  # 防御性自检（diagnostic_bundle 内部也会抛错）
        print("FAIL: 诊断包内发现图片成员（违反 REL-004）", file=sys.stderr)
        return 1
    print("隐私自检：无图片成员（PASS）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
