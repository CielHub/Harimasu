# Changelog

## Periodic Full Restart + join verification

- Mengubah periodic action dari deep-link-only menjadi full restart: package target di-force-stop, dipastikan PID-nya hilang, lalu diluncurkan ulang melalui alur normal.
- Menambahkan global launch coordinator agar full launch/restart lintas package tidak tumpang-tindih. Tidak ada pemeriksaan atau ambang RAM.
- Menambahkan verifikasi join yang mengutamakan marker positif dari log internal Roblox yang muncul setelah deep link, jika log tersedia dan terbaca oleh root.
- Menambahkan verifikasi tambahan: PID masih hidup, WindowManager melihat package di tiga sampel berurutan, tidak ada ActivityManager crash/ANR yang eksplisit, dan tidak ada sinyal join-error baru.
- Menambahkan fallback PID/window jika log internal tidak tersedia. Dokumentasi menjelaskan bahwa fallback ini tidak bisa membuktikan game world 100% selesai dimuat.
- Memastikan jeda stabilisasi 20 detik terjadi setelah join health check lulus dan sebelum package berikutnya direstart.
- Mengatur ulang interval periodic hanya setelah restart, join verification, dan stabilisasi berhasil.
- Jika join gagal, package tidak ditandai Running; bot melakukan retry terbatas dan menambah cooldown 300 detik setelah budget retry habis.
- Menahan sensor recovery biasa saat periodic restart agar tidak memulai deep link kedua yang bertabrakan; crash nyata tetap dapat mempreempt operasi.
- Menambahkan regression tests untuk retry offline, deteksi join error, full restart package-scoped, dan serialisasi global launch.

## Retained previous fixes

- Recovery worker tetap hidup saat event error baru membatalkan percobaan lama.
- Deep-link recovery tetap punya retry terbatas dan cooldown.
- Startup awal berjalan satu per satu dengan jeda 20 detik setelah join flow diterima dan PID diverifikasi.
- Lobby delay dapat dikonfigurasi dari 0 sampai 3600 detik.
