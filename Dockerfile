# Official Playwright image: chromium + all system deps preinstalled (matches playwright==1.48.0).
# Avoids the apt/font-package failures of installing Playwright deps on Debian trixie.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist the SQLite DB on a mounted volume in production.
# On Railway: add a Volume with mount path /data (do NOT use the Docker VOLUME instruction — Railway rejects it).
ENV DB_PATH=/data/companies.db
ENV PORT=8000

EXPOSE 8000
# Runs the web dashboard; set RUN_BOT=1 to also launch the Telegram bot in the same container.
CMD ["sh", "/app/start.sh"]
