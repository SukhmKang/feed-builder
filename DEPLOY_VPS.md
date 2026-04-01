# Backend VPS Deployment

This backend is a FastAPI app. The clean production shape is:

- frontend on Vercel/Netlify
- backend on your VPS
- database on managed Postgres
- reverse proxy with Nginx
- process manager with `systemd`

## 1. Recommended architecture

- VPS runs the Python backend only
- Managed Postgres stores `feeds`, `articles`, and `push_subscriptions`
- `FRONTEND_URL` is set to your deployed frontend origin
- Nginx terminates HTTPS and proxies to Uvicorn on `127.0.0.1:8000`

## 2. External DB recommendation

Use managed Postgres instead of running Postgres on the VPS.

Good fit for this app:

- Neon: simple serverless Postgres, good for small projects and side projects
- Supabase Postgres: solid managed Postgres with dashboard and backups
- Railway Postgres / Render Postgres: easy app-platform integration if you already use them

For this codebase, managed Postgres is the best default because:

- you avoid backups and DB maintenance on the VPS
- the app already uses SQLAlchemy, so swapping from SQLite to Postgres is easy
- VPS failures do not take the database down with them

Set:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
```

If your provider gives `postgres://...`, the app now normalizes that automatically.

## 3. VPS setup

Assumes Ubuntu 24.04 or similar.

Install packages:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
```

Create app directory and copy code:

```bash
sudo mkdir -p /srv/feed_builder
sudo chown $USER:$USER /srv/feed_builder
```

On the VPS, create the virtualenv and install deps:

```bash
cd /srv/feed_builder
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn sqlalchemy apscheduler python-dotenv beautifulsoup4 httpx anthropic pywebpush psycopg2-binary
```

Create `.env`:

```env
FRONTEND_URL=https://your-frontend-domain.com
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
YOUTUBE_API_KEY=...
NITTER_BASE=...
PROXY_USERNAME=...
PROXY_PASSWORD=...
```

## 4. Start command

Do not use `--reload` in production.

Use:

```bash
/srv/feed_builder/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 5. systemd service

Create `/etc/systemd/system/feed-builder.service`:

```ini
[Unit]
Description=Feed Builder backend
After=network.target

[Service]
User=YOUR_USER
Group=YOUR_USER
WorkingDirectory=/srv/feed_builder
EnvironmentFile=/srv/feed_builder/.env
ExecStart=/srv/feed_builder/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now feed-builder
sudo systemctl status feed-builder
```

Logs:

```bash
journalctl -u feed-builder -f
```

## 6. Nginx reverse proxy

Example `/etc/nginx/sites-available/feed-builder`:

```nginx
server {
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable it:

```bash
sudo ln -s /etc/nginx/sites-available/feed-builder /etc/nginx/sites-enabled/feed-builder
sudo nginx -t
sudo systemctl reload nginx
```

Then add HTTPS with Certbot.

## 7. Important notes for this app

- The app creates tables automatically on startup with `create_tables()`
- There are no migrations yet, so schema changes should be handled carefully
- Scheduler jobs run inside the backend process, so you should run only one backend instance unless you add leader election or move scheduling to a worker
- SQLite is fine locally, but production should use Postgres

## 8. Suggested first production setup

- Frontend: Vercel
- Backend: single small VPS
- DB: managed Postgres
- Domain split:
  - `app.yourdomain.com` for frontend
  - `api.yourdomain.com` for backend

This is the simplest reliable setup for the current codebase.
