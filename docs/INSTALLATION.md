# Installation

This guide takes a fresh Ubuntu server with Docker installed to a running dashboard.

## Requirements

- Docker Engine 24 or newer with the Compose plugin (`docker compose version` prints a version).
- About 400 MB of disk space for the image and a few megabytes for data.
- Outbound HTTPS access. No inbound access from the internet is needed.

## 1. Get the code

```bash
git clone https://github.com/fm407/amazon-price-monitor.git
cd amazon-price-monitor
```

## 2. Create the configuration

```bash
cp .env.example .env
chmod 600 .env
```

The example file starts the application in mock mode with demo data. No API key is needed.
See [CONFIGURATION.md](CONFIGURATION.md) for every option.

## 3. Start

```bash
docker compose up -d --build
docker compose ps
```

Wait until the `STATUS` column shows `healthy`. The first build takes one to two minutes.

## 4. Create the administrator account

The application ships without any account or default password.
On first start it creates a one-time setup token and prints it once in the log.

```bash
docker compose logs app | grep setup_token
```

Open the dashboard and enter the token, a username and a password (12 characters or more).
The token is deleted as soon as the account exists.

## 5. Open the dashboard

The dashboard listens on `127.0.0.1:8080` of the Docker host only.

- On the server itself: <http://127.0.0.1:8080>
- From your laptop, through an SSH tunnel:
  ```bash
  ssh -L 8080:127.0.0.1:8080 you@your-server
  ```
  Then open <http://127.0.0.1:8080> on the laptop.
- From other devices on a trusted home network: see "LAN access" in [DOCKER.md](DOCKER.md).

## 6. Leave mock mode

1. Configure a provider: [API_PROVIDERS.md](API_PROVIDERS.md).
2. Configure notifications: [DISCORD_SETUP.md](DISCORD_SETUP.md).
3. Set `DEMO_MODE=false`, then run `docker compose up -d`. The demo products are removed at start.

## Updating

```bash
git pull
docker compose up -d --build
```

Database migrations run automatically at start. Your data stays in the Docker volume.

## Uninstalling

```bash
docker compose down        # keeps the data volume
docker compose down -v     # also deletes all data
```
