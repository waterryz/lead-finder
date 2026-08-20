FROM python:3.12-slim

WORKDIR /app

# System deps for lxml / Playwright are pulled in by `playwright install --with-deps`.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

# Persist the SQLite DB on a mounted volume in production (Railway: add a Volume at /data).
ENV DB_PATH=/data/companies.db
ENV PORT=8000
VOLUME ["/data"]

EXPOSE 8000
# Runs the web dashboard; set RUN_BOT=1 to also launch the Telegram bot in the same container.
CMD ["sh", "/app/start.sh"]
