# Running MarkBase with Docker

The Docker setup replaces portbroker with a fixed port bound via compose.
Your host systemd service (run-service.sh) is unaffected.

## Quick start

    docker compose up -d

MarkBase will be available at http://localhost:8000 (or whatever port
you set via MARKBASE_PORT in a .env file beside docker-compose.yml).

## Custom port

Create a .env file next to docker-compose.yml:

    MARKBASE_PORT=8733

Then restart:

    docker compose up -d

## Library data

All content is stored in Docker named volumes:
  - markbase_library: your Markdown + metadata
  - markbase_state: jobs.db SQLite queue

To back up your library:

    docker run --rm \
      -v markbase_markbase_library:/data \
      -v $(pwd):/backup \
      alpine tar czf /backup/markbase-library-backup.tar.gz /data

## Updating

    docker compose pull
    docker compose up -d --build
