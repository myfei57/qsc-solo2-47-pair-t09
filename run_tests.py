"""Minimal stand-in runner: pytest is not installable in this environment.

Provides the conftest fixtures by hand and executes every test_ function in the
tests directory, reporting failures with tracebacks.
"""

import inspect
import os
import pathlib
import sys
import tempfile
import traceback

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/tests")
sys.path.insert(0, "/workspace/.shims")

from paperops.config import Config  # noqa: E402
from paperops.store import Journal  # noqa: E402
from support import WEB_DIR, build  # noqa: E402


def run_one(fn):
    tmp = tempfile.mkdtemp()
    config = Config(data_dir=os.path.join(tmp, "data"), web_dir=WEB_DIR)
    services = build(config)
    fixtures = {
        "data_dir": os.path.join(tmp, "data2"),
        "config": config,
        "services": services,
        "console": services.console,
        "journal": Journal(os.path.join(tmp, "stream")),
        "tmp_path": pathlib.Path(tmp),
    }
    kwargs = {name: fixtures[name] for name in inspect.signature(fn).parameters}
    fn(**kwargs)


def main():
    modules = sys.argv[1:] or [
        f"test_{name}"
        for name in (
            "acceptance audit broke cal clock console_api dry glue interlock press "
            "pressloop reel server_http state steam store verdict version water"
        ).split()
    ]
    failed = 0
    total = 0
    for module_name in modules:
        module = __import__(module_name)
        for name in sorted(vars(module)):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            total += 1
            label = f"{module_name}::{name}"
            try:
                run_one(fn)
            except Exception:
                failed += 1
                print(f"FAIL {label}")
                traceback.print_exc()
            else:
                print(f"ok   {label}")
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
