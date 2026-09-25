"""``python -m domain_model`` 入口：转发到项目校验 CLI。

用法::

    python -m domain_model <project_dir> [--json]
"""

from domain_model.validate import main

if __name__ == "__main__":
    raise SystemExit(main())
