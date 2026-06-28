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
    echo "Got imageid: $IMAGE_ID"
    echo "DOCKER_TAG_API=main@$IMAGE_ID" > .env
fi

COMPOSE_CMD=(docker compose up --build --pull always --force-recreate --detach)
if [ "${#SERVICES[@]}" -gt 0 ]; then
    "${COMPOSE_CMD[@]}" "${SERVICES[@]}"
else
    "${COMPOSE_CMD[@]}"
fi
