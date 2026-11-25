# mlai-shim 4.0 — ml.ai di 9Router / opencode

Jembatan OpenAI-compatible untuk **ML AI Code**, memakai daemon lokal `mlai serve`.

## Status

| Komponen | Nilai |
|---|---|
| Shim | `http://127.0.0.1:8787/v1` |
| Daemon socket | `~/.mlai-shim/daemon.sock` |
| Gateway 9Router | `http://localhost:20128/v1` |
| Provider 9Router | `mlai` |
| Model | `mlai/pixis`, `mlai/pixis-sol`, `mlai/pixis-opus`, +7 lainnya |
| Tier default | `fast` (raw completion, ~1 s) |

## Perbaikan di 4.0

| # | Masalah | Perbaikan | Hasil terukur |
|---|---|---|---|
| 1 | 5 request paralel → 4 gagal | serialisasi + jeda 0,35 s + cooldown | **25 serial 25/25, 3× burst 16/16** |
| 2 | prompt >6k gagal | trim tengah + fallback ke agent | 66k char → **200 OK** |
| 3 | `max_tokens` diabaikan | potong hasil, `finish_reason=length` | mt=5 → 16 char, mt=200 → 715 char |
| 4 | model asing diganti diam-diam | HTTP 404 `model_not_found` | ✅ |
| 5 | tanpa CORS | header + handler OPTIONS | ACAO `*`, OPTIONS 204 |
| 6 | client disconnect → traceback | tangkap BrokenPipe/Reset | 0 traceback di log |
| 7 | antrian tak terbatas | semaphore → 429 | 30 paralel → 16 ok, 14 rapi 429 |
| 8 | socket di `/tmp` | pindah ke `~/.mlai-shim/` | tahan reboot |
| 9 | log tanpa rotasi | logrotate `su ubuntu` | maxsize 10M, 4 arsip |
| 10 | tidak ada watchdog | timer 5 menit (hanya jika shim mati) | tidak salah restart saat antrian sibuk |
| 11 | `daemon:false` nyangkut 30 s saat boot | cache negatif hanya 3 s | cold start langsung benar |

## Arsitektur

```
opencode / 9Router
        │  http://127.0.0.1:8787/v1
        ▼
   shim_v4.py   (systemd: mlai-shim)
        │  unix socket ~/.mlai-shim/daemon.sock
        │  serialisasi + jeda 0,35 s
        ▼
   mlai serve   (systemd: mlai-daemon)
        ▼
   code.ml.ai
```

## Latensi

| Jalur | Latensi | Kuota |
|---|---|---|
| `/v1/complete` (tier fast) | **0,9–2,7 s** | tanpa limit |
| `/v1/sessions` (tier agent) | 6–8 s | 100 task/hari |
| `mlai -p` (tier cli) | 13–14 s | — |

Request paralel dilayani **berurutan** (~0,7 req/detik) karena daemon hanya
sanggup satu completion sekaligus. Lebih dari 16 antrian → HTTP 429.

## Service

```bash
systemctl status mlai-daemon mlai-shim mlai-health.timer
sudo systemctl restart mlai-daemon mlai-shim
tail -f ~/mlai-shim/shim.log ~/mlai-shim/daemon.log
```

| Unit | Fungsi |
|---|---|
| `mlai-daemon.service` | `mlai serve` → socket |
| `mlai-shim.service` | `shim_v4.py` → port 8787 (Requires daemon) |
| `mlai-health.timer` | cek `/health` tiap 5 menit, restart kalau rusak |

Semua `enabled` — otomatis jalan saat boot.

## Konfigurasi

| Env | Default | Guna |
|---|---|---|
| `MLAI_SHIM_PORT` | `8787` | port HTTP |
| `MLAI_SHIM_HOST` | `127.0.0.1` | `0.0.0.0` untuk akses LAN |
| `MLAI_SOCKET` | `~/.mlai-shim/daemon.sock` | socket daemon |
| `MLAI_SHIM_TIER` | `fast` | `fast`, `agent`, `auto`, `cli` |
| `MLAI_SHIM_MODEL` | `pixis` | model default |
| `MLAI_SHIM_MAX_PROMPT` | `6000` | batas prompt sebelum trim |
| `MLAI_SHIM_MIN_GAP` | `0.35` | jeda minimum antar panggilan daemon |
| `MLAI_SHIM_MAX_QUEUE` | `16` | antrian sebelum 429 |
| `MLAI_SHIM_TIMEOUT` | `600` | detik |

## Model

| ID | Nama |
|---|---|
| `pixis` | Pixis (cepat) |
| `pixis-sol` | GPT 6.1R Sol |
| `pixis-opus` | Opus 5.5R |
| `ml-ai-forge` | ML.ai Forge |
| `gpt-6-astra` | GPT 6 Astra |
| `grok-4.7` | Grok 4.7 |
| `kimi-k3` | ML.ai Coder |
| `glm-5.3` | GLM 5.3 |
| `claude-sonnet-5.5` | Claude Sonnet 5.5 |
| `claude-fable-5.2` | Claude Fable 5.2 |

Di 9Router jadi `mlai/<id>`.

## Tes

```bash
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/v1/models
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mlai/pixis","messages":[{"role":"user","content":"halo"}]}'
```

## Kalau perlu daftar ulang di 9Router

Dashboard `http://localhost:20128/dashboard` → **Providers** → **Add Provider**
→ **OpenAI Compatible**:

```
Name     : mlai
Base URL : http://127.0.0.1:8787/v1
API Key  : mlai-shim-local      (harus tidak kosong, nilainya tidak diperiksa)
```

Model terisi otomatis dari `/v1/models`.

## opencode

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "9router": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "9Router",
      "options": {
        "baseURL": "http://localhost:20128/v1",
        "apiKey": "{env:ROUTER_KEY}"
      },
      "models": {
        "mlai/pixis":      { "name": "ml.ai Pixis (cepat)" },
        "mlai/pixis-sol":  { "name": "ml.ai Sol" },
        "mlai/pixis-opus": { "name": "ml.ai Opus" }
      }
    }
  }
}
```

`ROUTER_KEY` = API key 9Router (dashboard → API Keys), **bukan** key ml.ai.

## Batasan (tidak bisa diperbaiki)

- **Tanpa tool-calling.** Model menjawab teks; opencode tidak bisa baca/tulis
  file lewat model ini. Cocok untuk chat & tanya-jawab, bukan agentic coding.
- **Bukan token-streaming.** Teks datang utuh sekali (~1 s). Format SSE valid,
  tapi tidak ada efek mengetik.
- **Serial, bukan paralel.** Daemon hanya satu completion sekaligus. Beban
  tinggi → antrian; >16 → 429.
- **Prompt >6k dipotong tengah** (atau dialihkan ke tier agent yang lebih lambat
  dan kena kuota 100/hari).
- **`max_tokens` dipotong di shim**, bukan oleh model — perkiraan 3,6 char/token.
- Kalau `mlai login` kedaluwarsa (2 Des 2026): `mlai login` lalu restart service.

## Troubleshooting

| Gejala | Obat |
|---|---|
| `daemon: false` | `sudo systemctl restart mlai-daemon` |
| 429 terus | naikkan `MLAI_SHIM_MAX_QUEUE`, atau kurangi konkurensi klien |
| Semua lambat 6-10 s | `MLAI_SHIM_TIER` bukan `fast` |
| `already running` di daemon.log | `pkill -f "mlai serve"` lalu restart |
| Model tidak muncul di 9Router | `curl 127.0.0.1:8787/v1/models`, restart 9Router |
| `Missing API key` dari 9Router | kirim header `Authorization: Bearer <key 9Router>` |

## File

| File | Isi |
|---|---|
| `shim_v4.py` | versi aktif |
| `shim_fast.py` | v3 (tanpa perbaikan paralel) |
| `shim2.py` | v2 (agent-only) |
| `shim.py` | v1 (CLI spawn) |
| `systemd/` | unit + logrotate (sumber kebenaran) |
| `README-v1.md` | dokumentasi lama |
