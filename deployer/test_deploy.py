import os
import subprocess
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import cast

os.environ["SETTINGS_PATH"] = str(Path(__file__).with_name("settings.example.yaml"))

import deploy


def test_deploy_locks_are_scoped_by_repository() -> None:
    assert deploy._deploy_lock("owner/first") is deploy._deploy_lock("owner/first")
    assert deploy._deploy_lock("owner/first") is not deploy._deploy_lock("owner/second")


def test_lock_wait_emits_heartbeat(monkeypatch) -> None:
    monkeypatch.setattr(deploy.settings, "heartbeat_interval_seconds", 0.01)
    lock = threading.Lock()
    lock.acquire()
    stream = deploy._acquire_deploy_lock(lock, "owner/repository")

    assert (
        next(stream) == "Waiting for another owner/repository deployment to finish...\n"
    )

    lock.release()
    list(stream)
    lock.release()


def test_process_stream_emits_heartbeat_and_times_out(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deploy.settings, "heartbeat_interval_seconds", 0.01)
    monkeypatch.setattr(deploy.settings, "deploy_timeout_seconds", 0.05)
    process = subprocess.Popen(
        ["python", "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    output = "".join(deploy._stream_process(process))

    assert "Deployment is still running...\n" in output
    assert "Deployment timed out after 0.05 seconds\n" in output
    assert process.poll() is not None


def test_deploy_stream_releases_lock_when_client_disconnects(
    monkeypatch, tmp_path: Path
) -> None:
    script = tmp_path / "deploy.sh"
    script.write_text("#!/usr/bin/env bash\nsleep 60\n")
    script.chmod(0o755)
    repository = "owner/disconnect"
    lock = deploy._deploy_lock(repository)
    monkeypatch.setattr(deploy.settings, "heartbeat_interval_seconds", 0.01)
    monkeypatch.setattr(deploy.settings, "deploy_timeout_seconds", 60)

    stream = cast(
        Generator[str],
        deploy.deploy_stream(repository, script, "sha256:abc", "main", []),
    )
    assert next(stream) == "Deployment is still running...\n"
    stream.close()

    deadline = time.monotonic() + 1
    while lock.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not lock.locked()
