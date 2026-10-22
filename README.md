# Harimasu Auto Rejoin Roblox

Target: Termux dengan akses root (Magisk/KernelSU) pada Android 10.

## Startup awal

Untuk setiap package yang dipilih, bot menjalankan alur **satu per satu**:

1. Membuka package Roblox. Clone yang memang otomatis masuk floating window tetap dibiarkan memakai perilaku tersebut.
2. Menunggu PID package terdeteksi.
3. Diam di lobby selama nilai `lobby_delay` (default di source: 30 detik; nilai konfigurasi lama tetap dipertahankan).
4. Mengirim deep link ke Place ID atau private server, dengan maksimal 3 percobaan jika perintah Android gagal.
5. Memastikan proses package masih hidup.
6. Menunggu 20 detik sebelum membuka package berikutnya.

Lobby delay bisa diubah dari menu **Setting → Lobby delay**. Nilai yang diterima adalah 0 sampai 3600 detik.

## Periodic Full Restart

Pengaturan lama `periodic_rejoin_*` tetap dipakai agar file konfigurasi pengguna tetap kompatibel, tetapi fungsinya sekarang adalah **full restart package berkala**, bukan hanya mengirim deep link ke proses yang masih hidup.

Alur setiap package yang jatuh tempo:

1. Masuk ke antrean global, lalu tunggu sampai tidak ada peluncuran/restart penuh lain yang sedang berjalan.
2. Jalankan `am force-stop` hanya untuk package target dan pastikan PID-nya hilang.
3. Buka ulang package target.
4. Tunggu lobby sesuai `lobby_delay`.
5. Kirim deep link dan verifikasi join.
6. Jika akses log internal Roblox tersedia, baca hanya byte log baru dan cari marker join baru seperti `Connection accepted from`, `DataModelLoadingPhase::OnAfterLoadingComplete`, atau `joinGamePostStandard finished`. Marker koneksi/join gagal menyebabkan percobaan ditolak.
7. Verifikasi PID tetap ada, WindowManager melaporkan jendela package pada 3 sampel berurutan, dan tidak ada sinyal error/ANR yang diketahui.
8. Tunggu 20 detik stabilisasi. **Baru setelah semua pemeriksaan selesai, timer interval berikutnya dimulai dan package selanjutnya boleh direstart.**

Jika direktori log internal Roblox tidak tersedia atau tidak bisa dibaca, bot memakai fallback PID + window stabil + tidak ada error yang diketahui. Fallback ini lebih lemah: jendela terlihat bukan bukti mutlak bahwa dunia game sudah selesai dimuat. Marker internal Roblox dapat berbeda antarversi/clone.

## Recovery saat join gagal

- Maksimal 3 percobaan deep link di dalam alur startup.
- Restart periodik yang gagal akan dicoba ulang setelah cooldown dari konfigurasi (`periodic_rejoin_retry_seconds`, default 60 detik), maksimal sejumlah retry yang disetel (`periodic_rejoin_max_retries`, default 2).
- Jika batas retry tercapai, package tetap ditandai belum pulih dan dijadwalkan ulang setelah cooldown lebih panjang, minimal 300 detik. Bot tidak menunggu 4 jam sambil menganggap package itu sehat.
- Error join yang terdeteksi ketika periodic restart berjalan dicatat untuk verifier, bukan mengirim recovery kedua yang bertabrakan.
- Crash proses yang nyata tetap boleh mempreempt restart periodik dan masuk jalur full recovery.
- Global launch coordinator menserialkan seluruh proses launch/restart, bukan hanya perintah `am start`.
- Pengamanan/threshold RAM sengaja **tidak** digunakan pada versi ini.

## Konfigurasi periodic

- `periodic_rejoin_enabled`: mengaktifkan atau menonaktifkan periodic full restart.
- `periodic_rejoin_interval_hours`: interval sejak join berhasil diverifikasi dan stabil, default 4 jam.
- `periodic_rejoin_stagger_seconds`: jarak minimum awal antarjob restart yang jatuh tempo, default 45 detik.
- `periodic_rejoin_verify_seconds`: batas waktu verifikasi join, default 45 detik (rentang 15–120 detik).
- `periodic_rejoin_retry_seconds`: cooldown retry, default 60 detik.
- `periodic_rejoin_max_retries`: jumlah retry setelah percobaan awal, default 2.

## Menjalankan

```bash
python main.py
```

## Tes logika tanpa perangkat Android

Dari folder proyek, jalankan:

```bash
python tests/test_logic.py
```

Tes memakai fake shell dan tidak menjalankan perintah root, tidak membaca log Android sungguhan, dan tidak membuka Roblox.
