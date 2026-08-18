FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY miami_bot/ ./miami_bot/
COPY main.py config.yaml ./

# The SQLite database and logs live on a volume; the container stays stateless.
RUN mkdir -p /app/data && \
    useradd --create-home --uid 1000 monitor && \
    chown -R monitor:monitor /app
USER monitor

VOLUME ["/app/data"]

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD python main.py check > /dev/null || exit 1

ENTRYPOINT ["python", "main.py"]
CMD ["watch"]
