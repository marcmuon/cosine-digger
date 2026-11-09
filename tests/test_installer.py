"""Exercise the piped installer without real tools, network, or user shell files."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
RETRY = "Check your connection and run the same command again. Steps marked ✓ are done."
FAKE_UV = r'''
import json
import os
from pathlib import Path
import sys

home = Path(os.environ['HOME'])
args = sys.argv[1:]
with (home / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
failure = home / 'fail'
if failure.exists() and failure.read_text() == ' '.join(args[:2]):
    print('fake failure', file=sys.stderr)
    sys.exit(1)
bin_dir = Path(os.environ['UV_TOOL_BIN_DIR'])
state = home / 'installed'
if args == ['--version']:
    version = Path(__file__).with_suffix('.version')
    print('uv ' + (version.read_text() if version.exists() else '0.11.16'))
elif args == ['tool', 'dir', '--bin']:
    print(bin_dir)
elif args == ['tool', 'list']:
    if state.exists():
        print('cosine-digger v' + state.read_text())
        print('- cosine-digger')
    else:
        print('No tools installed', file=sys.stderr)
elif args[:3] == ['python', 'install', '3.12'] and args[3:] in ([], ['--no-bin']):
    version = Path(__file__).with_suffix('.version')
    current = tuple(int(part) for part in (version.read_text() if version.exists() else '0.11.16').split('.'))
    if args[3:] and current < (0, 8):
        sys.exit('unexpected argument --no-bin')  # uv before 0.8 has no --no-bin
elif args[:4] == ['tool', 'install', '--python', '3.12'] and len(args) == 5:
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / 'cosine-digger'
    launcher.write_text('#!/bin/sh\nprintf "START %s %s\\n" "$0" "$*"\n')
    launcher.chmod(0o755)
    state.write_text('0.4.0')
else:
    sys.exit('unexpected uv arguments: ' + repr(args))
'''

FAKE_CURL = r"""
import os
from pathlib import Path
import sys

home = Path(os.environ['HOME'])
(home / 'curl-args').write_text(' '.join(sys.argv[1:]))
if (home / 'fail-curl').exists():
    sys.exit(22)
print('''
test "$UV_NO_MODIFY_PATH" = 1 || exit 1
install_dir=${UV_INSTALL_DIR:-$HOME/.local/bin}
mkdir -p "$install_dir"
cp "$HOME/uv-template" "$install_dir/uv"
chmod +x "$install_dir/uv"
''')
"""


def executable(path, source):
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home with spaces"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    executable(fake_bin / "uv", FAKE_UV)
    executable(fake_bin / "curl", FAKE_CURL)
    executable(home / "uv-template", FAKE_UV)
    environment = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "SHELL": "/bin/zsh",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "UV_CACHE_DIR": str(home / ".cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(home / ".local/share/uv/python"),
        "UV_TOOL_DIR": str(home / ".local/share/uv/tools"),
        "UV_TOOL_BIN_DIR": str(home / ".local/bin"),
        "UV_INSTALL_DIR": str(home / ".local/bin"),
    }
    return environment


def run_installer(environment, *, piped=False):
    return subprocess.run(
        ["/bin/sh"] if piped else ["/bin/sh", str(ROOT / "scripts/start.sh")],
        input=(ROOT / "scripts/start.sh").read_text() if piped else None,
        cwd=environment["HOME"], env=environment,
        text=True, capture_output=True, timeout=10,
    )


def calls(environment):
    return [json.loads(line) for line in
            (Path(environment["HOME"]) / "calls.jsonl").read_text().splitlines()]


def start_line(environment):
    return f"START {environment['UV_TOOL_BIN_DIR']}/cosine-digger start"


def install_lines():
    return [
        "Installing Digger 0.4.0. No sudo needed.",
        "  ✓ uv 0.11.16",
        "  ✓ Python 3.12",
        "  ✓ Digger 0.4.0 → ~/.local/bin/cosine-digger",
    ]


@pytest.mark.parametrize("shell,system,existing,rc,new_terminal", [
    ("/bin/zsh", "Darwin", None, ".zshrc", ["/bin/zsh", "-i", "-c"]),
    ("/bin/bash", "Linux", None, ".bashrc", ["/bin/bash", "-i", "-c"]),
    # A macOS Terminal starts bash as a login shell, which reads the first of these it finds.
    ("/bin/bash", "Darwin", None, ".profile", ["/bin/bash", "-l", "-c"]),
    ("/bin/bash", "Darwin", ".bash_profile", ".bash_profile", ["/bin/bash", "-l", "-c"]),
    ("/opt/homebrew/bin/fish", "Darwin", None, ".config/fish/conf.d/cosine-digger.fish", None),
], ids=["zsh", "bash-linux", "bash-macos", "bash-macos-with-bash_profile", "fish"])
def test_fresh_install_and_rerun(sandbox, shell, system, existing, rc, new_terminal):
    """The PATH line goes where a new Terminal's shell reads it (deep review A1 #8): zsh's .zshrc,
    bash's .bashrc on Linux, a macOS login bash's first startup file, fish's conf.d folder."""
    sandbox["SHELL"] = shell
    executable(Path(sandbox["PATH"].split(":")[0]) / "uname", f"print({system!r})")
    rc_path = Path(sandbox["HOME"]) / rc
    mine = "# the user's own lines\n" if existing else ""
    if existing:
        rc_path.write_text(mine)
    result = run_installer(sandbox, piped=True)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == install_lines() + [
        f"Added ~/.local/bin to your PATH in ~/{rc}. New Terminal windows will find cosine-digger.",
        start_line(sandbox),
    ]
    bin_dir = sandbox["UV_TOOL_BIN_DIR"]
    line = (f'contains -- "{bin_dir}" $PATH; or set -gx PATH "{bin_dir}" $PATH' if new_terminal is None
            else f'export PATH="{bin_dir}:$PATH"')
    expected = mine + f"\n# Added by the Digger installer.\n{line}\n"
    assert rc_path.read_text() == expected
    assert ["tool", "install", "--python", "3.12", "cosine-digger==0.4.0"] in calls(sandbox)

    # A new Terminal's shell, started the way the Terminal starts it, finds the short command.
    # (fish isn't on test machines: its line is checked as written.)
    if new_terminal is not None:
        terminal = {key: value for key, value in sandbox.items() if key != "SHELL"}
        new_shell = subprocess.run(new_terminal + ["cosine-digger start"], env=terminal, cwd=sandbox["HOME"],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        assert new_shell.returncode == 0, new_shell.stderr
        assert new_shell.stdout.strip().splitlines()[-1] == start_line(sandbox)
    (Path(sandbox["HOME"]) / "calls.jsonl").write_text("")
    (Path(sandbox["HOME"]) / "installed").write_text("0.3.1")
    rerun = run_installer(sandbox)
    assert rerun.returncode == 0, rerun.stderr
    assert rerun.stdout.splitlines() == ["Digger 0.3.1 is already installed.", start_line(sandbox)]
    assert rc_path.read_text() == expected
    assert not any(command[:2] in (["python", "install"], ["tool", "install"]) for command in calls(sandbox))


@pytest.mark.parametrize("folder", [".local/bin", "my bin"])
def test_unwritable_rc_still_starts(sandbox, folder):
    """The line and the command it prints work as printed (deep review A1 #9): "~" doesn't expand
    inside quotes, and a folder with a space needs quoting."""
    sandbox["UV_TOOL_BIN_DIR"] = str(Path(sandbox["HOME"]) / folder)
    # A directory fails even when the test runner has elevated filesystem permissions.
    (Path(sandbox["HOME"]) / ".zshrc").mkdir()
    result = run_installer(sandbox)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    shown, bin_dir = "~/" + folder, sandbox["UV_TOOL_BIN_DIR"]
    run = f"{shown}/cosine-digger" if folder == ".local/bin" else f"'{bin_dir}/cosine-digger'"
    assert result.stdout.splitlines() == install_lines()[:3] + [
        f"  ✓ Digger 0.4.0 → {shown}/cosine-digger",
        f"Couldn't add {shown} to your PATH. Add this line to ~/.zshrc, then open a new Terminal:",
        f'  export PATH="{bin_dir}:$PATH"',
        f"Until then, run {run} start",
        start_line(sandbox),
    ]
    line, until = result.stdout.splitlines()[-3:-1]
    for typed in (line.strip() + "; cosine-digger start", until.removeprefix("Until then, run ")):
        ran = subprocess.run(["/bin/sh", "-c", typed], env=sandbox, capture_output=True, text=True, timeout=10)
        assert ran.stdout.strip() == start_line(sandbox), (typed, ran.stderr)


@pytest.mark.parametrize("step,line,completed", [
    ("python install", "  ✗ Python 3.12: download failed", 2),
    ("tool install", "  ✗ Digger 0.4.0: installation failed", 3),
])
def test_failed_step_stops(sandbox, step, line, completed):
    (Path(sandbox["HOME"]) / "fail").write_text(step)
    result = run_installer(sandbox)
    assert result.returncode == 1
    assert result.stdout.splitlines() == install_lines()[:completed] + [line, RETRY]
    assert result.stderr == ""
    assert not (Path(sandbox["HOME"]) / ".zshrc").exists()
    if step == "python install":
        assert not any(command[:2] == ["tool", "install"] for command in calls(sandbox))
        # One attempt: a failed download is never retried with other flags (deep review A1 #6).
        assert [command for command in calls(sandbox) if command[:2] == ["python", "install"]] == [
            ["python", "install", "3.12", "--no-bin"]]


@pytest.mark.parametrize("symlink", [False, True])
def test_unrelated_launcher_is_untouched(sandbox, symlink):
    launcher = Path(sandbox["UV_TOOL_BIN_DIR"]) / "cosine-digger"
    launcher.parent.mkdir(parents=True)
    if symlink:
        launcher.symlink_to("missing-unrelated-program")
    else:
        launcher.write_text("unrelated program")
    result = run_installer(sandbox)
    assert result.returncode == 1
    assert result.stdout.splitlines() == install_lines()[:2] + [
        "  ✗ Digger 0.4.0: another program is already installed as ~/.local/bin/cosine-digger",
        "Rename or remove that file, then run the same command again. Steps marked ✓ are done.",
    ]
    if symlink:
        assert launcher.readlink() == Path("missing-unrelated-program")
    else:
        assert launcher.read_text() == "unrelated program"
    assert not any(command[:2] in (["python", "install"], ["tool", "install"]) for command in calls(sandbox))


@pytest.mark.parametrize("version", [None, "0.4.99", "0.5.0", "1.0.0"])
def test_uv_bootstrap_or_reuse(sandbox, version):
    fake_uv = Path(sandbox["PATH"].split(":")[0]) / "uv"
    if version is None:
        fake_uv.unlink()
    else:
        fake_uv.with_suffix(".version").write_text(version)
    result = run_installer(sandbox)
    assert result.returncode == 0, result.stderr
    downloaded = Path(sandbox["HOME"]) / "curl-args"
    if version in (None, "0.4.99"):
        assert downloaded.read_text() == "-LsSf https://astral.sh/uv/0.11.16/install.sh"
        assert result.stdout.splitlines()[:4] == install_lines()
    else:
        assert not downloaded.exists()
        assert f"  ✓ uv {version}" in result.stdout.splitlines()
    assert result.stdout.splitlines()[-1] == start_line(sandbox)
    # Python goes in without a python3.12 command in the bin folder; uv before 0.8 has no
    # --no-bin (and installs no such command), so it's left out there: one call either way.
    python_calls = [command for command in calls(sandbox) if command[:2] == ["python", "install"]]
    assert python_calls == [["python", "install", "3.12"] + ([] if version == "0.5.0" else ["--no-bin"])]


def test_uv_download_failure(sandbox):
    (Path(sandbox["PATH"].split(":")[0]) / "uv").unlink()
    (Path(sandbox["HOME"]) / "fail-curl").touch()
    result = run_installer(sandbox, piped=True)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [install_lines()[0], "  ✗ uv: download failed", RETRY]


def test_bootstrap_with_default_user_folders(sandbox):
    (Path(sandbox["PATH"].split(":")[0]) / "uv").unlink()
    del sandbox["UV_INSTALL_DIR"]
    del sandbox["XDG_DATA_HOME"]
    result = run_installer(sandbox)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:4] == install_lines()
    assert result.stdout.splitlines()[-1] == start_line(sandbox)


def test_uv_in_bin_folder_and_custom_package(sandbox):
    local_uv = Path(sandbox["UV_TOOL_BIN_DIR"]) / "uv"
    local_uv.parent.mkdir(parents=True)
    (Path(sandbox["PATH"].split(":")[0]) / "uv").rename(local_uv)
    sandbox["DIGGER_PACKAGE"] = str(Path(sandbox["HOME"]) / "test wheel.whl")
    result = run_installer(sandbox)
    assert result.returncode == 0, result.stderr
    assert ["tool", "install", "--python", "3.12", sandbox["DIGGER_PACKAGE"]] in calls(sandbox)
    assert not (Path(sandbox["HOME"]) / "curl-args").exists()


def test_custom_bin_already_on_path(sandbox):
    sandbox["UV_TOOL_BIN_DIR"] = str(Path(sandbox["HOME"]) / "custom bin")
    sandbox["PATH"] += ":" + sandbox["UV_TOOL_BIN_DIR"]
    result = run_installer(sandbox)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == install_lines()[:3] + [
        "  ✓ Digger 0.4.0 → ~/custom bin/cosine-digger", start_line(sandbox),
    ]
    assert not (Path(sandbox["HOME"]) / ".zshrc").exists()
