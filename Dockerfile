FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bot \
 && mkdir -p /srv/data && chown bot:bot /srv/data

COPY app ./app
COPY admin_ui ./admin_ui

USER bot
ENV HOST=0.0.0.0 PORT=8090 DATA_DIR=/srv/data
EXPOSE 8090
CMD ["python", "-m", "app.main"]
