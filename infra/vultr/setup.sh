#!/usr/bin/env bash
# Manthan — Vultr VM bootstrap
#
# Idempotent installer for a fresh Ubuntu 22.04 / 24.04 Vultr Cloud Compute
# instance. Installs Docker + compose, clones the repo, writes a placeholder
# .env, and runs `docker compose up -d`. The same body is embedded in
# cloud-init.yaml for one-click provisioning.
#
# Manual usage:
#   curl -fsSL https://raw.githubusercontent.com/Miny-Labs/Manthan/main/infra/vultr/setup.sh | bash

set -euo pipefail

REPO_URL="${MANTHAN_REPO_URL:-https://github.com/Miny-Labs/Manthan.git}"
INSTALL_DIR="${MANTHAN_INSTALL_DIR:-/opt/manthan}"
BRANCH="${MANTHAN_BRANCH:-main}"

echo "→ Manthan / Vultr VM bootstrap"
echo "  repo:    $REPO_URL"
echo "  branch:  $BRANCH"
echo "  install: $INSTALL_DIR"
echo

# 1. base packages
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl git gnupg lsb-release ufw

# 2. Docker (official convenience script — pinned, idempotent)
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# 3. clone (or update) the repo
if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "→ updating existing checkout at $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
else
  echo "→ cloning into $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# 4. placeholder .env if absent (so judges/operator can fill it in once)
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  echo "→ wrote placeholder .env — edit it before the next restart"
fi

# 5. minimal firewall — allow web traffic + SSH only
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow ssh >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
# FastAPI direct port — keep it open while there's no TLS terminator yet
ufw allow 8000/tcp >/dev/null
ufw --force enable >/dev/null

# 6. up the stack
cd "$INSTALL_DIR"
docker compose up -d --build

echo
echo "✓ Manthan is up at http://$(curl -fsSL https://ifconfig.me)/"
echo "  next: edit $INSTALL_DIR/.env to add your VULTR_API_KEY, then"
echo "       docker compose -f $INSTALL_DIR/docker-compose.yml restart"
