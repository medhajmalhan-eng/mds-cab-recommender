#!/usr/bin/env bash
# Stand the recommender up on a fresh Ubuntu/Debian host.
#
#   scp -r ~/mds-cab-recommender user@HOST:~/     (or git clone)
#   ssh user@HOST
#   cd mds-cab-recommender && bash bootstrap.sh
#
# Everything is Python stdlib — no pip, no virtualenv, no build step.
set -euo pipefail
cd "$(dirname "$0")"
APP="$(pwd)"
USER_NAME="$(whoami)"

echo "==> app dir: $APP  (user $USER_NAME)"
command -v python3 >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y python3; }
python3 -c 'import sqlite3,urllib.request,csv,json' || { echo "python3 stdlib incomplete"; exit 1; }

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "!! .env created from the template. Fill it in, then re-run this script:"
  echo "     MDS_PASSWORD, APPS_SCRIPT_EXEC_URL, APPS_SCRIPT_TOKEN, METABASE_CARD_ID"
  echo "     nano $APP/.env"
  exit 1
fi
chmod 600 .env
mkdir -p data

# auth on by default: generate a UI password if the field is blank
if ! grep -q '^UI_PASSWORD=..*' .env; then
  PW="$(openssl rand -hex 8 2>/dev/null || head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  grep -q '^UI_PASSWORD=' .env \
    && sed -i "s/^UI_PASSWORD=.*/UI_PASSWORD=$PW/" .env \
    || echo "UI_PASSWORD=$PW" >> .env
  echo "==> generated UI_PASSWORD: $PW   (deployers log in with any username + this)"
fi

echo "==> seeding history (only pulls days that are missing)"
python3 sync.py --ensure

echo "==> installing systemd service"
sudo tee /etc/systemd/system/cabreco.service >/dev/null <<EOF
[Unit]
Description=MDS Cab Recommender
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP
# --ensure fills any gap left by downtime before serving
ExecStartPre=/usr/bin/python3 $APP/sync.py --ensure
ExecStart=/usr/bin/python3 $APP/service.py 8770
Restart=always
RestartSec=10
StandardOutput=append:$APP/data/service.log
StandardError=append:$APP/data/service.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cabreco

echo "==> installing nightly jobs"
( crontab -l 2>/dev/null | grep -v mds-cab-recommender || true
  echo "0 22 * * * cd $APP && /usr/bin/python3 sync.py >> data/sync.log 2>&1"
  echo "30 22 * * * cd $APP && /usr/bin/python3 shadow.py reconcile >> data/shadow.log 2>&1"
) | crontab -

sleep 3
echo
echo "==> status"
systemctl is-active cabreco && curl -s localhost:8770/health && echo
echo
echo "Serving on port 8770. It is NOT reachable from outside yet — pick one:"
echo "  a) cloudflared tunnel --url http://127.0.0.1:8770     (quick, public URL)"
echo "  b) nginx + certbot on your own domain                 (proper)"
echo "  c) open port 8770 in the cloud firewall               (no TLS — avoid)"
echo
echo "  logs:    tail -f $APP/data/service.log"
echo "  restart: sudo systemctl restart cabreco"
