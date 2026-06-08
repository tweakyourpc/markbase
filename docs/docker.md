# Docker

MarkBase can run fully inside Docker with no manual dependency installation on the host.
The image includes `ffmpeg`, `yt-dlp`, and `markitdown[all]`.

## Start

### Docker Compose

```bash
docker compose up -d
```

### Docker CLI

```bash
docker run -d \
  -p 8733:8733 \
  -v $(pwd)/library:/data/library \
  -v $(pwd)/state:/data/state \
  --name markbase \
  ghcr.io/tweakyourpc/markbase:latest
```

## Change the port

Edit the port mapping in `docker-compose.yml`:

```yaml
ports:
  - "9000:8733"
```

Then MarkBase will be available at `http://localhost:9000` while the container still listens on `8733` internally.

For `docker run`, change the published port:

```bash
docker run -d -p 9000:8733 ...
```

## Use a different library or an existing library

Change the bind mounts to point at a different host directory:

```yaml
volumes:
  - /srv/markbase/library:/data/library
  - /srv/markbase/state:/data/state
```

If you already have a MarkBase library, bind that existing folder to `/data/library` and MarkBase will index and serve it on startup.

## Logs

```bash
docker compose logs -f
```

## Restart

```bash
docker compose restart
```

## Stop

```bash
docker compose down
```

## Notes

- `library` and `state` are mounted as volumes so content and queue state survive rebuilds and container replacement.
- No `portbroker` is used inside the container. Uvicorn binds directly to `0.0.0.0:8733`.
- A clean empty `library` directory works. MarkBase creates its required structure on startup.
