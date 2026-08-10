# Matches the image definition used in the local development environment so
# the deployed database is the same build the team tests against.
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       postgresql-16-postgis-3 \
       postgresql-16-postgis-3-scripts \
       postgresql-16-pgrouting \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
