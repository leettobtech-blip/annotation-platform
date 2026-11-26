FROM python:3.12-slim-bookworm
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home app \
    && mkdir -p /data && chown app:app /data
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY annotation ./annotation
USER 10001
CMD ["uvicorn", "annotation.api:app", "--host", "0.0.0.0", "--port", "3000", "--limit-concurrency", "128"]
