# Official slim Python image.
FROM python:3.11-slim

# No .pyc files, and unbuffered output so logs appear as they are written.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System dependencies:
# - tini: init process, so signals reach the app and zombies are reaped
# - ca-certificates / tzdata: HTTPS trust store and time zone data
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies are copied and installed before the source, so editing the
# source does not invalidate the cached install layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# tini as init, so SIGTERM is delivered to the application rather than absorbed.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run the program directly, with no shell wrapper in between.
CMD ["python", "main.py"]
