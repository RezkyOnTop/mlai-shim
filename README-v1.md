# mlai-shim — ml.ai di 9Router / opencode (SUDAH JALAN)

Jembatan OpenAI-compatible di depan CLI resmi **ML AI Code** (`mlai`).

`code.ml.ai/v1/*` tidak ada — Next.js membalas HTTP 200 + HTML "Page not found",
makanya 9Router menampilkan hijau palsu. Yang benar-benar ada adalah **CLI**
mereka, dengan mode headless:

    mlai -p "prompt" --mode ask --output-format json [-m MODEL]

Shim ini membungkusnya jadi `/v1/chat/completions` yang valid.

## Status terpasang di mesin ini

| Komponen | Nilai |
|---|---|
| CLI | `mlai` 0.3.26 (`~/.npm-global/bin/mlai`) |
| Akun | akun ml.ai (dirahasiakan) |
| Shim | `http://127.0.0.1:8787/v1` |
| Provider 9Router | `mlai` (id `openai-compatible-chat-0124663d-...`) |
| Model di 9Router | `mlai/pixis-opus`, `mlai/pixis`, `mlai/mlai-auto` |
| Gateway | `http://localhost:20128/v1` |

Tes cepat:

```bash
curl -s http://127.0.0.1:8787/v1/models
curl -s http://127.0.0.1:20128/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"mlai/pixis-opus","messages":[{"role":"user","content":"halo"}]}'
```

## 1. Jalankan shim

```bash
~/mlai-shim/run.sh          # daemon + health check
python3 ~/mlai-shim/shim.py # atau foreground, untuk lihat log
```

Variabel lingkungan:

| Var | Default | Guna |
|---|---|---|
| `MLAI_SHIM_PORT` | `8787` | port |
| `MLAI_SHIM_HOST` | `127.0.0.1` | `0.0.0.0` kalau 9Router di mesin lain |
| `MLAI_BIN` | `mlai` (PATH) | path binary CLI |
| `MLAI_SHIM_TIMEOUT` | `600` | detik per request |
| `MLAI_SHIM_MODEL` | `pixis-opus` | model default |
| `MLAI_SHIM_WORKDIR` | `~/mlai-shim/scratch` | folder kerja CLI |
| `MLAI_SHIM_ARGS` | kosong | argumen CLI tambahan |

**Kenapa ada WORKDIR:** CLI itu agen. Kalau dijalankan di folder proyek, dia
mencoba membaca file. Di folder kosong + instruksi "jangan baca file", dia
menjawab seperti chat model biasa. Jangan arahkan ke folder kerjamu.

## 2. Kalau perlu daftar ulang di 9Router

Dashboard `http://localhost:20128/dashboard` (password kamu) → **Providers** →
**Add Provider** → **OpenAI Compatible**:

```
Name     : mlai
Base URL : http://127.0.0.1:8787/v1
API Key  : mlai-shim-local      (harus tidak kosong, nilainya tidak diperiksa)
```

Model terisi otomatis dari `/v1/models`. Kalau tidak, isi manual:
`pixis-opus`, `pixis`, `mlai-auto`.

## 3. Pakai di opencode

```bash
opencode plugin add opencode-9router
```

atau manual di `~/.config/opencode/opencode.json`:

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
        "mlai/pixis-opus": { "name": "ml.ai Pixis Opus" },
        "mlai/pixis":      { "name": "ml.ai Pixis (cepat)" }
      }
    }
  }
}
```

`ROUTER_KEY` = API key 9Router (dashboard → API Keys), bukan key ml.ai.

## 4. Batasan nyata

- **Lambat.** Tiap request menyalakan sesi agen: ~10–40 detik. Bukan model chat instan.
- **Satu request = satu proses CLI.** 3 request paralel = 3 proses `mlai`.
- **`tools`/`tool_calls` tidak benar-benar dipakai.** Parameter `tools` dari
  opencode diterima, tapi model menjawab dalam teks — opencode akan jalan
  mode teks, bukan agentic tool loop.
- **Kuota ikut paket ml.ai.** Exit code 5 = plan limit; shim balas HTTP 502.
- **Node 20 di mesin ini** → CLI pakai "built-in engine" (lebih lambat).
  Upgrade ke Node 22.19+ kalau mau engine utama.
- **9Router mengirim `data: [DONE]` dua kali** untuk stream (quirk 9Router, bukan shim).

## 5. Troubleshooting

| Gejala | Sebab / obat |
|---|---|
| `502 mlai_error: File reading is denied...` | WORKDIR menunjuk folder proyek; pakai folder kosong |
| `502 ... Not signed in` | `mlai login` ulang (token kedaluwarsa) |
| Model tidak muncul di 9Router | restart 9Router, atau cek `curl 127.0.0.1:8787/v1/models` |
| Balasan aneh soal "folder ini kosong" | prompt sistem hilang; lihat `flatten()` di `shim.py` |
| `Missing API key` dari 9Router | kirim header `Authorization: Bearer <key 9Router>` |

## 6. Kalau CLI-nya berubah

Argumen ada di `run_cli()`. Format output yang dibaca: field `text`, fallback
`result`/`output`; error di field `error` atau `state == "error"`.

```bash
mlai --help
mlai -p "test" --mode ask --output-format json | tail -1
```
