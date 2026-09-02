# HTTPS reverse proxy

The server binds `127.0.0.1:8730` only; TLS is terminated by a reverse proxy. Examples below use `ask.example.com` (add a DNS A record → your server's public IP first).

## Option A: caddy (recommended — automatic certificates)

Append to `/etc/caddy/Caddyfile`:

```caddy
ask.example.com {
    reverse_proxy 127.0.0.1:8730
}
```

```bash
sudo systemctl reload caddy
```

Install caddy if you don't have it:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

## Option B: nginx + certbot

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo tee /etc/nginx/sites-available/askmate > /dev/null <<'EOF'
server {
    listen 80;
    server_name ask.example.com;
    client_max_body_size 10m;
    location / {
        proxy_pass http://127.0.0.1:8730;
        proxy_set_header Host $host;
        proxy_read_timeout 120s;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/askmate /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ask.example.com
```

## Verify

```bash
curl -s https://ask.example.com/api/health
# {"code":200,...,"status":"ok"}
```
