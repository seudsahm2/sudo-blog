# Deploy Sudo Blog to Render (Production)

## 1) Create services

1. Push this repository to GitHub.
2. In Render, create:
- a `Web Service` from this repo.
- a `PostgreSQL` instance.
- optional `Redis` instance (recommended for Celery/cache).

You can use `render.yaml` (Blueprint deploy) for one-click setup.

## 2) Set environment variables

Required minimum:
- `SECRET_KEY`
- `DEBUG=False`
- `FORCE_HTTPS=True`
- `USE_POSTGRES=True`
- `DATABASE_URL` (from Render PostgreSQL)
- `DB_SSLMODE=require`
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`

Use `.env.render.example` as your reference.

## 3) Build and start commands

If not using Blueprint:
- Build Command:
  `pip install -r requirements.txt && python manage.py collectstatic --noinput`
- Start Command:
  `python manage.py migrate --noinput; gunicorn sudo_blog.wsgi:application --bind 0.0.0.0:$PORT --workers 3 --threads 2 --timeout 120 --access-logfile - --error-logfile -`

## 4) Domain and HTTPS

1. Attach your custom domain in Render.
2. Keep `FORCE_HTTPS=True`.
3. Add domain(s) to:
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS` (must include `https://`)

## 5) Security checklist

- Never commit `.env`.
- Rotate API keys before production if they were ever exposed.
- Keep `DEBUG=False` in production.
- Use strong `SECRET_KEY`.
- Keep `DB_SSLMODE=require`.
- Enable HSTS only after confirming HTTPS domain works correctly.

## 6) Performance checklist

- Keep static assets served by WhiteNoise (`collectstatic` required).
- Set `DB_CONN_MAX_AGE=120` (or tune higher based on load).
- Use Redis (`REDIS_URL`) for cache and Celery workload.
- Scale Gunicorn workers with CPU/RAM limits.

## 7) Post-deploy verification

Run these in Render Shell:

```bash
python manage.py check --deploy
python manage.py showmigrations
```

Open these URLs and verify 200 responses:
- `/blog/`
- `/sitemap.xml`
- `/robots.txt`
- `/admin/`

## 8) Optional background worker (Celery)

If you use async jobs heavily, create a second Render worker service:

Start command:

```bash
celery -A sudo_blog worker -l info
```

And set:
- `CELERY_BROKER_URL` (Redis URL)
- `CELERY_RESULT_BACKEND` (Redis URL)

## 9) No-shell and no-worker mode (free-tier friendly)

If Render shell and background worker are unavailable, you can still run operations manually from the admin analytics page.

1. Keep only the web service.
2. Set `CELERY_TASK_ALWAYS_EAGER=True`.
3. Log in as staff and open `/blog/analytics/`.
4. Use **Manual Pipeline Run** to execute:
- Fetch only
- Summarize only
- Publish only
- Full pipeline

This runs tasks synchronously in the web process, so use moderate limits per run.
