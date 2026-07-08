#!/usr/bin/env bash
set -euo pipefail

IMAGE_ID=""
SERVICES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image-id)
            IMAGE_ID="$2"
            shift 2
            ;;
        --service)
            SERVICES+=("$2")
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [ -n "$IMAGE_ID" ]; then
    ENV_FILE=".env"
    echo "Got imageid: $IMAGE_ID"
    if [ -f "$ENV_FILE" ]; then
        if [ ! -w "$ENV_FILE" ]; then
            echo "ERROR: $ENV_FILE exists but is not writable by $(whoami)" >&2
            exit 1
        fi
    elif [ ! -w . ]; then
        echo "ERROR: cannot create $ENV_FILE in $(pwd)" >&2
        echo "Run once: sudo touch $(pwd)/$ENV_FILE && sudo chown deployer:deployer $(pwd)/$ENV_FILE" >&2
        exit 1
    fi
    echo "DOCKER_TAG_API=main@$IMAGE_ID" > "$ENV_FILE"
fi

start=$(date +%s)
if [ "${#SERVICES[@]}" -gt 0 ]; then
    docker compose pull --quiet "${SERVICES[@]}"
else
    docker compose pull --quiet
fi
echo "pull finished in $(( $(date +%s) - start ))s"

start=$(date +%s)
if [ "${#SERVICES[@]}" -gt 0 ]; then
    docker compose up --force-recreate --wait --wait-timeout 180 "${SERVICES[@]}"
else
    docker compose up --force-recreate --wait --wait-timeout 180
fi
echo "up finished in $(( $(date +%s) - start ))s"
