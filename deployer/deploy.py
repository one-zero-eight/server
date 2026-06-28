import hmac
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
from collections.abc import Iterator
from pathlib import Path, PurePath

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings
from config_schema import RepositoryConfig


app = FastAPI()
logger = logging.getLogger(__name__)
DEPLOY_LOCK = threading.Lock()
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-fA-F]+$")
SERVICE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
auth_scheme = HTTPBearer(auto_error=False)


def validate_webhook_secret(
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    if not hmac.compare_digest(credentials.credentials, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook_secret.")


def _validate_image_id(image_id: str) -> None:
    if not IMAGE_ID_PATTERN.fullmatch(image_id):
        raise HTTPException(status_code=400, detail="Invalid image_id format, expected sha256:<hex>.")


def _validate_services(services: list[str]) -> None:
    for service in services:
        if not SERVICE_NAME_PATTERN.fullmatch(service):
            raise HTTPException(status_code=400, detail=f"Invalid service name: {service}")


def _build_deploy_command(deploy_script: Path, image_id: str, services: list[str]) -> list[str]:
    command = [str(deploy_script), "--image-id", image_id]
    for service in services:
        command.extend(["--service", service])
    return command


def _sanitize_ref(ref: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in ref).strip("._-") or "ref"


def _is_member_path_safe(member_name: str) -> bool:
    member_path = PurePath(member_name)
    return not member_path.is_absolute() and ".." not in member_path.parts


def _get_repository_config(repository: str) -> RepositoryConfig:
    repository_config = settings.repositories.get(repository)
    if not repository_config:
        raise HTTPException(status_code=404, detail=f"Repository config not found: {repository}")

    return repository_config


def deploy_stream(deploy_script: Path, image_id: str, ref: str, services: list[str]) -> Iterator[str]:
    work_dir = deploy_script.parent
    with DEPLOY_LOCK:
        logger.info(
            "Deploy start: script=%s ref=%s image_id=%s services=%s",
            deploy_script,
            ref,
            image_id,
            services,
        )
        try:
            process = subprocess.Popen(
                _build_deploy_command(deploy_script, image_id, services),
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                yield line

            return_code = process.wait()
            if return_code != 0:
                yield f"\nDeployment failed with exit code {return_code}\n"
            else:
                yield "\nDeployment completed successfully\n"
        finally:
            logger.info(
                "Deploy finish: script=%s ref=%s image_id=%s services=%s",
                deploy_script,
                ref,
                image_id,
                services,
            )


def deploy_static_stream(
    archive: UploadFile,
    target_symlink: Path,
    new_target_dir: Path,
    ref: str,
) -> Iterator[str]:
    with DEPLOY_LOCK:
        logger.info("Deploy static start: symlink=%s ref=%s target=%s", target_symlink, ref, new_target_dir)
        try:
            yield f"Starting static deploy for ref={ref}\n"
            if new_target_dir.exists() or new_target_dir.is_symlink():
                yield f"Cleaning existing directory: {new_target_dir}\n"
                if new_target_dir.is_dir() and not new_target_dir.is_symlink():
                    shutil.rmtree(new_target_dir)
                else:
                    new_target_dir.unlink()

            yield f"Creating directory: {new_target_dir}\n"
            new_target_dir.mkdir(parents=True, exist_ok=True)

            yield "Extracting archive\n"
            _extract_archive_safely(archive, new_target_dir)

            if target_symlink.exists() and not target_symlink.is_symlink():
                yield f"ERROR: target path exists and is not a symlink: {target_symlink}\n"
                return

            tmp_symlink = target_symlink.parent / f".{target_symlink.name}.tmp-link"
            if tmp_symlink.exists() or tmp_symlink.is_symlink():
                tmp_symlink.unlink()

            yield f"Switching symlink: {target_symlink} -> {new_target_dir}\n"
            os.symlink(str(new_target_dir), str(tmp_symlink))
            os.replace(str(tmp_symlink), str(target_symlink))
            yield "Static deploy completed successfully\n"
        except tarfile.TarError as exc:
            shutil.rmtree(new_target_dir, ignore_errors=True)
            yield f"ERROR: invalid tar.xz archive: {exc}\n"
        except HTTPException as exc:
            shutil.rmtree(new_target_dir, ignore_errors=True)
            yield f"ERROR: {exc.detail}\n"
        except Exception as exc:
            shutil.rmtree(new_target_dir, ignore_errors=True)
            logger.exception("Static deploy failed")
            yield f"ERROR: unexpected failure: {exc}\n"
        finally:
            archive.file.close()
            logger.info("Deploy static finish: symlink=%s ref=%s target=%s", target_symlink, ref, new_target_dir)


def _extract_archive_safely(archive: UploadFile, destination: Path) -> None:
    with tarfile.open(fileobj=archive.file, mode="r:xz") as tar:
        for member in tar.getmembers():
            if not _is_member_path_safe(member.name):
                raise HTTPException(status_code=400, detail=f"Unsafe archive entry: {member.name}")
            if member.issym() or member.islnk():
                raise HTTPException(status_code=400, detail=f"Links are not allowed in archive: {member.name}")
            if member.isdev():
                raise HTTPException(
                    status_code=400, detail=f"Device files are not allowed in archive: {member.name}"
                )

        tar.extractall(path=destination)


@app.post("/deploy")
def deploy(
    repository: str = Query(...),
    image_id: str = Body(...),
    ref: str = Body(...),
    services: list[str] = Body(default=[]),
    _: None = Depends(validate_webhook_secret),
) -> StreamingResponse:
    _validate_image_id(image_id)
    _validate_services(services)
    logger.info(
        "Deploy request: repository=%s ref=%s services=%s",
        repository,
        ref,
        services,
    )

    repository_config = _get_repository_config(repository)
    if repository_config.deploy_script is None:
        raise HTTPException(
            status_code=400,
            detail=f"deploy_script is not configured for {repository}",
        )

    deploy_script = Path(repository_config.deploy_script)
    if not deploy_script.is_file():
        raise HTTPException(status_code=404, detail=f"Deploy script not found: {deploy_script}")

    return StreamingResponse(
        deploy_stream(deploy_script, image_id, ref, services),
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/deploy-static")
def deploy_static(
    repository: str = Query(...),
    ref: str = Form(...),
    archive: UploadFile = File(...),
    _: None = Depends(validate_webhook_secret),
) -> StreamingResponse:
    logger.info("Deploy static request: repository=%s ref=%s", repository, ref)

    repository_config = _get_repository_config(repository)
    if repository_config.static_dir is None:
        raise HTTPException(
            status_code=400,
            detail=f"static_dir is not configured for {repository}",
        )

    target_symlink = Path(repository_config.static_dir)
    base_parent = target_symlink.parent
    base_parent.mkdir(parents=True, exist_ok=True)
    safe_ref = _sanitize_ref(ref)
    new_target_dir = base_parent / f"{target_symlink.name}-{safe_ref}"

    return StreamingResponse(
        deploy_static_stream(
            archive=archive,
            target_symlink=target_symlink,
            new_target_dir=new_target_dir,
            ref=ref,
        ),
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
