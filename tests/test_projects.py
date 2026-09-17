"""Project compatibility, multi-folder boundaries and desktop connection regressions."""
from urllib.parse import urlsplit, parse_qs
import uuid

import pytest
from fastapi.testclient import TestClient

from harness.core import HarnessError, OperationKey, RuntimeOutcome, CheckpointRef, InterruptRef, new_id
from harness.platform.config import Settings
from harness.server.app import create_app
from harness.server.composition import Services
from harness.storage import Store


@pytest.fixture
def project_api(tmp_path):
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    services = Services(Settings(data_dir=tmp_path / "data", instance_id="project-test"))
    app = create_app(services)
    client = TestClient(app, base_url="http://127.0.0.1:8767")
    client.headers["Origin"] = "http://127.0.0.1:8767"
    ticket = app.state.auth.issue_ticket()
    assert client.post("/api/auth/exchange", json={"ticket": ticket}).status_code == 204
    client.headers["X-CSRF-Token"] = client.cookies["harness_csrf"]
    yield services, client, roots
    client.close()


def write(client, method, url, body):
    return client.request(method, url, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})


def test_legacy_project_and_multiple_sessions_survive_reopen(project_api):
    services, client, roots = project_api
    old = services.dispatch("create_workspace", "local", body={"name": "Legacy", "root_candidate": str(roots[0])})
    project = client.get("/api/projects").json()["items"][0]
    assert project["id"] == old["id"] and project["roots"] == [str(roots[0])]
    result = write(client, "PATCH", f"/api/projects/{old['id']}", {
        "name": "Two folders", "roots": list(map(str, roots)), "expected_revision": 1,
    })
    assert result.status_code == 200, result.text
    sessions = [write(client, "POST", "/api/sessions", {"workspace_id": old["id"], "title": title}).json()
                for title in ["Conversation A", "Conversation B"]]
    assert len({session["id"] for session in sessions}) == 2
    assert len({session["default_branch_id"] for session in sessions}) == 2
    reopened = Store(services.store.path)
    assert reopened.workspace(old["id"])["roots"] == list(map(str, roots))
    assert len(reopened.sessions(old["id"])) == 2
    other = write(client, "POST", "/api/projects", {"name": "Other", "roots": [str(roots[1])]}).json()
    assert client.get(f"/api/sessions?workspace_id={other['id']}").json()["items"] == []
    assert write(client, "PATCH", f"/api/projects/{old['id']}", {
        "name": "Stale", "roots": list(map(str, roots)), "expected_revision": 1,
    }).status_code == 409


@pytest.mark.asyncio
async def test_secondary_folder_tools_and_project_boundary(project_api):
    services, client, roots = project_api
    project = write(client, "POST", "/api/projects", {"name": "Multi", "roots": list(map(str, roots))}).json()
    session = services.dispatch("create_session", "local", body={"workspace_id": project["id"], "agent_spec_id": "default"})
    receipt = services.scheduler.submit("local", session["id"], {
        "branch_id": session["default_branch_id"], "expected_branch_revision": 1,
        "content_parts": [{"type": "text", "text": "test"}],
    })
    ctx = services.scheduler.claim("project-test-worker")
    assert ctx.run_id == receipt["run_id"]
    assert services.store.get("snapshots", ctx.config_snapshot_id)["project_roots"] == list(map(str, roots))
    target = roots[1] / "evidence.txt"
    target.write_text("secondary evidence", encoding="utf-8")
    assert services.policy.path(ctx, str(target)) == target
    assert services.policy.path(ctx, "relative.txt") == roots[0] / "relative.txt"
    tools = services.builtin_tools
    listing = await tools.list_files(ctx, {}, {"path": str(roots[1])})
    assert listing["structured_data"]["items"][0]["name"] == target.name
    search = await tools.search(ctx, {}, {"path": str(roots[1]), "pattern": "evidence"})
    assert search["structured_data"]["matches"][0]["path"] == str(target)
    read = await tools.read_file(ctx, {"id": "project-read"}, {"path": str(target)})
    assert read["structured_data"]["content"] == "secondary evidence"
    with pytest.raises(HarnessError, match="授权"):
        services.policy.path(ctx, str(roots[0].parent / "outside.txt"))
    for root in roots:
        with pytest.raises(HarnessError, match="根目录"):
            services.policy.path(ctx, str(root), write=True)
    assert write(client, "PATCH", f"/api/projects/{project['id']}", {
        "name": "Busy", "roots": [str(roots[0])], "expected_revision": 1,
    }).json()["code"] == "PROJECT_BUSY"
    key = OperationKey(run_id=ctx.run_id, message_id=new_id(), tool_call_id=new_id())
    destination = roots[1] / "approved.txt"
    args = {"path": str(destination), "content": "second root write", "expected_hash": None}
    waiting = await services.gateway.execute(ctx, key, "write_file", args)
    assert not destination.exists()
    checkpoint = CheckpointRef(graph_thread_key=ctx.graph_thread_key, checkpoint_id="project-fixture")
    services.scheduler.settle(ctx, RuntimeOutcome(
        kind="waiting", run_id=ctx.run_id, input_revision=ctx.input_revision, checkpoint_ref=checkpoint,
        interrupt_refs=[InterruptRef(interrupt_id="project-approve", checkpoint_ref=checkpoint,
                                     kind="user", interaction_id=waiting.interaction_id)],
    ))
    interaction = services.store.interaction(waiting.interaction_id)
    services.scheduler.respond("local", interaction["id"], {
        "expected_revision": interaction["revision"], "response": {"decision": "approve_once"},
        "binding_ref": interaction["binding_ref"],
    })
    resumed = services.scheduler.claim("project-test-worker")
    result = await services.gateway.execute(resumed, key, "write_file", args)
    assert result.status == "succeeded"
    assert destination.read_text() == "second root write"


def test_project_validation_picker_and_auth(project_api, monkeypatch):
    _, client, roots = project_api
    from harness.platform import folders
    monkeypatch.setattr(folders, "choose_folder", lambda: {"path": str(roots[0]), "cancelled": False})
    assert client.post("/api/local/folder-picker").json()["path"] == str(roots[0])
    monkeypatch.setattr(folders, "choose_folder", lambda: {"path": None, "cancelled": True})
    assert client.post("/api/local/folder-picker").json()["cancelled"]
    for candidates in [[], ["missing"], [str(roots[0] / "missing")]]:
        assert write(client, "POST", "/api/projects", {"name": "Invalid", "roots": candidates}).status_code == 422
    result = write(client, "POST", "/api/projects", {"name": "Dedup", "roots": [str(roots[0]), str(roots[0] / ".")]}).json()
    assert result["roots"] == [str(roots[0])]
    assert client.post("/api/local/folder-picker", headers={"Origin": "https://evil.invalid"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/local/folder-picker").status_code == 401


def test_launcher_uses_running_port_and_does_not_print_ticket(tmp_path, monkeypatch, capsys):
    from harness.platform import cli
    from harness.server.auth import AuthService
    opened = []
    monkeypatch.setattr(cli, "start", lambda config: {"status": "ready", "host": "127.0.0.1", "port": 8877})
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["--data-dir", str(tmp_path), "serve", "--open"]) == 0
    url = urlsplit(opened[0])
    assert url.netloc == "127.0.0.1:8877" and not url.query
    ticket = parse_qs(url.fragment)["launch"][0]
    assert ticket not in capsys.readouterr().out
    auth = AuthService(Store(tmp_path / "app.db"))
    token, _ = auth.exchange(ticket, "local")
    assert auth.authenticate(token)["owner_id"] == "local"
    with pytest.raises(HarnessError):
        auth.exchange(ticket, "local")
