"""Real multipart uploads under Windows Store-style long data directories."""
import hashlib
from io import BytesIO
import random
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from harness.artifacts import ArtifactStore
from harness.platform.config import Settings
from harness.server.app import create_app
from harness.server.composition import Services
from harness.storage import Store
from harness.storage.backup import backup, restore
from test_api import pair


def test_jpeg_upload_preview_replay_and_backup_with_long_storage_path(tmp_path):
    # The directory is legal without extended paths; its full hash object is > MAX_PATH.
    data_dir = tmp_path / ("data-" + "d" * max(1, 155 - len(str(tmp_path)) - 6))
    services = Services(Settings(data_dir=data_dir, instance_id="long-upload-test"))
    workspace = services.store.create_workspace("local", "long upload", str(tmp_path), {})
    output = BytesIO()
    Image.frombytes("RGB", (2048, 1536), random.Random(17).randbytes(2048 * 1536 * 3)).save(output, "JPEG", quality=90)
    raw = output.getvalue()
    assert 1024 * 1024 < len(raw) < 10 * 1024 * 1024  # Exercises disk-backed multipart spool too.
    app = create_app(services)
    with TestClient(app, base_url="http://127.0.0.1:8767", raise_server_exceptions=False) as client:
        pair(client, app.state.auth)
        def upload():
            return client.post("/api/artifacts", data={"workspace_id": workspace["id"]},
                               files={"file": ("测试照片.jpg", raw, "image/jpeg")})
        response = upload()
        assert response.status_code == 201, response.text
        ref = response.json()
        row = services.store.artifact(ref["artifact_id"], "local")
        assert len(str(data_dir / "artifacts" / row["storage_key"])) > 260
        assert ref["content_hash"] == hashlib.sha256(raw).hexdigest()
        assert upload().json() == ref
        preview = client.get(f"/api/artifacts/{ref['artifact_id']}?disposition=preview")
        assert preview.status_code == 200 and preview.content == raw
        assert services.artifacts.integrity() == []
        assert services.artifacts.gc_preview() == []
        assert services.artifacts.read_range(ref["artifact_id"], "local", 8, 64) == raw[8:72]
    archive = tmp_path / "backup"
    manifest = backup(data_dir, archive, writers_stopped=True)
    assert "artifacts/" + row["storage_key"] in manifest["files"]
    destination = data_dir.parent / (data_dir.name + "-restored")
    assert restore(archive, destination)["ok"]
    restored = ArtifactStore(Store(destination / "app.db"), destination / "artifacts")
    assert restored.path(ref["artifact_id"], "local").read_bytes() == raw
