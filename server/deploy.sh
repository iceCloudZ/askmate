#!/usr/bin/env bash
# askmate self-hosted deploy: probe env -> upload server.py -> systemd -> health check
# Usage: ./deploy.sh <ssh-host-alias> [ssh-user]
set -euo pipefail

HOST_ALIAS="${1:?Usage: ./deploy.sh <ssh-host-alias> [ssh-user]}"
REMOTE_USER="${2:-$(whoami)}"
REMOTE_DIR="/home/${REMOTE_USER}/askmate"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "══ 1/4 Probing $HOST_ALIAS ══"
ssh -o ConnectTimeout=8 "$REMOTE_USER@$HOST_ALIAS" '
echo "host:   $(uname -m) $(grep PRETTY /etc/os-release | cut -d\" -f2)"
echo "python: $(python3 --version 2>&1)"
echo "caddy:  $(systemctl is-active caddy 2>/dev/null || echo none)"
echo "nginx:  $(systemctl is-active nginx 2>/dev/null || echo none)"
uptime
free -h | head -2 | tail -1
'

echo
echo "══ 2/4 Uploading server.py + systemd unit ══"
ssh "$REMOTE_USER@$HOST_ALIAS" "mkdir -p $REMOTE_DIR"
scp -q "$HERE/server.py" "$REMOTE_USER@$HOST_ALIAS:$REMOTE_DIR/server.py"
ssh "$REMOTE_USER@$HOST_ALIAS" "sudo tee /etc/systemd/system/askmate.service > /dev/null" \
  < "$HERE/askme.service"
ssh "$REMOTE_USER@$HOST_ALIAS" "sudo sed -i \"s/^User=.*/User=$REMOTE_USER/; s|WorkingDirectory=.*|WorkingDirectory=$REMOTE_DIR|; s|ExecStart=.*|ExecStart=$(command -v python3 || echo /usr/bin/python3) $REMOTE_DIR/server.py serve|\" /etc/systemd/system/askmate.service \
  && sudo systemctl daemon-reload && sudo systemctl enable --now askmate && sleep 1 && systemctl is-active askmate"

echo
echo "══ 3/4 Health check (loopback) ══"
ssh "$REMOTE_USER@$HOST_ALIAS" "curl -s http://127.0.0.1:8730/api/health && echo"

echo
echo "══ 4/4 Manual follow-ups ══"
cat <<'EOF'
[1] Create the two accounts on the server:
      ssh <host> 'python3 ~/askmate/server.py adduser <username>'
[2] DNS: point your domain (e.g. ask.example.com) A record at this server's public IP
[3] TLS reverse proxy: see reverse-proxy.md (caddy = 2 lines; or nginx + certbot)
[4] Verify: curl -s https://ask.example.com/api/health
[5] Both sides: askmate login --user <name> --password <pw>  (or use the GitHub backend and skip all of this)
EOF
echo "Done."
