#!/bin/sh
# Pasang MbahGPT sebagai layanan systemd (butuh sudo).
#
#   ./install-service.sh            pasang, enable, start
#   ./install-service.sh --uninstall  stop, disable, hapus unit
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
UNIT=mbahgpt.service
DEST=/etc/systemd/system/$UNIT

if [ "${1:-}" = "--uninstall" ]; then
    sudo systemctl disable --now "$UNIT" || true
    sudo rm -f "$DEST"
    sudo systemctl daemon-reload
    echo "$UNIT dilepas."
    exit 0
fi

# Aset UI tidak ada di repo; tanpa ini halaman tidak merender.
[ -f "$HERE/vendor/ionic/ionic.esm.js" ] || "$HERE/fetch-vendor.sh"

# Unit memakai path absolut /home/ubuntu/projects/mbahgpt. Kalau repo dipindah,
# tulis ulang path-nya saat memasang daripada mengedit berkas di repo.
sed -e "s#/home/ubuntu/projects/mbahgpt#$HERE#g" \
    -e "s#^User=.*#User=$(id -un)#" \
    -e "s#^Group=.*#Group=$(id -gn)#" \
    "$HERE/$UNIT" | sudo tee "$DEST" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"
sudo systemctl --no-pager --lines=0 status "$UNIT" || true
echo
echo "Log:  journalctl -u $UNIT -f"
echo "Buka: http://127.0.0.1:8000"
