"""Black-box checks for the interactive installer wrapper.

The real installer, Docker daemon, Git remote and systemd are deliberately
replaced with tiny local doubles. These tests exercise the shell script itself
without changing the host or requiring a Discord token.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "installer.sh"


def run_installer(app: Path, *args: str, input_text: str = "", extra_env=None):
    env = os.environ.copy()
    env.update({
        "APP_DIR": str(app),
        "PYTHON_BIN": sys.executable,
        "GIT_BIN": str(app / "bin" / "git"),
        "SYSTEMCTL_BIN": str(app / "bin" / "systemctl"),
    })
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=30,
    )


def make_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "tgdbot" / "scripts").mkdir(parents=True)
    (app / "bin").mkdir()
    return app


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_main_menu_has_the_three_requested_actions(tmp_path):
    app = make_app(tmp_path)
    result = run_installer(app, input_text="0\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TGD BOT" in result.stdout
    assert "1) Install Files Of Bot And Run" in result.stdout
    assert "2) Migration" in result.stdout
    assert "3) Auto Updater" in result.stdout
    assert "\x1b[" not in result.stdout  # captured/non-TTY output stays script-friendly


def test_install_prompts_preserve_blank_values_and_hide_token(tmp_path):
    app = make_app(tmp_path)
    (app / ".env.example").write_text(
        "DISCORD_TOKEN=default-token\nMAIN_ADMIN_ID=111\nOWNER_IDS=222\nKEEP_ME=yes\n",
        encoding="utf-8",
    )
    make_executable(
        app / "tgdbot" / "scripts" / "install.sh",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$NO_START\" > \"$APP_DIR/install-ran\"\n",
    )

    result = run_installer(
        app, "--install",
        input_text="new-secret-token\n\n333333333333333333\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    env_text = (app / ".env").read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=new-secret-token" in env_text
    assert "MAIN_ADMIN_ID=111" in env_text  # blank answer preserved the default
    assert "OWNER_IDS=333333333333333333" in env_text
    assert "KEEP_ME=yes" in env_text
    assert (app / "install-ran").read_text(encoding="utf-8").strip() == "0"
    assert "new-secret-token" not in result.stdout + result.stderr

    # A second installation with three Enter answers must not erase anything.
    result = run_installer(app, "--install", input_text="\n\n\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (app / ".env").read_text(encoding="utf-8") == env_text


def test_migration_option_uses_the_existing_runner(tmp_path):
    app = make_app(tmp_path)
    (app / "tgdbot" / "scripts" / "migrate_database.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(__file__).resolve().parents[2].joinpath('migration-args').write_text(\n"
        "    '\\n'.join(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    (app / ".venv" / "bin").mkdir(parents=True)
    (app / ".venv" / "bin" / "python").symlink_to(sys.executable)

    database = app / "custom.db"
    result = run_installer(app, "--migrate", extra_env={"DATABASE_PATH": str(database)})
    assert result.returncode == 0, result.stdout + result.stderr
    args = (app / "migration-args").read_text(encoding="utf-8")
    assert f"--database\n{database}" in args
    assert f"--report-dir\n{app / 'migration_reports'}" in args


def test_auto_update_is_fast_forward_only_and_runs_post_update_steps(tmp_path):
    app = make_app(tmp_path)
    (app / ".git").mkdir()
    (app / "requirements.txt").write_text("# test requirements\n", encoding="utf-8")
    (app / "tgdbot" / "scripts" / "migrate_database.py").write_text(
        "# the fake Python below records this invocation\n", encoding="utf-8"
    )
    (app / ".venv" / "bin").mkdir(parents=True)

    make_executable(
        app / "bin" / "git",
        """#!/usr/bin/env bash
set -eu
printf 'git %s\\n' "$*" >> "$APP_DIR/commands"
args=" $* "
if [[ "$args" == *" status --porcelain "* ]]; then
  exit 0
elif [[ "$args" == *" rev-parse HEAD "* ]]; then
  count="$(cat "$APP_DIR/rev-count" 2>/dev/null || echo 0)"
  if [[ "$count" == 0 ]]; then echo old-commit; else echo new-commit; fi
  echo $((count + 1)) > "$APP_DIR/rev-count"
elif [[ "$args" == *" pull --ff-only "* ]]; then
  exit 0
else
  echo "unexpected git invocation: $*" >&2
  exit 2
fi
""",
    )
    make_executable(
        app / ".venv" / "bin" / "python",
        """#!/usr/bin/env bash
set -eu
printf 'python %s\\n' "$*" >> "$APP_DIR/commands"
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
  exit 0
elif [[ "$1" == *migrate_database.py ]]; then
  exit 0
else
  echo "unexpected python invocation: $*" >&2
  exit 2
fi
""",
    )
    make_executable(
        app / "bin" / "systemctl",
        "#!/usr/bin/env bash\nprintf 'systemctl %s\\n' \"$*\" >> \"$APP_DIR/commands\"\n",
    )

    result = run_installer(app, "--auto-update")
    assert result.returncode == 0, result.stdout + result.stderr
    commands = (app / "commands").read_text(encoding="utf-8")
    assert "safe.directory=" in commands
    assert "pull --ff-only" in commands
    assert "python -m pip install --quiet -r" in commands
    assert "migrate_database.py" in commands
    assert "systemctl restart tgdbot" in commands


def test_auto_update_refuses_dirty_worktree(tmp_path):
    app = make_app(tmp_path)
    (app / ".git").mkdir()
    make_executable(
        app / "bin" / "git",
        """#!/usr/bin/env bash
printf 'git %s\\n' "$*" >> "$APP_DIR/commands"
if [[ "$*" == *"status --porcelain"* ]]; then
  echo ' M bot.py'
  exit 0
fi
echo 'git pull must not run for a dirty tree' >&2
exit 2
""",
    )
    result = run_installer(app, "--auto-update")
    assert result.returncode != 0
    assert "dirty" in result.stderr or "local or untracked" in result.stderr
    assert "pull --ff-only" not in (app / "commands").read_text(encoding="utf-8")


def test_auto_update_timer_can_be_enabled_and_disabled(tmp_path):
    app = make_app(tmp_path)
    (app / ".git").mkdir()
    systemd = app / "systemd"
    make_executable(
        app / "bin" / "systemctl",
        "#!/usr/bin/env bash\nprintf 'systemctl %s\\n' \"$*\" >> \"$APP_DIR/commands\"\n",
    )

    result = run_installer(app, "--enable-auto-update", extra_env={"SYSTEMD_DIR": str(systemd)})
    assert result.returncode == 0, result.stdout + result.stderr
    service = systemd / "tgdbot-auto-update.service"
    timer = systemd / "tgdbot-auto-update.timer"
    assert service.exists() and timer.exists()
    assert "--auto-update" in service.read_text(encoding="utf-8")
    assert "OnUnitActiveSec=1h" in timer.read_text(encoding="utf-8")

    result = run_installer(app, "--disable-auto-update", extra_env={"SYSTEMD_DIR": str(systemd)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not service.exists() and not timer.exists()
    commands = (app / "commands").read_text(encoding="utf-8")
    assert "enable --now tgdbot-auto-update.timer" in commands
    assert "disable --now tgdbot-auto-update.timer" in commands
