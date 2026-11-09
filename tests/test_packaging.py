"""Check the distributable archive, independently of editable installs."""

from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


def test_wheel_contains_app_and_static_files(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [uv, "build", "--wheel", "--no-sources", "--out-dir", str(tmp_path)],
        cwd=root, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
    expected = {
        "cosine_digger/web/app.py", "cosine_digger/web/__init__.py",
        "cosine_digger/__main__.py", "cosine_digger/runtime.py",
        "cosine_digger/commands/lifecycle.py",
    }
    expected.update(
        path.relative_to(root / "src").as_posix()
        for path in (root / "src/cosine_digger/web/static").rglob("*")
        if path.is_file()
    )
    assert expected <= names, f"Missing from wheel: {sorted(expected - names)}"
    assert not any({"tests", ".planning"} & set(Path(name).parts) for name in names)
