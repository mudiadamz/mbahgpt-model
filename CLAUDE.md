# Petunjuk kerja di repo ini

## Aturan utama: ARCHITECTURE.md harus selalu ikut terbarui

**Setiap perubahan atau penambahan pada proyek ini wajib disertai pembaruan
`ARCHITECTURE.md` dalam perubahan yang sama.** Dokumen itu bukan lampiran — ia
catatan keputusan proyek, dan dokumen yang tidak akurat lebih berbahaya daripada
tidak ada dokumen sama sekali.

Yang memicu pembaruan:

| Perubahan | Bagian yang harus disentuh |
|---|---|
| Modul/berkas baru, ukuran modul berubah banyak | §2 Peta modul |
| Rute HTTP, urutan alur, header baru | §3 Alur satu pesan |
| Tabel/kolom/indeks/migrasi SQLite | §4 Model data |
| Perilaku streaming, memori, search, keamanan, konkurensi | §5 Subsistem |
| Apa pun yang mengubah tampilan atau interaksi | §6 Keputusan UI/UX |
| Variabel env baru atau default berubah | §7 Konfigurasi |
| Batasan baru ditemukan, atau batasan lama teratasi | §8 Batasan |
| Cara menjalankan berubah | §9 Menjalankan |

### Akurasi diverifikasi, bukan diingat

Sebelum menulis angka apa pun ke dokumen — konstanta, batas, harga, ukuran —
**baca dari kode**, jangan dari ingatan atau dari isi dokumen sebelumnya.

```bash
grep -oE 'MAX_CONCURRENT = [0-9]+' index.html
grep -rhoE 'os\.environ\.get\("[A-Z_]+"' *.py | sort -u
python3 -c "import sqlite3;print([r[0] for r in sqlite3.connect('chats.db').execute(\"SELECT sql FROM sqlite_master WHERE type='table'\")])"
wc -l *.py index.html fetch-vendor.sh
```

Kalau sebuah klaim tidak bisa diverifikasi, tulis apa adanya sebagai asumsi atau
jangan ditulis. Jangan menebak.

### Tulis "kenapa", bukan cuma "apa"

Nilai utama dokumen ini ada pada alasan di balik keputusan — terutama yang lahir
dari pengukuran atau dari bug yang sudah menggigit. Kalau sebuah perubahan
menyelesaikan masalah yang tidak kentara, catat masalahnya, bukan hanya solusinya.

---

## Batasan yang tidak boleh dilanggar diam-diam

Semua ini keputusan sadar. Kalau ada alasan kuat untuk mengubahnya, sampaikan
dulu ke pemilik repo — jangan diubah sebagai efek samping pekerjaan lain.

- **Python stdlib saja.** Tanpa `pip install`, tanpa build step. Target Python 3.9.
- **Kunci API tidak pernah sampai browser.** Halaman hanya bicara ke server lokal.
- **Renderer tidak pernah memakai `innerHTML`.** Keluaran model selalu dirender
  sebagai DOM node. Ini satu-satunya penahan XSS di jalur itu.
- **CSP tanpa `unsafe-inline`.** Nonce dibuat per response dan diteruskan ke Ionic
  lewat `setNonce`.
- **Server menolak start** saat bind non-loopback tanpa `OPENROUTER_UI_TOKEN`.
- **Model dan temperature dari `.env`**, tidak dari UI, dan nilai dari klien
  diabaikan.
- **`vendor/` tidak masuk repo** — dipasang lewat `./fetch-vendor.sh` dengan hash
  terverifikasi.

---

## Orientasi cepat

```
qwen.py      inti: .env loader, request builder, CLI
db.py        skema + akses SQLite
memory.py    ekstraksi & pemeringkatan memori (murni fungsi, paling mudah diuji)
web.py       deteksi kebutuhan search, ekspansi query, eksekusi search
security.py  hardening HTTP
server.py    routing + orkestrasi satu giliran chat
index.html   UI Ionic, renderer markdown, manajemen stream
```

Baca `ARCHITECTURE.md` lebih dulu sebelum mengubah subsistem yang belum dikenal —
beberapa hal yang terlihat aneh di kode sebenarnya perbaikan bug yang sudah
terdokumentasi di sana.

## Menguji perubahan

Tidak ada test runner. Verifikasi yang dipakai selama ini:

```bash
python3 -m py_compile *.py                      # sintaks Python
node --check <(sed -n '/<script>/,/<\/script>/p' index.html)   # sintaks JS
./server.py -p 8790                             # jalankan, lalu uji lewat curl/browser
```

Untuk perubahan UI, jalankan server dan periksa di browser sungguhan — termasuk
lebar ponsel (±414 px) karena layout memakai `ion-split-pane`.
