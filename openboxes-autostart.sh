#!/usr/bin/env bash
# Start OpenBoxes automatically on Fedora (rootless podman + systemd user service).
# Keep this script next to compose.yaml and run it as the regular user that will own the containers.
#
#   ./openboxes-autostart.sh             # install: enable the service at boot, start now, apply seed.sql
#   ./openboxes-autostart.sh seed        # (re)apply seed.sql; safe to repeat
#   ./openboxes-autostart.sh uninstall   # disable and remove the service (containers are stopped, data is kept)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=openboxes.service
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

new_secret() { od -An -tx1 -N16 /dev/urandom | tr -d ' \n'; }

new_env_file() {
    [ -f "$DIR/.env" ] && return
    cat > "$DIR/.env" <<EOF
OB_VERSION=v0.9.8-hotfix1
OB_PORT=8081
# To serve the office LAN: OB_BIND=0.0.0.0 and OB_HOST=<this machine's hostname or IP>
#OB_BIND=0.0.0.0
#OB_HOST=localhost
DB_PASSWORD=$(new_secret)
DB_ROOT_PASSWORD=$(new_secret)
EOF
    chmod 600 "$DIR/.env"
    echo "Created .env with new database passwords."
}

seed() {
    if [ ! -f "$DIR/seed.sql" ]; then
        echo "No seed.sql next to this script; nothing to seed."
        return 0
    fi
    echo "Waiting for OpenBoxes to finish booting (the first boot can take several minutes)..."
    local health="" i
    for i in $(seq 1 180); do
        health="$(podman inspect -f '{{.State.Health.Status}}' openboxes-app 2>/dev/null || true)"
        [ "$health" = "healthy" ] && break
        sleep 10
    done
    if [ "$health" != "healthy" ]; then
        echo "openboxes-app did not become healthy; seed skipped." >&2
        return 1
    fi
    podman cp "$DIR/seed.sql" openboxes-db:/tmp/seed.sql
    podman exec openboxes-db sh -c 'MYSQL_PWD=$MARIADB_PASSWORD mariadb -uopenboxes openboxes < /tmp/seed.sql'
    # the app caches locations, categories and products, so restart it to pick up the new rows
    podman restart openboxes-app >/dev/null
    echo "Seed applied; OpenBoxes is restarting."
}

install() {
    if [ "$(id -u)" -eq 0 ]; then
        echo "Run this as a regular user, not root (sudo is used where needed)." >&2
        exit 1
    fi

    local missing=()
    command -v podman >/dev/null || missing+=(podman)
    command -v podman-compose >/dev/null || missing+=(podman-compose)
    if [ ${#missing[@]} -gt 0 ]; then
        sudo dnf install -y "${missing[@]}"
    fi
    new_env_file

    local podman
    podman="$(command -v podman)"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=OpenBoxes (podman compose)

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$DIR
ExecStart=$podman compose up -d
ExecStop=$podman compose stop
TimeoutStartSec=1800
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
EOF

    # linger starts the user's services at boot, without anyone logging in
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"

    systemctl --user daemon-reload
    echo "Starting OpenBoxes (the first run pulls ~1 GB of images)..."
    systemctl --user enable --now "$UNIT"
    seed

    local port bind
    port="$(sed -n 's/^OB_PORT=//p' "$DIR/.env" | tail -n1)"
    bind="$(sed -n 's/^OB_BIND=//p' "$DIR/.env" | tail -n1)"
    port="${port:-8081}"
    echo "OpenBoxes is starting: http://localhost:$port/openboxes/ (allow a few minutes on first boot)"
    if [ -n "$bind" ] && [ "$bind" != "127.0.0.1" ] && command -v firewall-cmd >/dev/null; then
        echo "To allow other machines in: sudo firewall-cmd --permanent --add-port=$port/tcp && sudo firewall-cmd --reload"
    fi
    echo "Logs: journalctl --user -u $UNIT   |   podman logs -f openboxes-app"
}

uninstall() {
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload
    echo "Removed $UNIT. Data volumes are kept; linger was left enabled (loginctl disable-linger $USER to undo)."
}

case "${1:-install}" in
    install) install ;;
    uninstall) uninstall ;;
    seed) seed ;;
    *) echo "usage: $0 [install|uninstall|seed]" >&2; exit 2 ;;
esac
