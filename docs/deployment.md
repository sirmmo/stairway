# Deployment

## 1. Install

```bash
git clone https://github.com/sirmmo/stairway.git /srv/stairway
cd /srv/stairway && cp .env.example .env && chmod 600 .env
```

Set `STAIRWAY_TOKEN` to a long random value (`python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`),
and set `STAIRWAY_PUBLIC_URL` to the HTTPS URL you will expose.

Make sure the user that runs stairway can run `claude`, and that `claude` is logged in to a claude.ai account
with a subscription. Remote control needs one. Test it with `claude remote-control --help`.

## 2. systemd

`stairway.service` assumes `/srv/stairway`, root, and `claude` in `/root/.local/bin`. Adjust it if needed, then:

```bash
cp stairway.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now stairway
journalctl -u stairway -f
```

The unit uses `KillMode=process`, so `systemctl restart stairway` leaves the experiments' screens running.

## 3. HTTPS

claude.ai connectors need HTTPS. Keep stairway on a private address and put a proxy in front. The proxy must
pass websockets through (for VS Code) and allow long-lived connections.

### nginx

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

server {
  listen 443 ssl http2;
  server_name stairway.example.org;
  # ssl_certificate ...; ssl_certificate_key ...;
  client_max_body_size 2g;

  location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header X-Forwarded-Proto https;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 1h;
  }
}
```

`X-Forwarded-Proto: https` matters: stairway uses it to build OAuth URLs (unless `STAIRWAY_PUBLIC_URL` is set)
and to mark the login cookie `Secure`.

### Traefik on Docker Swarm

If Traefik only watches Docker, a small nginx service can forward to stairway on the host. See
[`examples/traefik-swarm`](../examples/traefik-swarm):

- Bind stairway to the node's `docker_gwbridge` gateway, usually `STAIRWAY_HOST=172.18.0.1`. Containers on
  overlay networks can reach that address, but it isn't exposed on the public interface.
- Pin the nginx service to the node that runs stairway (`placement.constraints`). On other nodes that gateway
  address is a different machine.
- Swarm configs are immutable, so bump the config name (`…_v2`) whenever `nginx.conf` changes.

### Tunnels

Cloudflare Tunnel, Tailscale Funnel and similar tools work too. Point them at `http://127.0.0.1:8765`.

## 4. code-server (optional)

stairway runs `code-server --auth none --socket …` per experiment and puts it behind its own login. Any 4.x
version works. Install the standalone release and set `STAIRWAY_CODE_SERVER`, or put `code-server` on PATH.

Note: code-server 4.17 and later need glibc 2.28 or newer. On older distributions such as Ubuntu 18.04, use
4.16.1.

## 5. Connect Claude

The status page has a **Connect Claude** section with ready-to-copy settings:

- **claude.ai:** Settings → Connectors → Add custom connector, with the URL `https://<host>/mcp`
- **Claude Code CLI:** `claude mcp add --transport http stairway https://<host>/mcp`, then run `/mcp` to log in
- **Clients without OAuth:** `https://<host>/mcp/<STAIRWAY_TOKEN>`

## Upgrading

```bash
cd /srv/stairway && git pull && systemctl restart stairway
```

Running sessions keep their old settings, such as hooks, until you restart them.
