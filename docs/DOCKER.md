# Docker operations

The deployment is one container and one named volume.

| Item | Value |
|---|---|
| Image | `amazon-price-monitor:local`, built from `Dockerfile` (multi-stage, about 300 MB) |
| Container | `amazon-price-monitor` |
| Volume | `amazon-price-monitor-data`, mounted at `/app/data` |
| Port | `127.0.0.1:8080` on the host, `8080` in the container |
| User | `app`, uid 10001 (non-root) |
| Restart policy | `unless-stopped` |

## Commands

| Task | Command |
|---|---|
| Build | `docker compose build` |
| Start | `docker compose up -d` |
| Stop | `docker compose down` |
| Restart | `docker compose restart app` |
| Logs | `docker compose logs -f app` |
| Health | `docker compose ps` or `curl -s http://127.0.0.1:8080/api/v1/health` |
| Update | `git pull && docker compose up -d --build` |
| Apply a `.env` change | `docker compose up -d` |
| Run migrations by hand | `docker compose exec app python -m alembic upgrade head` |
| Show the schema version | `docker compose exec app python -m alembic current` |
| Run the tests | `./scripts/docker_test.sh` |
| Reset a lost password | `docker compose exec app python -m app reset-admin && docker compose restart app` |

Migrations run automatically at every start, so the manual command is rarely needed.

## Persistence

Everything that must survive lives in the volume:

| File | Purpose |
|---|---|
| `monitor.db` (plus `-wal`, `-shm`) | The SQLite database |
| `secret-key` | Session signing key, created at first start |
| `setup-token` | One-time setup token, present only until the administrator exists |
| `backups/` | Files written by the backup command |

The volume survives container restarts, container recreation, image upgrades and host restarts.
The image creates `/app/data` owned by uid 10001, and a new named volume inherits that ownership,
so the application can write to it without extra steps.

## Backup

**Automatic.** Once a day the application writes a consistent copy of the database to
`/app/data/backups/monitor-<date>-<time>.db` and keeps the newest seven. Change this with
`BACKUP_INTERVAL_HOURS` and `BACKUP_KEEP`. **Settings** shows the time of the last backup.

**On demand.**

```bash
docker compose exec app python -m app backup
```

Both use SQLite's online backup, so the application keeps running.

**Copy the backups off the volume.** A backup inside the same volume does not protect against
losing the volume or the disk.

```bash
docker compose cp app:/app/data/backups ./
```

Run that from a cron job on the host if you want copies elsewhere. Backups made on demand follow the
same retention count as the automatic ones.

## Restore

```bash
docker compose stop app
docker compose run --rm -v "$PWD/backups":/restore:ro app \
  python -m app restore /restore/monitor-20261118-182247.db
docker compose up -d
```

The restore command checks the file's integrity, refuses files that are not a backup of this
application, replaces the database, and upgrades an older backup to the current schema.

## LAN access

By default only the Docker host can reach the dashboard. To reach it from other devices on a
**trusted** home network, bind it to the server's LAN address:

```ini
# .env
BIND_ADDRESS=192.168.1.50
```

```bash
docker compose up -d
```

Then open `http://192.168.1.50:8080`.

- Prefer the specific LAN address over `0.0.0.0`.
- Never forward this port from your router to the internet.
- Traffic is plain HTTP. On a network you do not fully trust, put a TLS reverse proxy in front and set
  `SESSION_COOKIE_SECURE=true`.
- If the host runs a firewall, allow the port for your LAN only, for example
  `sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp`.
- Docker publishes ports with its own firewall rules. A port bound to `0.0.0.0` can be reachable even
  when `ufw` appears to block it. Binding to a specific address avoids that surprise.

## Bind mount instead of a named volume

```yaml
# compose.override.yaml
services:
  app:
    volumes:
      - ./data:/app/data
```

```bash
mkdir -p data && sudo chown 10001:10001 data
docker compose up -d
```

## Container hardening in `compose.yaml`

| Setting | Effect |
|---|---|
| `read_only: true` | The root filesystem cannot be modified. Only the volume and `/tmp` are writable. |
| `tmpfs: /tmp:size=64m` | Small in-memory scratch space. |
| `cap_drop: [ALL]` | No Linux capabilities. |
| `security_opt: no-new-privileges` | Processes cannot gain privileges. |
| `mem_limit: 512m`, `pids_limit: 256` | Resource limits. |
| `init: true` | Reaps child processes and forwards signals. |
| `stop_grace_period: 30s` | Time for running checks to finish on stop. |

Not used, on purpose: host networking, privileged mode, the Docker socket.

## Health check

The container health check requests `/api/v1/health` every 30 seconds. It reports healthy when the
database answers, the data directory is writable and the scheduler thread is alive.
It never contacts an external service, so a provider outage does not make the container unhealthy.
`/api/v1/health/live` only confirms that the process serves requests.

## Shutdown

On `docker stop`, the scheduler stops taking new work, running checks finish, and the database
connections are closed. The log ends with `application shutdown complete`. The exit code is 143,
because the web server re-raises the termination signal after it has shut down cleanly.

## Why one worker

The scheduler runs as a thread inside the single application process. The entry point starts exactly
one worker. Do not add workers or run a second container on the same volume: each process would run
its own scheduler and its own in-process budget lock. See [ARCHITECTURE.md](ARCHITECTURE.md).
