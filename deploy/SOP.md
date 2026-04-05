# Server Update SOP

## Stack
- **API server**: `feed-builder.service` → `uvicorn app.main:app` on `127.0.0.1:8000`
- **Worker**: `feed-builder-worker.service` → `uvicorn app.worker.app:app` on `127.0.0.1:8001`
- **Reverse proxy**: Caddy → `api.sknitterinstance.com`
- **Database**: Neon PostgreSQL (connection via `DATABASE_URL` in `.env`)
- **Frontend**: Vercel (separate, no action needed for backend deploys)

---

## Standard deploy (code changes only)

```bash
ssh root@<your-vps-ip>
cd /srv/feed-builder
git pull
source venv/bin/activate
pip install -r requirements.txt   # only needed if requirements.txt changed
systemctl restart feed-builder feed-builder-worker
systemctl status feed-builder feed-builder-worker --no-pager
```

Verify:
```bash
curl -s https://api.sknitterinstance.com/health
```

---

## If you added a new dependency

The `pip install -r requirements.txt` step above covers this. Always pin the version in `requirements.txt` locally before pushing:

```bash
# local
pip freeze | grep <package-name>   # get the installed version
# add it to requirements.txt, then commit and push
```

---

## If you changed the DB schema (new columns/tables)

`create_tables()` on startup creates new tables automatically but does **not** add columns to existing tables. For new columns you must run ALTER TABLE manually.

1. Go to [Neon console](https://console.neon.tech) → your database → SQL Editor
2. Run:
   ```sql
   ALTER TABLE <table> ADD COLUMN IF NOT EXISTS <column> <type>;
   ```
3. Restart the API server:
   ```bash
   systemctl restart feed-builder
   ```

---

## If you changed a systemd service file

Service files live in `deploy/` in the repo. After `git pull`:

```bash
cp /srv/feed-builder/deploy/feed-builder.service /etc/systemd/system/feed-builder.service
cp /srv/feed-builder/deploy/feed-builder-worker.service /etc/systemd/system/feed-builder-worker.service
systemctl daemon-reload
systemctl restart feed-builder feed-builder-worker
```

---

## If you changed the .env

Edit directly on the server (not in the repo — `.env` is gitignored):

```bash
nano /srv/feed-builder/.env
systemctl restart feed-builder feed-builder-worker
```

---

## Checking logs

```bash
# Live logs
journalctl -u feed-builder -f
journalctl -u feed-builder-worker -f

# Last 50 lines
journalctl -u feed-builder -n 50 --no-pager
journalctl -u feed-builder-worker -n 50 --no-pager
```

---

## If a service won't start

```bash
journalctl -u feed-builder -n 50 --no-pager -l
# fix the issue, then:
systemctl restart feed-builder
```

Common causes:
- Missing dependency → `pip install -r requirements.txt`
- Bad `.env` value → `nano /srv/feed-builder/.env`
- DB schema mismatch → run ALTER TABLE in Neon console
