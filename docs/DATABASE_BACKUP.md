# Database Backup & Recovery

## What to Back Up

| Asset | Location | Criticality |
|---|---|---|
| PostgreSQL database | `pg_dump` | **Critical** — all task, memory, and learning state |
| `WALLET_VAULT_MASTER_KEY` | `.env` or secrets manager | **Critical** — without it, wallet secrets are unrecoverable |
| Browser sessions | `./data/browser_sessions` | High — avoids re-authentication |
| Evidence and screenshots | `./data/` | Medium |
| `.env` | Encrypted | **Critical** — contains all credentials |

> **The master key must be backed up separately from the database.** A database dump without the
> master key contains ciphertext that cannot be decrypted. This is by design; there is no escrow
> and no backdoor.

## Backup

```bash
# Database (custom format, compressed)
pg_dump -Fc -U airdropos airdropos_db > airdropos-$(date +%F).dump

# Data directory
tar czf airdropos-data-$(date +%F).tar.gz /opt/airdropos/data/

# Encrypt before storing offsite
gpg --symmetric --cipher-algo AES256 airdropos-$(date +%F).dump
```

### Automated (cron)

```cron
0 3 * * * pg_dump -Fc -U airdropos airdropos_db > /backup/airdrops-$(date +\%F).dump
30 3 * * 0 find /backup -name 'airdropos-*.dump' -mtime +30 -delete
```

## Restore

```bash
sudo systemctl stop airdropos-backend airdropos-worker

createdb airdropos_db_restored
pg_restore -d airdropos_db_restored --clean --if-exists airdropos-2026-01-15.dump

psql -c "ALTER DATABASE airdropos_db RENAME TO airdropos_db_old;"
psql -c "ALTER DATABASE airdropos_db_restored RENAME TO airdropos_db;"

sudo systemctl start airdropos-backend airdropos-worker
```

Verify:

```bash
curl localhost:8000/api/v1/readiness
psql -U airdropos -d airdropos_db -c "SELECT count(*) FROM tasks;"
```

## Schema Migrations

Alembic is the authoritative path for schema changes:

```bash
cd backend
alembic current            # applied revision
alembic history            # full history
alembic upgrade head       # apply
alembic downgrade -1       # revert one
```

Always back up before a migration that is not purely additive.

## Point-in-Time Recovery

For stronger guarantees, configure PostgreSQL WAL archiving:

```
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /backup/wal/%f && cp %p /backup/wal/%f'
```

This allows recovery to any point in time, which matters when a task state or memory record
needs to be reconstructed exactly.

## Retention

Suggested policy:

| Data | Retention |
|---|---|
| Task executions and events | 90 days |
| Checkpoints | 30 days after task completion |
| Screenshots and evidence | 180 days |
| Learning records | Indefinite (small) |
| Notification history | 60 days |
| Deadlines and campaign records | Indefinite |

Prune with SQL rather than application code so retention never interferes with task execution.
