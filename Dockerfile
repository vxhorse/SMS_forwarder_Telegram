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

# Named files rather than COPY . ., so the image holds exactly this list and
# nothing the build context happens to contain. The .dockerignore below it is a
# deny-list, and a deny-list has to anticipate every kind of file that must not
# ship; whatever it fails to anticipate is copied in and then published, where
# it cannot be withdrawn - deleting a tag unlinks the name but leaves the layer
# fetchable by digest. This project logs message data by nature, so the file it
# fails to anticipate is likelier than not to be one carrying message data.
# An allow-list can only fail the other way, and a file missing from here stops
# the process on its first import instead of travelling to everyone who pulls.
# LICENSE travels because MIT requires the notice to accompany the software.
COPY main.py logger.py config.py healthcheck.py LICENSE ./
COPY module/ ./module/

# tini as init, so SIGTERM is delivered to the application rather than absorbed.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run the program directly, with no shell wrapper in between.
CMD ["python", "main.py"]
