"""Regression coverage for the unified CLI API services and MCP adapters.

All remote client calls and credential loading are stubbed; these tests never
read real credentials or contact Gitea/YouTrack.
"""
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from auth.policy import Policy, Principal, correlation_id, current_policy, current_principal
from hbf_api_tools.config import ConfigurationError
from hbf_api_tools.gitea import Gitea
from hbf_api_tools.identity import acting_agent, add_attribution
from hbf_api_tools.youtrack import YouTrack
from services import identity
from tools import api


def policy():
    return Policy({
        "version": 1,
        "roles": {"reader": {"tools": {"api_gitea_branches": ["hbf/allowed"]}}},
        "bindings": [{"subject": "person", "client_id": "client", "roles": ["reader"]}],
        "service_accounts": {
            "gitea": {"login": "service-git", "host": "testhost", "unix_user": "tester"},
            "youtrack": {"login": "service-yt", "host": "testhost", "unix_user": "tester"},
        },
    })


@pytest.fixture
def authenticated_context():
    ph = current_policy.set(policy())
    uh = current_principal.set(Principal("person", "client", ("reader",)))
    ch = correlation_id.set("corr-test")
    try:
        yield
    finally:
        correlation_id.reset(ch)
        current_principal.reset(uh)
        current_policy.reset(ph)


@pytest.fixture
def stub_credentials():
    """Supply fake config only; no credential file is opened."""
    def load(name):
        if name == "gitea.yml":
            return {"gitea_url": "https://git.hbf.pl", "token_value": "stub-token"}
        if name == "youtrack.yml":
            return {"youtrack_url": "https://youtrack.hbf.pl", "youtrack_token_value": "stub-token"}
        raise AssertionError(f"unexpected config request: {name}")

    with patch("hbf_api_tools.gitea.load_scalar_yaml", side_effect=load), \
         patch("hbf_api_tools.youtrack.load_scalar_yaml", side_effect=load):
        yield


@pytest.mark.parametrize("client_type,default_identity,default_attribution", [
    (Gitea, acting_agent, add_attribution),
    (YouTrack, acting_agent, add_attribution),
])
def test_cli_client_defaults_preserve_local_identity(client_type, default_identity,
                                                       default_attribution, stub_credentials):
    client = client_type()
    assert client._identity_provider is default_identity
    assert client._attribution_provider is default_attribution


@pytest.mark.parametrize("service,expected", [
    ("gitea", "service-git"),
    ("youtrack", "service-yt"),
])
def test_service_identity_uses_bound_server_identity(service, expected, authenticated_context,
                                                      monkeypatch):
    monkeypatch.setattr(identity.socket, "gethostname", lambda: "testhost.example")
    monkeypatch.setattr(identity.os, "geteuid", lambda: 321)
    monkeypatch.setattr(identity.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="tester"))
    assert identity.service_actor(service) == expected


@pytest.mark.parametrize("host,user", [
    ("wronghost", "tester"),
    ("testhost", "wronguser"),
])
def test_service_identity_rejects_wrong_host_or_unix_user(authenticated_context, monkeypatch,
                                                           host, user):
    monkeypatch.setattr(identity.socket, "gethostname", lambda: host)
    monkeypatch.setattr(identity.os, "geteuid", lambda: 321)
    monkeypatch.setattr(identity.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name=user))
    with pytest.raises(PermissionError, match="binding mismatch"):
        identity.service_actor("gitea")


@pytest.mark.parametrize("which", ["policy", "principal"])
def test_service_identity_requires_authenticated_context(which):
    handle = (current_policy if which == "policy" else current_principal).set(None)
    try:
        with pytest.raises(PermissionError, match="Authenticated service context"):
            identity.service_actor("gitea")
    finally:
        (current_policy if which == "policy" else current_principal).reset(handle)


@pytest.mark.parametrize("client_type,resource_route", [
    (Gitea, "/api/v1/user"),
    (YouTrack, "/api/users/me"),
])
def test_downstream_me_mismatch_denies_reads_before_resource_request(
        client_type, resource_route, stub_credentials, monkeypatch):
    monkeypatch.setattr(identity.socket, "gethostname", lambda: "testhost.example")
    monkeypatch.setattr(identity.os, "geteuid", lambda: 321)
    monkeypatch.setattr(identity.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="tester"))
    monkeypatch.setenv("HBF_GITEA_REPOSITORIES", "hbf/allowed")
    kwargs = ({"identity_provider": lambda: "service-git"} if client_type is Gitea
              else {"identity_provider": lambda: "service-yt"})
    client = client_type(**kwargs)
    if client_type is Gitea:
        remote = patch("hbf_api_tools.gitea.request_json", return_value={"login": "wrong-user"})
        operation = lambda: client.branches("hbf/allowed")
    else:
        remote = patch("hbf_api_tools.youtrack.request_json", return_value={"login": "wrong-user"})
        operation = lambda: client.issue("ESE-12")
    with remote as request:
        with pytest.raises(ConfigurationError, match="does not match"):
            operation()
    assert request.call_count == 1
    assert request.call_args.args[1] == resource_route


def _registered_tools():
    functions = {}
    class FakeMCP:
        def tool(self, *, annotations):
            def decorate(function):
                functions[function.__name__] = (function, annotations)
                return function
            return decorate
    api.register_tools(FakeMCP())
    return functions


def test_api_adapters_delegate_to_shared_clients_and_monitoring(monkeypatch):
    registered = _registered_tools()
    gitea = Mock()
    youtrack = Mock()
    monkeypatch.setattr(api, "gitea_client", lambda: gitea)
    monkeypatch.setattr(api, "youtrack_client", lambda: youtrack)
    monkeypatch.setattr(api.monitoring, "list_profiles", Mock(return_value={"profiles": []}))
    monkeypatch.setattr(api.monitoring, "load_monitoring_config", Mock(return_value={"stub": True}))
    monkeypatch.setattr(api.monitoring, "zabbix_report", Mock(return_value={"z": 1}))
    monkeypatch.setattr(api.monitoring, "influx_report", Mock(return_value={"i": 1}))
    monkeypatch.setattr(api.dsh, "dispatch_status", Mock(return_value={"d": 1}))

    assert registered["api_gitea_branches"][0]("hbf/allowed") == {"branches": gitea.branches.return_value}
    gitea.branches.assert_called_once_with("hbf/allowed", 1, 30)
    assert registered["api_gitea_pulls"][0]("hbf/allowed", "closed") == {
        "pull_requests": gitea.pull_requests.return_value}
    gitea.pull_requests.assert_called_once_with("hbf/allowed", "closed", 1, 20)
    assert registered["api_gitea_pull"][0]("hbf/allowed", 4) == gitea.pull_request.return_value
    gitea.pull_request.assert_called_once_with("hbf/allowed", 4)
    assert registered["api_gitea_status"][0]("hbf/allowed", "main") == gitea.commit_status.return_value
    gitea.commit_status.assert_called_once_with("hbf/allowed", "main")
    assert registered["api_gitea_diff"][0]("hbf/allowed", 4) == gitea.pull_diff.return_value
    gitea.pull_diff.assert_called_once_with("hbf/allowed", 4)
    assert registered["api_gitea_files"][0]("hbf/allowed", 4) == {"files": gitea.pull_files.return_value}
    gitea.pull_files.assert_called_once_with("hbf/allowed", 4, 1, 30)
    registered["api_gitea_create_pr"][0]("hbf/allowed", "title", "feature", "body")
    gitea.create_pull_request.assert_called_once_with("hbf/allowed", "title", "feature", "master", "body")
    registered["api_gitea_merge_pr"][0]("hbf/allowed", 4, "sha")
    gitea.merge_pull_request.assert_called_once_with(
        "hbf/allowed", 4, expected_head_sha="sha", delete_branch=True)

    assert registered["api_youtrack_issue"][0]("ESE-12") == youtrack.issue.return_value
    youtrack.issue.assert_called_once_with("ESE-12")
    assert registered["api_youtrack_comments"][0]("ESE-12") == {"comments": youtrack.comments.return_value}
    youtrack.comments.assert_called_once_with("ESE-12", 20)
    registered["api_youtrack_add_comment"][0]("ESE-12", "hello")
    youtrack.add_comment.assert_called_once_with("ESE-12", "hello")
    registered["api_youtrack_set_state"][0]("ESE-12", "Done")
    youtrack.set_state.assert_called_once_with("ESE-12", "Done")

    assert registered["api_monitor_list"][0]() == {"profiles": []}
    api.monitoring.load_monitoring_config.assert_called_once_with()
    api.monitoring.list_profiles.assert_called_once_with({"stub": True})
    assert registered["api_monitor_zabbix"][0]("pve", 12) == {"z": 1}
    api.monitoring.zabbix_report.assert_called_once_with("pve", 12)
    assert registered["api_monitor_influx"][0]("influx") == {"i": 1}
    api.monitoring.influx_report.assert_called_once_with("influx", None)
    assert registered["api_dsh_status"][0]("dispatch-1") == {"d": 1}
    api.dsh.dispatch_status.assert_called_once_with("dispatch-1")


def test_api_tool_annotations_cover_every_read_and_mutation():
    registered = _registered_tools()
    read_names = {
        "api_gitea_branches", "api_gitea_pulls", "api_gitea_pull", "api_gitea_status",
        "api_gitea_diff", "api_gitea_files", "api_youtrack_issue", "api_youtrack_comments",
        "api_monitor_list", "api_monitor_zabbix", "api_monitor_influx", "api_dsh_status",
    }
    write_names = {
        "api_gitea_create_pr", "api_gitea_merge_pr",
        "api_youtrack_add_comment", "api_youtrack_set_state",
    }
    assert set(registered) == read_names | write_names
    for name, (_, annotations) in registered.items():
        if name in read_names:
            assert annotations.read_only_hint is True
            assert annotations.destructive_hint is False
            assert annotations.idempotent_hint is True
        else:
            assert annotations.read_only_hint is False
            assert annotations.destructive_hint is True
            assert annotations.idempotent_hint is False
