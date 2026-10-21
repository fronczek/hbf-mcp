"""Offline installation/upgrade tests: packages, ownership and systemd are mocked."""
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer(tmp_path):
    source = tmp_path / "checkout"
    source.mkdir()
    for name in ("server.py", "test_client.py", "gitea_tools.py", "youtrack_tools.py",
                 "git_workflow_tools.py", "agent_tools.py", "diagnostics_tools.py",
                 "zabbix_tools.py", "bin/zabbix-watch", "bin/kuk-boiler-watch",
                 "requirements.txt", "hbf-mcp.env.example", "systemd/hbf-mcp.service",
                 "completion/hbf-mcp"):
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    (tmp_path / "etc/systemd/system").mkdir(parents=True)
    (tmp_path / "locks").mkdir()
    script = (ROOT / "install").read_text()
    script = script.replace('if [[ "${EUID}" -ne 0 ]]; then', "if false; then")
    for old, new in {
        'Path("/opt")': 'Path(' + repr(str(tmp_path / "opt")) + ')',
        "/opt/": str(tmp_path / "opt") + "/",
        "/run/lock/": str(tmp_path / "locks") + "/",
        "/etc/systemd/system/": str(tmp_path / "etc/systemd/system") + "/",
        "/etc/hbf-mcp.env": str(tmp_path / "etc/hbf-mcp.env"),
        "/etc/bash_completion.d": str(tmp_path / "etc/bash_completion.d"),
        "/var/backups/": str(tmp_path / "backups") + "/",
    }.items():
        script = script.replace(old, new)
    # Port availability is mocked; allocated and reserved port logic remains real.
    script = script.replace('probe.bind(("0.0.0.0", port))', "None")
    (source / "install").write_text(script)

    commands = tmp_path / "commands"
    commands.mkdir()
    real_python = shlex.quote(sys.executable)
    runtime_python = '#!/bin/sh\nif [ "$1:$2" = "-m:pip" ]; then exit 0; fi\nexec ' + real_python + ' "$@"\n'
    wrappers = {
        "apt-get": "#!/bin/sh\nexit 0\n",
        "chown": "#!/bin/sh\nexit 0\n",
        "id": '#!/bin/sh\nif [ "$1" = "-gn" ]; then printf "%s\\n" "$2"; fi\nexit 0\n',
        "getent": '#!/bin/sh\nprintf "%s:x:1001:1001::%s/%s:/bin/bash\\n" "$2" "$HOMES" "$2"\n',
        "systemctl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SERVICE_CALLS"\n',
        "install": """#!""" + sys.executable + """
import os, sys
args = sys.argv[1:]
filtered = []
while args:
    arg = args.pop(0)
    if arg in ("-o", "-g"): args.pop(0)
    else: filtered.append(arg)
os.execv("/usr/bin/install", ["install", *filtered])
""",
        "python3": "#!" + sys.executable + "\n" + f"""
import pathlib, os, sys
if sys.argv[1:3] == ["-m", "venv"]:
    directory = pathlib.Path(sys.argv[3]) / "bin"
    directory.mkdir(parents=True)
    (directory / "python").write_text({runtime_python!r})
    (directory / "python").chmod(0o755)
    (directory / "pip").write_text("#!/bin/sh\\nexit 0\\n")
    (directory / "pip").chmod(0o755)
else:
    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
""",
    }
    for name, content in wrappers.items():
        path = commands / name
        path.write_text(content)
        path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(commands) + os.pathsep + env["PATH"]
    env["SERVICE_CALLS"] = str(tmp_path / "service-calls")
    env["HOMES"] = str(tmp_path / "homes")

    def run(*args, ok=True):
        result = subprocess.run(["bash", str(source / "install"), *args],
                                env=env, text=True, capture_output=True, timeout=30)
        if ok:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0
        return result
    return source, run


def test_no_arguments_only_lists_instances(tmp_path, installer):
    _, run = installer
    result = run()
    assert "No installed HBF MCP instances." in result.stdout
    assert not (tmp_path / "opt").exists()


def test_upgrade_preserves_config_port_and_private_data(tmp_path, installer):
    source, run = installer
    private_state = tmp_path / "homes/afronczek/credentials/synthetic.yml"
    private_state.parent.mkdir(parents=True)
    private_state.write_text("synthetic: untouched\n")
    run("artur", "afronczek")
    runtime = tmp_path / "opt/hbf-mcp-artur"
    config = runtime / ".env"
    original = config.read_text() + "CUSTOM_SETTING=unchanged\n"
    config.write_text(original)
    assert "HBF_MCP_PORT=8765" in original
    assert config.stat().st_mode & 0o777 == 0o600
    (source / "server.py").write_text((source / "server.py").read_text() + "\n# changed version\n")
    run("artur", "afronczek")
    assert (runtime / "server.py").read_bytes() == (source / "server.py").read_bytes()
    assert config.read_text() == original
    assert private_state.read_text() == "synthetic: untouched\n"
    for helper in ("kuk-boiler-watch", "zabbix-watch"):
        assert (tmp_path / "homes/afronczek/bin" / helper).read_bytes() == (source / "bin" / helper).read_bytes()
    backups = list((tmp_path / "backups/hbf-mcp-artur").iterdir())
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o700
    assert (backups[0] / "runtime/.env").read_text() == original
    calls = (tmp_path / "service-calls").read_text()
    assert calls.count("restart hbf-mcp-artur\n") == 2


def test_instances_get_consecutive_ports_and_separate_homes(tmp_path, installer):
    _, run = installer
    run("artur", "afronczek")
    artur = tmp_path / "opt/hbf-mcp-artur"
    original = (artur / ".env").read_bytes()
    run("mariusz", "mhejczyk")
    run("third", "thirduser")
    run("mariusz", "mhejczyk")
    assert (artur / ".env").read_bytes() == original
    second = tmp_path / "opt/hbf-mcp-mariusz"
    config = (second / ".env").read_text()
    assert "HBF_MCP_PORT=8766" in config
    assert "HBF_MCP_PORT=8767" in (tmp_path / "opt/hbf-mcp-third/.env").read_text()
    assert str(tmp_path / "homes/mhejczyk") in config
    unit = (tmp_path / "etc/systemd/system/hbf-mcp-mariusz.service").read_text()
    assert "User=mhejczyk" in unit
    assert "@" not in unit
    assert f"EnvironmentFile={second}/.env" in unit
    assert "HBF_MCP_SERVICE_NAME=hbf-mcp-mariusz" in unit
    assert not (tmp_path / "etc/hbf-mcp.env").exists()
    listing = run().stdout
    assert "hbf-mcp-artur" in listing and "afronczek" in listing
    assert "hbf-mcp-mariusz" in listing and "mhejczyk" in listing
    assert "8766" in listing


def test_changing_instance_owner_or_reusing_account_is_refused(installer):
    _, run = installer
    run("artur", "afronczek")
    run("artur", "otheruser", ok=False)
    run("other", "afronczek", ok=False)


@pytest.mark.parametrize("args", [
    ["../bad", "user"],
    ["other"],
    ["other", "user", "--port", "99999"],
    ["other", "root"],
])
def test_invalid_instance_options_are_rejected_before_install(installer, args):
    _, run = installer
    run(*args, ok=False)


def test_service_completion_includes_restart_and_preserves_other_services():
    script = '''
source "$1"
_comp_complete_service() { COMPREPLY=(original-completion); }
COMP_CWORD=2
COMP_WORDS=(service hbf-mcp-second re)
_hbf_mcp_service_completion
[[ "${COMPREPLY[*]}" == restart ]] || exit 1
COMP_WORDS=(service ssh re)
_hbf_mcp_service_completion
[[ "${COMPREPLY[*]}" == original-completion ]]
'''
    result = subprocess.run(["bash", "-c", script, "bash", str(ROOT / "completion/hbf-mcp")])
    assert result.returncode == 0


def test_explicit_reserved_port_is_rejected_before_install(tmp_path, installer):
    _, run = installer
    run("artur", "afronczek")
    run("mariusz", "mhejczyk", "--port", "8765", ok=False)
    assert not (tmp_path / "opt/hbf-mcp-mariusz").exists()


def test_runtime_paths_follow_the_selected_account(tmp_path):
    import json
    env = os.environ.copy()
    for key in ("HBF_MCP_DEFAULT_CWD", "HBF_MCP_READ_ROOTS",
                "HBF_MCP_WRITE_ROOTS", "HBF_MCP_BOILER_HELPER"):
        env.pop(key, None)
    env["HBF_MCP_AGENT_HOME"] = str(tmp_path / "private-agent")
    env["HBF_MCP_SERVICE_NAME"] = "hbf-mcp-test"
    code = """
import json
import server, gitea_tools, youtrack_tools, agent_tools, git_workflow_tools, zabbix_tools, diagnostics_tools
paths = [server.CONFIG.default_cwd, server.CONFIG.boiler_helper,
         gitea_tools.CREDENTIAL_FILE, gitea_tools.DB_FILE,
         youtrack_tools.CREDENTIALS, agent_tools.HELPER, agent_tools.BROWSER,
         agent_tools.CONFIG, git_workflow_tools.HOME, zabbix_tools.HELPER,
         diagnostics_tools.HOME]
print(json.dumps({"paths": [str(p) for p in paths],
                  "service": diagnostics_tools.SERVICE_NAME,
                  "logs": server.CONFIG.allowed_log_services}))
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)
    assert all(path.startswith(env["HBF_MCP_AGENT_HOME"]) for path in data["paths"])
    assert data["service"] == "hbf-mcp-test"
    # Installed instance .env and fallback both select the correct log service.
    if "HBF_MCP_ALLOWED_LOG_SERVICES" not in env:
        assert data["logs"] == ["hbf-mcp-test"]
