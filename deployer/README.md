# Deployer

Webhook service that deploys applications on a server. GitHub Actions calls the API; deployer runs a per-repository script (typically `docker compose up`) or publishes static files from a tarball.

## API

All endpoints require `Authorization: Bearer <webhook_secret>`.

### `POST /deploy`

Deploy a Docker-based repository.

Query:

- `repository` — GitHub repo name, e.g. `one-zero-eight/monorepo` (must match `settings.yaml`)

JSON body:

```json
{
  "image_id": "sha256:abc...",
  "ref": "main",
  "services": []
}
```

- `image_id` — required, format `sha256:<hex>`
- `ref` — branch or tag name (logged only)
- `services` — optional list of compose service names; empty list deploys all services

Response is a streamed plain-text log from the deploy script.

### `POST /deploy-static`

Deploy static files from a `tar.xz` archive.

Query:

- `repository` — must have `static_dir` configured

Form fields:

- `ref` — branch or tag name
- `archive` — `tar.xz` file

Extracts the archive to `<static_dir>-<ref>` and atomically switches a symlink at `static_dir`.

## Configuration

Copy the example and edit secrets locally (do not commit `settings.yaml`):

```bash
cp settings.example.yaml settings.yaml
```

```yaml
webhook_secret: <random-secret>
app_root_path: ""   # e.g. /deployer when served behind a path prefix

repositories:
  one-zero-eight/monorepo:
    deploy_script: /srv/innohassle/monorepo/deploy.sh

  one-zero-eight/website:
    static_dir: /var/www/website
```

Config path can be overridden with `SETTINGS_PATH`. By default the app reads `./settings.yaml` from the working directory.

Repository keys must match `github.repository` in workflows exactly.

## Local development

```bash
uv sync
cp settings.example.yaml settings.yaml
uv run uvicorn deploy:app --host 127.0.0.1 --port 9999
```

```bash
curl -i http://127.0.0.1:9999/deploy
# 401 = service is running
```

## Server setup

### Prerequisites

- Docker with compose plugin
- [uv](https://docs.astral.sh/uv/) in a system path (e.g. `/usr/local/bin/uv`)
- Python 3.14 (installed automatically by `uv`)

### Service user

```bash
sudo useradd --system --shell /sbin/nologin deployer
sudo usermod -aG docker deployer
sudo usermod -d /srv/innohassle/deployer/deployer deployer
```

### Application

```bash
sudo mkdir -p /srv/innohassle/deployer
sudo chown -R deployer:deployer /srv/innohassle/deployer

# clone or pull the repo; app lives in deployer/
sudo -u deployer git -C /srv/innohassle/deployer pull
sudo -u deployer cp /srv/innohassle/deployer/deployer/settings.example.yaml \
  /srv/innohassle/deployer/deployer/settings.yaml
sudo -u deployer nano /srv/innohassle/deployer/deployer/settings.yaml

sudo -u deployer bash -lc 'cd /srv/innohassle/deployer/deployer && uv sync'
```

### Deploy scripts

Each Docker repository needs an executable deploy script and a compose project in the same directory. See `examples/deploy.sh`:

```bash
sudo mkdir -p /srv/innohassle/monorepo
sudo cp examples/deploy.sh /srv/innohassle/monorepo/deploy.sh
sudo chmod +x /srv/innohassle/monorepo/deploy.sh
sudo touch /srv/innohassle/monorepo/.env
sudo chown deployer:deployer /srv/innohassle/monorepo/.env
```

The script receives `--image-id sha256:...` and optional `--service <name>` arguments.

### systemd

`/etc/systemd/system/deployer.service`:

```ini
[Unit]
Description=Deployer API
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=deployer
Group=deployer
WorkingDirectory=/srv/innohassle/deployer/deployer
Environment=HOME=/srv/innohassle/deployer/deployer
ExecStartPre=/usr/local/bin/uv sync --frozen
ExecStart=/usr/local/bin/uv run uvicorn deploy:app --host 127.0.0.1 --port 9999
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now deployer.service
sudo journalctl -u deployer -f
```

### Reverse proxy

Do not expose port 9999 publicly. Put nginx or Caddy in front with TLS. For streamed deploy logs:

```nginx
proxy_buffering off;
proxy_read_timeout 3600s;
```

## GitHub Actions

Copy `examples/build-and-deploy.yaml` into your application repo and set secrets:

| Secret | Example |
|--------|---------|
| `DEPLOYER_STAGING_URL` | `https://deployer-staging.example.com` |
| `DEPLOYER_STAGING_WEBHOOK_SECRET` | same as `webhook_secret` in `settings.yaml` |
| `DEPLOYER_PRODUCTION_URL` | `https://deployer.example.com` |
| `DEPLOYER_PRODUCTION_WEBHOOK_SECRET` | production secret |

The workflow POSTs to `${DEPLOYER_URL}/deploy?repository=${REPOSITORY}`.

## Updating

```bash
sudo -u deployer git -C /srv/innohassle/deployer/deployer pull
sudo -u deployer bash -lc 'cd /srv/innohassle/deployer/deployer && uv sync'
sudo systemctl restart deployer
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `uv: command not found` for `deployer` | Install `uv` to `/usr/local/bin` |
| `Permission denied` on `~/.cache/uv` | Set `deployer` home to the project dir (`usermod -d ...`) |
| `address already in use` on port 9999 | Stop the other process or `systemctl restart deployer` |
| `permission denied` on docker.sock | Add `deployer` to the `docker` group |
| `.env: Permission denied` | `sudo touch .../.env && sudo chown deployer:deployer .../.env` |
| `401 Invalid webhook_secret` | Match GitHub secret and `settings.yaml` |
| `404 Repository config not found` | Repository key must equal `owner/repo` |
| Deploy log cuts off behind proxy | Disable `proxy_buffering`, increase `proxy_read_timeout` |
