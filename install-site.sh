#!/bin/sh
# Arahkan sebuah domain ke layanan ini: sertifikat Let's Encrypt + vhost nginx.
# Butuh sudo, dan butuh record DNS domain itu sudah menunjuk ke mesin ini.
#
#   ./install-site.sh                      pasang mbahgpt.com
#   ./install-site.sh --email you@mail.com  alamat notifikasi kedaluwarsa
#   ./install-site.sh --skip-dns-check      lanjut walau DNS belum menyebar
#   ./install-site.sh --uninstall           lepas vhost (sertifikat dibiarkan)
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DOMAIN=mbahgpt.com
ALIAS=www.mbahgpt.com
EMAIL=${CERTBOT_EMAIL:-}
SKIP_DNS=0
LINK=/etc/nginx/sites-enabled/$DOMAIN
DEST=/etc/nginx/sites-available/$DOMAIN

while [ $# -gt 0 ]; do
    case "$1" in
        --email)          EMAIL=$2; shift 2 ;;
        --skip-dns-check) SKIP_DNS=1; shift ;;
        --uninstall)
            sudo rm -f "$LINK" "$DEST"
            sudo nginx -t && sudo systemctl reload nginx
            echo "vhost $DOMAIN dilepas (sertifikat tetap ada)."
            exit 0 ;;
        *) echo "opsi tidak dikenal: $1" >&2; exit 2 ;;
    esac
done

[ -n "$EMAIL" ] || { echo "butuh --email <alamat> untuk pendaftaran ACME" >&2; exit 2; }

# --- 1. DNS ---------------------------------------------------------------
# Tanpa ini certbot gagal dengan pesan yang membingungkan: Let's Encrypt
# meminta http://$DOMAIN/.well-known/... dan yang menjawab adalah server LAIN
# (mis. halaman parkir registrar), bukan mesin ini.
MINE=$(curl -fsS --max-time 10 https://api.ipify.org)
if [ "$SKIP_DNS" -ne 1 ]; then
    for name in "$DOMAIN" "$ALIAS"; do
        # Resolver lokal bisa masih memegang cache record lama (TTL 300 di
        # Namecheap), jadi jawabannya digabung dengan resolver publik. Yang
        # diperiksa adalah KEANGGOTAAN, bukan baris pertama/terakhir: satu nama
        # boleh punya beberapa A record, dan yang berbahaya justru sisa record
        # lama yang masih ikut dijawab.
        GOT=$( { dig +short "$name" A; dig +short @1.1.1.1 "$name" A; } \
               2>/dev/null | grep -E '^[0-9.]+$' | sort -u)
        if ! echo "$GOT" | grep -qx "$MINE"; then
            cat >&2 <<EOF
$name menunjuk ke "$(echo "$GOT" | paste -sd' ' -)", bukan ke $MINE.

Buat/ubah record di panel DNS domain (Namecheap: Domain List > Manage >
Advanced DNS), lalu jalankan skrip ini lagi:

    A    @      $MINE    (TTL otomatis)
    A    www    $MINE

Penyebaran biasanya 5-30 menit. Cek dengan: dig +short @1.1.1.1 $name A
EOF
            exit 1
        fi
        OTHER=$(echo "$GOT" | grep -vx "$MINE" | paste -sd' ' -)
        if [ -n "$OTHER" ]; then
            echo "PERINGATAN: $name juga menjawab $OTHER — hapus record lama itu," \
                 "kalau tidak sebagian permintaan (termasuk tantangan ACME saat" \
                 "perpanjangan) akan mendarat di server lain." >&2
        fi
    done
    echo "DNS: $DOMAIN dan $ALIAS -> $MINE"
fi

# --- 2. Sertifikat --------------------------------------------------------
# Mode certonly + webroot, bukan installer nginx: vhost-nya dikelola dari repo
# ini, dan certbot yang mengedit sendiri berkas config akan membuat salinan di
# repo dan yang terpasang perlahan berbeda.
#
# Webroot-nya milik catch-all: conf.d/20-vhost.conf sengaja membiarkan jalur
# /.well-known/acme-challenge/ hidup untuk Host yang belum punya blok server,
# jadi sertifikat bisa terbit SEBELUM vhost ini dipasang. Urutannya penting —
# vhost di bawah menunjuk ke berkas sertifikat, dan nginx menolak start kalau
# berkas itu belum ada.
# sudo test, bukan test biasa: /etc/letsencrypt/live ber-mode 700 milik root,
# jadi pemeriksaan sebagai user biasa SELALU bilang "belum ada" dan certbot
# dipanggil setiap kali skrip ini dijalankan.
if ! sudo test -d "/etc/letsencrypt/live/$DOMAIN"; then
    sudo certbot certonly --webroot -w /var/www/letsencrypt \
        -d "$DOMAIN" -d "$ALIAS" \
        --agree-tos -m "$EMAIL" --non-interactive --keep-until-expiring
else
    echo "sertifikat $DOMAIN sudah ada, dilewati."
fi

# Perpanjangan otomatis lewat certbot.timer hanya menulis berkas baru; nginx
# masih memegang sertifikat lama di memori sampai dimuat ulang.
HOOK=/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
if [ ! -f "$HOOK" ]; then
    printf '#!/bin/sh\nsystemctl reload nginx\n' | sudo tee "$HOOK" >/dev/null
    sudo chmod 755 "$HOOK"
    echo "hook reload nginx dipasang: $HOOK"
fi

# --- 3. vhost -------------------------------------------------------------
sudo install -m 644 "$HERE/nginx-mbahgpt.conf" "$DEST"
sudo ln -sfn "$DEST" "$LINK"
sudo nginx -t
sudo systemctl reload nginx

# --- 4. Bukti, bukan asumsi ----------------------------------------------
# Jalur ACME diuji lewat nama domainnya, bukan hanya diasumsikan ada di config.
# Sejak vhost ini terpasang, permintaan tantangan tidak lagi jatuh ke catch-all,
# jadi blok port 80 di atas yang harus menjawabnya — kalau tertukar dengan
# redirect 301, perpanjangan otomatis gagal 60 hari dari sekarang.
PROBE=probe-$(id -u)-$$
echo ok | sudo tee "/var/www/letsencrypt/.well-known/acme-challenge/$PROBE" >/dev/null
ACME=$(curl -s -A "Mozilla/5.0" "http://$DOMAIN/.well-known/acme-challenge/$PROBE" || true)
sudo rm -f "/var/www/letsencrypt/.well-known/acme-challenge/$PROBE"
if [ "$ACME" = "ok" ]; then
    echo "ACME: jalur tantangan terjawab lewat http://$DOMAIN (perpanjangan aman)"
else
    echo "PERINGATAN: jalur .well-known/acme-challenge/ TIDAK terjawab" \
         "(dapat: '${ACME:-kosong}'). Perpanjangan otomatis akan gagal." >&2
fi

# 401 adalah jawaban yang BENAR di sini: proxy sampai ke Python, dan Python
# menolak permintaan tanpa token. 200 tanpa token berarti UI_TOKEN tidak
# terpasang dan situs terbuka untuk siapa pun.
# User-agent browser wajib: $block_ua di 10-hardening.conf menolak "curl" dari IP
# tak tepercaya dengan 444, dan permintaan ini keluar-masuk lewat IP publik, jadi
# tanpa -A hasilnya 000 (di HTTP/2 terlihat sebagai PROTOCOL_ERROR) — kelihatan
# seperti situs mati padahal justru aturan anti-bot yang bekerja.
CODE=$(curl -s -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36" \
            -o /dev/null -w '%{http_code}' "https://$DOMAIN/" || echo "gagal")
echo
echo "https://$DOMAIN/ -> HTTP $CODE"
case "$CODE" in
    401) echo "OK: proxy jalan, autentikasi token aktif."
         echo "Buka sekali: https://$DOMAIN/?token=<isi OPENROUTER_UI_TOKEN di .env>" ;;
    200) echo "PERINGATAN: terbuka tanpa token. Set OPENROUTER_UI_TOKEN di .env," \
              "lalu: sudo systemctl restart mbahgpt.service" ;;
    400) echo "PERINGATAN: server.py menolak Host-nya — tambahkan $DOMAIN ke" \
              "OPENROUTER_PUBLIC_HOST di .env, lalu restart mbahgpt.service" ;;
    *)   echo "Periksa: journalctl -u mbahgpt.service -n 30;" \
              "tail /var/log/nginx/mbahgpt.error.log" ;;
esac
