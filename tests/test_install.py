"""Run the real installer flow in a temporary tree with OS/package commands stubbed."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import shlex

ROOT = Path(__file__).resolve().parents[1]


def test_install_can_be_rerun_without_replacing_configuration(tmp_path):
    source = tmp_path / "checkout"
    source.mkdir()
    for name in ("server.py", "test_client.py", "gitea_tools.py", "youtrack_tools.py",
                 "git_workflow_tools.py", "agent_tools.py", "diagnostics_tools.py",
                 "zabbix_tools.py", "bin/zabbix-watch", "bin/kuk-boiler-watch", "requirements.txt",
                 "hbf-mcp.env.example", "systemd/hbf-mcp.service"):
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)

    runtime = tmp_path / "runtime"
    bin_dir = runtime / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text(
        '#!/bin/sh\nif [ "$1:$2" = "-m:pip" ]; then exit 0; fi\nexec '
        + shlex.quote(sys.executable) + ' "$@"\n')
    (bin_dir / "python").chmod(0o755)
    (bin_dir / "pip").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "pip").chmod(0o755)
    (runtime / "server.py").write_text("# previous version\n")
    config = tmp_path / "etc/hbf-mcp.env"
    config.parent.mkdir()
    config.write_text("HBF_MCP_NAME=Existing custom value\n")
    unit = tmp_path / "etc/systemd/hbf-mcp.service"
    unit.parent.mkdir()
    unit.write_text("# previous unit\n")
    helper = tmp_path / "agent/bin/zabbix-watch"
    helper.parent.mkdir(parents=True)
    helper.write_text("# previous helper\n")
    boiler = helper.parent / "kuk-boiler-watch"
    boiler.write_text("# previous boiler helper\n")
    private_state = tmp_path / "agent/credentials/synthetic.yml"
    private_state.parent.mkdir()
    private_state.write_text("synthetic: untouched\n")

    script = (ROOT / "install.sh").read_text()
    # Only the test copy skips the root gate; all writes target this temporary tree.
    script = script.replace('if [[ "${EUID}" -ne 0 ]]; then', "if false; then")
    replacements = {
        "/opt/hbf-mcp": str(runtime),
        "/etc/hbf-mcp.env": str(config),
        "/etc/systemd/system/hbf-mcp.service": str(unit),
        "/var/backups/hbf-mcp": str(tmp_path / "backups"),
        "/home/${SERVICE_USER}/bin": str(helper.parent),
        "/etc/sudoers.d/gptagent": str(tmp_path / "no-sudoers"),
    }
    for old, new in replacements.items():
        script = script.replace(old, new)
    (source / "install.sh").write_text(script)

    fake_bin = tmp_path / "commands"
    fake_bin.mkdir()
    # Keep real file modes/copies; ownership and package/service changes are mocked.
    wrappers = {
        "apt-get": "#!/bin/sh\nexit 0\n",
        "chown": "#!/bin/sh\nexit 0\n",
        "id": "#!/bin/sh\nexit 0\n",
        "systemctl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SERVICE_CALLS"\n',
        "install": """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
filtered = []
while args:
    arg = args.pop(0)
    if arg in ("-o", "-g"):
        args.pop(0)
    else:
        filtered.append(arg)
os.execv("/usr/bin/install", ["install", *filtered])
""",
    }
    for name, content in wrappers.items():
        path = fake_bin / name
        path.write_text(content)
        path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["SERVICE_CALLS"] = str(tmp_path / "service-calls")
    for run in range(2):
        if run == 1:
            with (source / "server.py").open("a") as changed:
                changed.write("\n# next upgrade candidate\n")
        result = subprocess.run(["bash", str(source / "install.sh")],
                                env=env, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert config.read_text() == "HBF_MCP_NAME=Existing custom value\n"
        assert private_state.read_text() == "synthetic: untouched\n"
        assert (runtime / "server.py").read_bytes() == (source / "server.py").read_bytes()
        assert helper.read_bytes() == (source / "bin/zabbix-watch").read_bytes()
        assert boiler.read_bytes() == (source / "bin/kuk-boiler-watch").read_bytes()
    backups = sorted((tmp_path / "backups").iterdir())
    assert len(backups) == 2
    assert any((p / "runtime/server.py").read_text() == "# previous version\n" for p in backups)
    assert any((p / "kuk-boiler-watch").read_text() == "# previous boiler helper\n" for p in backups)
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in backups)
    assert all((p / "hbf-mcp.env").read_text() == config.read_text() for p in backups)
    calls = Path(env["SERVICE_CALLS"]).read_text()
    assert calls.count("restart hbf-mcp\n") == 2
    assert calls.count("is-active --quiet hbf-mcp\n") == 2
