# CityFlow deployment infrastructure

Provisions and operates the CityFlow deployment environment: a single EC2
instance running PostgreSQL, PostGIS and pgRouting in Docker, an S3 bucket
holding the raw open-data CSVs, and a timer that keeps the live pedestrian
data current.

## Architecture

```text
S3  cityflow/raw/            raw open-data CSVs, uploaded by the data owner
 │
 ▼
EC2  Ubuntu 24.04            application host
 ├─ Docker
 │   ├─ db        PostgreSQL 16 + PostGIS + pgRouting, port bound to loopback
 │   └─ api       FastAPI service (added once the backend branch merges)
 ├─ data pipeline in a Python virtual environment
 └─ systemd timer  live ingestion every 15 minutes
```

The database is not published to the internet. Port 22 is the only inbound
route, and the database is reached through an SSH tunnel. Password
authentication is disabled on the instance, so access requires a key that has
been registered on it.

The frontend is hosted separately on Vercel and reaches the API through a
Next.js rewrite, which keeps browser requests same-origin.

## Choice of database host

PostgreSQL runs in a container on the instance rather than on Amazon RDS. RDS
adds roughly USD 15 per month for redundancy the project does not need at this
scale, and the build has to stay live for six months. The trade-off is that
the data lives on the instance's disk, so `scripts/db-backup.sh` should be run
after the historical load and before any teardown.

## Prerequisites

- AWS CLI v2, configured with an IAM user (not the account root) and region
  `ap-southeast-2`
- An EC2 key pair, with the private key at `~/.ssh/cityflow-ec2.pem` and mode
  `400`
- A billing budget alert, set before the first deployment

## Deploying

```bash
./scripts/deploy.sh t3.small
```

`deploy.sh` creates the stack on the first run and applies only the difference
on later runs. Use `t3.small` or larger while the historical pipeline runs;
`t3.micro` is enough for serving afterwards. Use `scripts/resize.sh` to move
between them once the host is in use.

Optional settings:

| Variable | Purpose |
| --- | --- |
| `CITYFLOW_SSH_CIDR` | Restricts port 22 to a given range. Defaults to any address. |
| `CITYFLOW_UPLOADER_ARN` | IAM principal granted upload access to the raw data prefix. |
| `CITYFLOW_STACK` | Stack name, if more than one environment is needed. |

## Bringing up the database

Once the instance has finished its first boot:

```bash
./scripts/connect.sh
```

Then, on the instance:

```bash
git clone <repository-url> ~/cityflow
cd ~/cityflow/infra
cp .env.example compose/.env
sed -i "s/CHANGE_ME/$(openssl rand -hex 24)/g" compose/.env
cd compose && docker compose up -d --build
```

Apply the schema from the repository root, with the pipeline virtual
environment active:

```bash
python database/migrate.py
```

## Scheduling live ingestion

```bash
sudo cp ~/cityflow/infra/scheduler/cityflow-live.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cityflow-live.timer
```

Check it:

```bash
systemctl list-timers cityflow-live.timer
journalctl -u cityflow-live.service -n 50
```

The timer is `Persistent`, so a run missed while the instance was stopped is
executed on the next start rather than skipped.

The service loads `infra/compose/.env` through its systemd `EnvironmentFile`.
The checked-in `infra/.env.example` supplies these retention defaults when it
is copied during setup:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CITYFLOW_LIVE_RETENTION_HOURS` | `24` | Retains minute-level live rows for this many hours, with the cutoff anchored to the latest source timestamp stored in the database. |
| `CITYFLOW_LIVE_QUARANTINE_RETENTION_DAYS` | `7` | Retains quarantined rows for this many days, based on `detected_at`. |
| `CITYFLOW_LIVE_RUN_RETENTION_DAYS` | `30` | Retains completed ingestion audit runs for this many days; referenced runs are kept until their live and quarantine records are removed. |

Cleanup runs only after a successful, non-dry-run live ingestion. A dry run
does not write or delete database records.

## Daily commands

| Command | Effect |
| --- | --- |
| `./scripts/status.sh` | Stack, instance and container state |
| `./scripts/connect.sh` | Shell on the instance |
| `./scripts/tunnel.sh` | Database on `localhost:5432` for the session |
| `./scripts/resize.sh <type>` | Changes the instance size, then waits for the database |
| `./scripts/stop-instance.sh` | Stops compute charges, keeps data and address |
| `./scripts/start-instance.sh` | Brings it back |
| `./scripts/db-backup.sh` | Compressed dump to the bucket |
| `./scripts/destroy.sh` | Deletes the stack; the bucket is retained |

## Connecting from a workstation

Leave `./scripts/tunnel.sh` running, then use the local connection string:

```text
postgresql://cityflow_app:<password>@localhost:5432/cityflow
```

This is the same address as the local development environment, so no tool
needs reconfiguring between the two.

## Granting access to another team member

Each person generates their own key pair and sends the public half only:

```bash
ssh-keygen -t ed25519 -C "<name>@cityflow"
```

Append their `.pub` line to `~/.ssh/authorized_keys` on the instance. Private
keys are never shared, and removing one person's line does not affect anyone
else.

## Cost

Compute is charged only while the instance is running, so `t3.small` can be
used for the initial data load and the host stopped between demonstrations
without affecting the stored data.

The Elastic IP is billed while the instance is stopped. It is kept anyway so
the address stays stable across restarts and does not have to be redistributed
to the team.

## Operational notes

- `docker compose down` is safe; `docker compose down -v` deletes the volume
  and every loaded row with it.
- Terminating the instance destroys its disk. Take a backup first.
- `compose/.env` holds the database password and is gitignored. It is never
  committed, and it does not appear in any template or script.
