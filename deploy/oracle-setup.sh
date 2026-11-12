#!/usr/bin/env bash
# One-command setup of ClipForge AI on an Oracle Cloud "Always Free" Ubuntu server (Ampere A1, arm64).
# Also works on any Ubuntu 22.04/24.04 server.
#
#   curl -fsSLO https://raw.githubusercontent.com/sizwendlovu01/Free-Clipper/main/deploy/oracle-setup.sh
#   bash oracle-setup.sh
#
# Options:
#   --domain clips.example.com   use your own domain (point its DNS A record at this server first)
#   --no-ollama                  skip the free local AI model (uses the built-in highlight finder)
# Re-running the script updates the app to the latest version and keeps your data and settings.
set -euo pipefail

REPO_URL="https://github.com/sizwendlovu01/Free-Clipper.git"
APP_DIR="${APP_DIR:-$HOME/Free-Clipper}"
OLLAMA_MODEL="qwen2.5:3b"
DOMAIN=""
WITH_OLLAMA=1

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --no-ollama) WITH_OLLAMA=0; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (e.g. ubuntu), not root. It uses sudo where needed." >&2
  exit 1
fi

say "Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl openssl iptables-persistent >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker (official install script from get.docker.com)"
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker >/dev/null

say "Opening ports 80 and 443 in the server firewall"
# Oracle's Ubuntu images ship iptables rules that reject everything except SSH.
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 1 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  fi
done
sudo netfilter-persistent save >/dev/null 2>&1 || sudo sh -c 'iptables-save > /etc/iptables/rules.v4'

say "Fetching ClipForge AI"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

PUBLIC_IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
if [ -z "$DOMAIN" ]; then
  if [ -z "$PUBLIC_IP" ]; then
    echo "Could not detect the public IP. Re-run with --domain your.domain" >&2
    exit 1
  fi
  # Free hostname that resolves to this IP, so HTTPS works without buying a domain.
  DOMAIN="${PUBLIC_IP//./-}.sslip.io"
fi

if [ ! -f .env ]; then
  say "Creating settings (.env) with fresh secrets"
  SETUP_TOKEN="$(openssl rand -hex 8)"
  cat > .env <<EOF
DOMAIN=$DOMAIN
FC_SECRET_KEY=$(openssl rand -hex 32)
FC_SETUP_TOKEN=$SETUP_TOKEN
FC_HTTPS_ONLY_COOKIES=true
FC_AI_PROVIDER=auto
FC_OLLAMA_URL=http://ollama:11434
FC_OLLAMA_MODEL=$OLLAMA_MODEL
FC_MAX_CONCURRENT_JOBS=1
FC_RENDER_WORKERS=2
FC_WHISPER_MODEL=small
# Optional: Claude picks better highlights and titles. Paste a key here, then re-run the script.
ANTHROPIC_API_KEY=
EOF
  chmod 600 .env
else
  SETUP_TOKEN="$(grep -E '^FC_SETUP_TOKEN=' .env | cut -d= -f2- || true)"
  sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" .env
fi

PROFILES=(--profile https)
[ "$WITH_OLLAMA" -eq 1 ] && PROFILES+=(--profile ollama)

say "Building and starting (first run takes 5-15 minutes)"
sudo docker compose "${PROFILES[@]}" up -d --build

if [ "$WITH_OLLAMA" -eq 1 ]; then
  say "Downloading the free local AI model ($OLLAMA_MODEL, about 2 GB)"
  for _ in $(seq 1 30); do
    sudo docker compose exec -T ollama ollama list >/dev/null 2>&1 && break
    sleep 2
  done
  sudo docker compose exec -T ollama ollama pull "$OLLAMA_MODEL"
fi

say "Waiting for the app to report healthy"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 3
done
curl -fsS --max-time 3 http://127.0.0.1:8000/healthz >/dev/null || {
  echo "The app did not start. See: sudo docker compose logs freeclipper" >&2
  exit 1
}

cat <<EOF

========================================================================
 ClipForge AI is running.

   Open:        https://$DOMAIN
   Setup code:  ${SETUP_TOKEN:-"(see FC_SETUP_TOKEN in $APP_DIR/.env)"}

 Create your admin account there using the setup code.
 The HTTPS certificate can take a minute to issue on first visit.
 If the page doesn't load, open ports 80 and 443 in Oracle's
 Security List (see README, "Oracle Cloud Always Free").

 Update later:  re-run this script
 Logs:          cd $APP_DIR && sudo docker compose logs -f freeclipper
========================================================================
EOF
