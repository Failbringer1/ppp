#!/usr/bin/env bash
# One-command install / update of TGAds on a fresh Ubuntu/Debian VPS:
#   curl -fsSL https://raw.githubusercontent.com/Failbringer1/ppp/claude/telegram-channel-ads-platform-8qn7b8/tgads/deploy/install.sh | sudo bash
# Re-running it updates the code and keeps data/ and .env.
set -euo pipefail

REPO="${REPO:-https://github.com/Failbringer1/ppp.git}"
BRANCH="${BRANCH:-claude/telegram-channel-ads-platform-8qn7b8}"
DIR="${DIR:-/opt/tgads}"

if [ "$(id -u)" != "0" ]; then echo "Запустите от root: sudo bash install.sh"; exit 1; fi

echo "==> Пакеты"
apt-get update -y -qq
apt-get install -y -qq git curl ca-certificates openssl >/dev/null
if ! command -v docker >/dev/null; then
  echo "==> Docker"
  curl -fsSL https://get.docker.com | sh
fi

echo "==> Код"
git ls-remote --exit-code --heads "$REPO" "$BRANCH" >/dev/null 2>&1 || BRANCH=main
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$DIR" checkout -q -B "$BRANCH" FETCH_HEAD
else
  git clone -q --depth 1 -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR/tgads"

if [ ! -f .env ]; then
  echo "==> Настройка (один раз)"
  read -rp "Telegram api_id: " API_ID </dev/tty
  read -rp "Telegram api_hash: " API_HASH </dev/tty
  read -rp "Домен для панели (Enter — без домена, адрес по IP): " DOMAIN </dev/tty
  if [ -z "$DOMAIN" ]; then
    IP="$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')"
    DOMAIN="$(echo "$IP" | tr . -).sslip.io"
  fi
  PASS="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)"
  cat > .env <<ENV
TGADS_TG_API_ID=$API_ID
TGADS_TG_API_HASH=$API_HASH
TGADS_PANEL_USER=admin
TGADS_PANEL_PASSWORD=$PASS
TGADS_AI_MODE=auto
PUBLIC_HOST=$DOMAIN
ENV
  chmod 600 .env
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

echo "==> Сборка и запуск (первый раз 5–10 минут)"
docker compose -f docker-compose.yml -f deploy/docker-compose.vps.yml up -d --build

set -a; . ./.env; set +a
cat <<DONE

Готово.
  Панель:  https://$PUBLIC_HOST
  Логин:   $TGADS_PANEL_USER
  Пароль:  $TGADS_PANEL_PASSWORD   (хранится в $DIR/tgads/.env)

Чтобы ИИ работал без API-ключа, один раз войдите в Claude:
  cd $DIR/tgads && docker compose exec -it tgads claude
  (в Claude Code наберите /login, откройте ссылку, вставьте код, затем /exit)

Логи:      cd $DIR/tgads && docker compose logs -f tgads
Обновить:  ещё раз запустите эту же команду установки
DONE
