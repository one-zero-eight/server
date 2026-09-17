import io
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Never
from unittest.mock import Mock

import anyio
import pytest
from fastapi import UploadFile
from starlette.requests import ClientDisconnect
from starlette.types import Message

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


def test_lock_wait_caps_acquire_timeout(monkeypatch) -> None:
    monkeypatch.setattr(deploy.settings, "heartbeat_interval_seconds", 15)
    monkeypatch.setattr(deploy.settings, "deploy_timeout_seconds", 2)
    clock = iter([100, 100.5, 102])
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(clock))
    lock = Mock(spec=threading.Lock)
    lock.acquire.return_value = False

    stream = deploy._acquire_deploy_lock(lock, "owner/busy")
    assert next(stream) == "Waiting for another owner/busy deployment to finish...\n"
    assert "Deployment timed out after 2 seconds" in next(stream)
    with pytest.raises(StopIteration) as exc:
        next(stream)
    assert exc.value.value is False
    lock.acquire.assert_called_once_with(timeout=1.5)


@pytest.mark.parametrize("static", [False, True])
def test_lock_timeout_does_not_start_deployment(
    monkeypatch, tmp_path: Path, static: bool
) -> None:
    monkeypatch.setattr(deploy.settings, "heartbeat_interval_seconds", 0.01)
    monkeypatch.setattr(deploy.settings, "deploy_timeout_seconds", 0.03)
    repository = "owner/lock-timeout"
    lock = deploy._deploy_lock(repository)
    archive = UploadFile(file=io.BytesIO())

    def unexpected_process(*args, **kwargs):
        pytest.fail("Deployment started without acquiring the lock")

    monkeypatch.setattr(deploy.subprocess, "Popen", unexpected_process)
    if static:
        stream = deploy.deploy_static_stream(
            repository, archive, tmp_path / "site", tmp_path / "site-main", "main"
        )
    else:
        stream = deploy.deploy_stream(
            repository, tmp_path / "deploy.sh", "sha256:abc", "main", []
        )
    lock.acquire()
    try:
        output = "".join(stream)
        assert "Waiting for another owner/lock-timeout deployment" in output
        assert "Deployment timed out after 0.03 seconds waiting" in output
        assert lock.locked()
        assert not (tmp_path / "site-main").exists()
        if static:
            assert archive.file.closed
    finally:
        stream.close()
        archive.file.close()
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

    stream = deploy.deploy_stream(repository, script, "sha256:abc", "main", [])
    assert next(stream) == "Deployment is still running...\n"
    stream.close()

    deadline = time.monotonic() + 1
    while lock.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not lock.locked()


async def disconnect_response(response, spec_version: str) -> None:
    sent_body = anyio.Event()

    async def receive() -> Message:
        await sent_body.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            sent_body.set()
            if spec_version == "2.4":
                raise OSError("Client disconnected")

    with anyio.fail_after(2):
        try:
            await response(
                {"type": "http", "asgi": {"spec_version": spec_version}},
                receive,
                send,
            )
        except ClientDisconnect:
            assert spec_version == "2.4"


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_response_disconnect_reaps_process_and_releases_lock(
    monkeypatch, tmp_path: Path, spec_version: str
) -> None:
    script = tmp_path / "deploy.sh"
    script.write_text("#!/usr/bin/env bash\nexec sleep 60\n")
    script.chmod(0o755)
    repository = "owner/http-disconnect"
    monkeypatch.setattr(deploy.settings, "heartbeat_interval_seconds", 0.01)
    processes = []
    popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(deploy.subprocess, "Popen", capture_process)
    stream = deploy.deploy_stream(repository, script, "sha256:abc", "main", [])
    response = deploy.DeploymentResponse(stream)
    try:
        anyio.run(disconnect_response, response, spec_version)
        assert not deploy._deploy_lock(repository).locked()
        assert len(processes) == 1
        assert processes[0].returncode is not None
    finally:
        stream.close()


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_static_response_disconnect_releases_lock_and_closes_upload(
    tmp_path: Path, spec_version: str
) -> None:
    repository = "owner/static-disconnect"
    archive = UploadFile(file=io.BytesIO())
    stream = deploy.deploy_static_stream(
        repository, archive, tmp_path / "site", tmp_path / "site-main", "main"
    )
    response = deploy.DeploymentResponse(stream, archive=archive)
    try:
        anyio.run(disconnect_response, response, spec_version)
        assert not deploy._deploy_lock(repository).locked()
        assert archive.file.closed
    finally:
        stream.close()
        archive.file.close()


def test_disconnect_while_waiting_preserves_other_deployment_lock(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deploy.settings, "heartbeat_interval_seconds", 0.01)
    repository = "owner/waiting-disconnect"
    lock = deploy._deploy_lock(repository)
    archive = UploadFile(file=io.BytesIO())
    stream = deploy.deploy_static_stream(
        repository, archive, tmp_path / "site", tmp_path / "site-main", "main"
    )
    response = deploy.DeploymentResponse(stream, archive=archive)
    lock.acquire()
    try:
        anyio.run(disconnect_response, response, "2.3")
        assert lock.locked()
        assert archive.file.closed
        assert not (tmp_path / "site-main").exists()
    finally:
        stream.close()
        archive.file.close()
        lock.release()


def test_response_cleanup_survives_cancellation(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "deploy.sh"
    script.write_text("#!/usr/bin/env bash\nexec sleep 60\n")
    script.chmod(0o755)
    repository = "owner/cancelled-response"
    monkeypatch.setattr(deploy.settings, "heartbeat_interval_seconds", 0.01)
    stream = deploy.deploy_stream(repository, script, "sha256:abc", "main", [])
    response = deploy.DeploymentResponse(stream)

    async def cancel_response() -> None:
        async def receive() -> Never:
            while True:
                await anyio.sleep_forever()

        async def send(message: Message) -> None:
            if message["type"] == "http.response.body":
                scope.cancel()
                await anyio.sleep_forever()

        with anyio.fail_after(2), anyio.CancelScope() as scope:
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )

    try:
        anyio.run(cancel_response)
        assert not deploy._deploy_lock(repository).locked()
    finally:
        stream.close()


def test_response_completes_normally(tmp_path: Path) -> None:
    script = tmp_path / "deploy.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'deployed\\n'\n")
    script.chmod(0o755)
    repository = "owner/successful-response"
    response = deploy.DeploymentResponse(
        deploy.deploy_stream(repository, script, "sha256:abc", "main", [])
    )
    chunks = []

    async def run_response() -> None:
        async def receive() -> Never:
            while True:
                await anyio.sleep_forever()

        async def send(message: Message) -> None:
            if message["type"] == "http.response.body":
                chunks.append(message["body"])

        with anyio.fail_after(2):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )

    anyio.run(run_response)
    assert b"deployed\n" in b"".join(chunks)
    assert b"Deployment completed successfully" in b"".join(chunks)
    assert not deploy._deploy_lock(repository).locked()
