# mlai-shim 3.0 — ml.ai di 9Router / opencode (OPTIMIZED)

Jembatan OpenAI-compatible untuk **ML AI Code**, memakai daemon lokal `mlai serve`.

## Hasil optimasi

| Jalur | Latensi | Catatan |
|---|---|---|
| **`/v1/complete`** (dipakai sekarang) | **0,9 – 2,3 s** (avg ~1,6 s) | tanpa agent loop, tanpa intent pass |
| `/v1/sessions` + `/messages` | 6 – 8 s | agent penuh; kena limit 100 task/hari |
| `mlai -p` (spawn CLI) | 13 – 14 s | startup proses tiap request |

**~8x lebih cepat** dari versi pertama (13,5 s → 1,6 s). Terukur 20/20 sukses.

Kunci temuan:
- `POST /v1/complete` di socket daemon = raw completion, **tidak** menghitung
  kuota "task starts" (jalur agent kena limit 100/hari, reset 00:00 UTC).
- Prompt **wajib** diakhiri `Assistant:` — tanpa itu balasannya kosong.
- ~1 dari 6 prompt balik kosong walau diulang identik. Menambahkan `\n` atau
  ` Jawab:` di ekor prompt membuatnya jalan. Shim mencoba 4 variasi ekor.

## Arsitektur

```
opencode / 9Router
        │  http://127.0.0.1:8787/v1
        ▼
   shim_fast.py  (systemd: mlai-shim)
        │  unix socket /tmp/mlai-shim.sock
        ▼
   mlai serve    (systemd: mlai-daemon)
        │
        ▼
   code.ml.ai
```

## Status terpasang di mesin ini

| Komponen | Nilai |
|---|---|
| CLI | `mlai` 0.3.26 (`~/.npm-global/bin/mlai`) |
| Akun | akun ml.ai (dirahasiakan) |
| Daemon | `mlai serve` → socket `/tmp/mlai-shim.sock` |
| Shim | `http://127.0.0.1:8787/v1` |
| Provider 9Router | `mlai` (`openai-compatible-chat-0124663d-...`) |
| Model di 9Router | `mlai/pixis`, `mlai/pixis-sol`, `mlai/pixis-opus`, dst. |
| Gateway | `http://localhost:20128/v1` |

## Service (aktif, auto-start saat boot)

```bash
systemctl status mlai-daemon mlai-shim
sudo systemctl restart mlai-daemon mlai-shim
tail -f ~/mlai-shim/shim.log ~/mlai-shim/daemon.log
```

| Unit | Fungsi |
|---|---|
| `mlai-daemon.service` | `mlai serve` → socket `/tmp/mlai-shim.sock` |
| `mlai-shim.service` | `shim_fast.py` → `http://127.0.0.1:8787/v1` |

Urutan penting: daemon dulu, shim kedua (`Requires=mlai-daemon.service`).

## Konfigurasi

| Env | Default | Guna |
|---|---|---|
| `MLAI_SHIM_PORT` | `8787` | port HTTP |
| `MLAI_SHIM_HOST` | `127.0.0.1` | `0.0.0.0` kalau 9Router di mesin lain |
| `MLAI_SOCKET` | `/tmp/mlai-shim.sock` | socket daemon |
| `MLAI_SHIM_TIER` | `fast` | `fast` (cepat), `agent` (penuh), `auto` (fast→agent→cli), `cli` |
| `MLAI_SHIM_MODEL` | `pixis` | model default |
| `MLAI_SHIM_TIMEOUT` | `600` | detik |
| `MLAI_SHIM_WORKDIR` | `~/mlai-shim/scratch` | folder kerja untuk tier agent |

`MLAI_SHIM_TIER=fast` sengaja **tidak** fallback ke agent — supaya tidak
menghabiskan kuota 100 task/hari saat raw completion gagal sesaat.

## Model tersedia

`pixis` (cepat), `pixis-sol` (GPT 6.1R Sol), `pixis-opus` (Opus 5.5R),
`ml-ai-forge`, `gpt-6-astra`, `grok-4.7`, `kimi-k3`, `glm-5.3`,
`claude-sonnet-5.5`, `claude-fable-5.2`

Di 9Router tampil sebagai `mlai/<id>`, mis. `mlai/pixis`.

## Tes

```bash
curl -s http://127.0.0.1:8787/health
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

`ROUTER_KEY` = API key 9Router (dashboard → API Keys), bukan key ml.ai.

## Batasan

- **Bukan token-streaming.** Teks datang utuh sekali; `stream:true` tetap
  mengirim format SSE yang valid agar klien tidak error, tapi isinya satu chunk.
- **Tanpa tool-calling.** `tools` diterima tapi model menjawab sebagai teks.
- **Jalur agent kena limit harian 100 task** (reset 00:00 UTC). Jalur `fast`
  tidak terkena limit ini.
- **Konteks tidak dipangkas otomatis.** Prompt panjang = lebih lambat.
- Kalau `mlai login` kedaluwarsa (2 Des 2026): `mlai login` lalu
  `sudo systemctl restart mlai-daemon mlai-shim`.

## Troubleshooting

| Gejala | Obat |
|---|---|
| `502 empty completion` | cek `systemctl status mlai-daemon`; restart daemon |
| `daemon: false` di /health | socket hilang → `sudo systemctl restart mlai-daemon` |
| Semua lambat (6-10 s) | `MLAI_SHIM_TIER` bukan `fast` |
| `already running` di daemon.log | ada proses `mlai serve` lain; `pkill -f "mlai serve"` lalu restart |
| Model tidak muncul di 9Router | `curl 127.0.0.1:8787/v1/models`, lalu restart 9Router |
| `Missing API key` dari 9Router | kirim header `Authorization: Bearer <key 9Router>` |

## File

- `shim_fast.py` — versi aktif (tier: complete → agent → cli)
- `shim2.py` — versi sebelumnya (agent-only)
- `shim.py` — versi pertama (CLI spawn)
- `run.sh` — runner manual tanpa systemd
