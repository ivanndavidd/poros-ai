# Poros — panduan integrasi untuk aplikasi consumer

Panduan ini menunjukkan cara **menyematkan (embed) asisten AI Poros ke dalam aplikasi Anda** dari nol:
dari meminta kredensial, menandatangani token per-user, memanggil API, sampai menampilkan
jawaban lengkap dengan grafik dan sitasi. Perkiraan waktu: ~15 menit untuk versi paling sederhana.

Referensi API lengkap (semua endpoint, field respons, kode error) ada di
[consumer-api.md](consumer-api.md). Dokumen ini fokus ke **alur end-to-end + contoh kode siap pakai**.

## Apa yang Anda bangun

```
Browser/app user  →  BACKEND Anda (penandatangan)  →  Poros /v1/chat
     (UI chat)         (simpan API key + private key,    (governed: SQL + dokumen +
                        tanda tangani token per user)      memory → jawaban + sitasi)
```

Aturan yang tidak bisa ditawar: **API key dan private signing key hanya boleh ada di backend Anda**, tidak
pernah di kode browser/mobile. Browser bicara ke backend Anda; backend Anda yang bicara ke Poros.

## Langkah 0 — siapkan informasi integrasi bersama admin Poros

| Item | Dipakai untuk | Contoh |
|---|---|---|
| Base URL | Alamat API Poros | `https://poros.internal:8000` |
| API key (`sk_…`) | Header `X-API-Key` (identitas aplikasi) | `sk_7d9…` |
| Private key RSA/EC | Menandatangani token end-user (RS256 direkomendasikan) | file PEM di secret manager |
| Issuer + audience | Mengikat token ke consumer dan Poros | `https://acme…` + `poros-acme` |
| Daftar nama role (atau role mapping) | Nilai claim `role` di token | mis. `Admin`, `Supervisor` |

Satu API key mewakili satu tenant (mis. tenant "Acme"). Poros tahu tenant dari API key — Anda tidak
mengirim id tenant.

## Model autentikasi — dua kredensial per request

Setiap panggilan `/v1` wajib membawa keduanya:

```
X-API-Key: sk_...            ← identitas APLIKASI (tenant, metering, rate limit) — tanpa role
Authorization: Bearer <JWT>  ← identitas + role END-USER (Anda yang menandatangani per user)
```

- API key menjawab "aplikasi mana yang memanggil".
- JWT menjawab "siapa orangnya dan role-nya apa". **Claim `role` inilah yang menentukan data apa
  yang boleh dilihat** (tabel, baris, kolom PII, dokumen). Claim `sub` adalah identitas untuk memory
  per-user dan kartu user di Audit Log.

### Menandatangani JWT (di backend Anda)

Claim minimum — RS256, TTL pendek (mis. 30-60 menit), token baru per sesi:

```json
{ "sub": "id-user-unik", "role": "Supervisor", "iss": "https://consumer-app.internal",
  "aud": "poros-acme", "iat": 1730000000, "exp": 1730003600 }
```

- `sub` = id atau email user asli yang stabil di sistem Anda. Jangan ganti-ganti untuk user yang
  sama dan jangan pakai nilai generik bersama seperti `console-user`: memory serta pengelompokan
  aktivitas Audit Log terikat ke nilai ini. Field `user_id` dari body request tidak dipercaya.
- `role` = nama role Poros, **atau** id role sistem Anda sendiri (mis. `SPV-01`) bila admin sudah
  membuat *role mapping*. Role tak dikenal → `403 ROLE_NOT_FOUND`.
- Tanda tangani dengan **private key**; admin Poros memasang pasangan public key/JWKS,
  algorithm, issuer, dan audience di konfigurasi tenant.

## Langkah 1 — backend proxy (contoh Node.js / Express)

Endpoint ini: (a) mengambil user yang sudah login di aplikasi Anda, (b) menandatangani token,
(c) meneruskan pertanyaan ke Poros, (d) mengembalikan jawaban. Browser tidak pernah menyentuh kredensial Poros.

```js
// npm i express jsonwebtoken
import express from "express";
import jwt from "jsonwebtoken";
import fs from "node:fs";

const app = express();
app.use(express.json());

const POROS_BASE   = process.env.POROS_BASE;        // https://poros.internal:8000
const POROS_KEY    = process.env.POROS_API_KEY;     // sk_...
const POROS_PRIVATE_KEY = fs.readFileSync(process.env.POROS_JWT_PRIVATE_KEY_FILE);

function signToken(user) {
  return jwt.sign(
    { sub: String(user.id), role: user.porosRole },  // porosRole = "Supervisor" / "Admin" / dst
    POROS_PRIVATE_KEY,
    { algorithm: "RS256", issuer: "https://consumer-app.internal",
      audience: "poros-acme", expiresIn: "30m" }
  );
}

app.post("/api/assistant", async (req, res) => {
  const user = req.user;                              // dari sesi login aplikasi Anda
  const { message, history, analysis_mode = "auto" } = req.body;
  const token = signToken(user);

  const r = await fetch(`${POROS_BASE}/v1/chat`, {
    method: "POST",
    headers: {
      "X-API-Key": POROS_KEY,
      "Authorization": "Bearer " + token,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ message, history, analysis_mode }),
  });

  const data = await r.json();
  if (!r.ok) return res.status(r.status).json(data);  // teruskan { detail: {code,message} }
  res.json(data);                                      // { answer, sql, citations, documents, ... }
});

app.listen(3000);
```

Variasi Python / FastAPI:

```python
# pip install fastapi uvicorn pyjwt httpx
import os, time, jwt, httpx
from fastapi import FastAPI, Request

app = FastAPI()
BASE, KEY = os.environ["POROS_BASE"], os.environ["POROS_API_KEY"]
PRIVATE_KEY = open(os.environ["POROS_JWT_PRIVATE_KEY_FILE"], "rb").read()

def sign(user):
    return jwt.encode({"sub": str(user["id"]), "role": user["poros_role"],
                       "iss": "https://consumer-app.internal", "aud": "poros-acme",
                       "iat": int(time.time()), "exp": int(time.time()) + 1800},
                      PRIVATE_KEY, algorithm="RS256")

@app.post("/api/assistant")
async def assistant(req: Request):
    body = await req.json()
    token = sign(req.state.user)                       # user dari sesi login Anda
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{BASE}/v1/chat",
                         headers={"X-API-Key": KEY, "Authorization": f"Bearer {token}"},
                         json={"message": body["message"], "history": body.get("history")})
    return r.json()
```

## Langkah 2 — UI chat (frontend minimal)

Frontend hanya bicara ke `/api/assistant` milik Anda:

```js
async function ask(message, history) {
  const r = await fetch("/api/assistant", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, history }),
  });
  const data = await r.json();
  if (!r.ok) { showError(data.detail?.message || "Gagal"); return; }
  render(data);   // lihat Langkah 3
}
```

## Langkah 3 — menampilkan jawaban lengkap

Respons `/v1/chat` berisi lebih dari sekadar teks. Tampilkan bagian yang relevan:

| Field | Cara menampilkan |
|---|---|
| `answer` | Teks jawaban (render sebagai Markdown). Bisa memuat blok ` ```chart ` / ` ```artifact ` — lihat di bawah |
| `sql` | Panel yang bisa dilipat "SQL (governed)": `sql.safe_sql`, tabel `sql.columns`/`sql.rows` |
| `citations` / `documents` | Chip sumber; jadikan link ke `GET /v1/documents/{document_id}/file` agar user bisa membuka dokumen aslinya |
| `analysis_mode` / `coverage` | Chip "Deep Analysis" dan status sudut bukti yang berhasil/masih hilang |
| `datasources_used` | Datasource yang benar-benar menghasilkan SQL, bukan datasource pertama saja |
| `web_sources` | Link sumber eksternal resmi yang dipakai sebagai validasi/benchmark |
| `blocked` | Bila `true`, gatekeeper menolak — tampilkan pesan sopan, bukan jawaban |
| `trace_id` | Simpan untuk dukungan/korelasi dengan audit admin |

Tambahkan tombol **membantu / tidak membantu** pada setiap jawaban. Kirim `trace_id`
hasil chat ke endpoint feedback; question/answer tidak perlu dikirim ulang:

```js
await fetch(`${POROS_BASE}/v1/feedback`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": POROS_API_KEY,
    "Authorization": `Bearer ${endUserJwt}`,
  },
  body: JSON.stringify({
    trace_id: data.trace_id,
    rating: "unhelpful",
    reason: "incorrect",
    comment: "Angka total berbeda dari dashboard sumber.",
  }),
});
```

Feedback tersebut muncul di menu **Response Feedback** pada console Poros, dikelompokkan
per tenant lalu per pasangan pertanyaan–jawaban.

**Grafik & dashboard interaktif.** Bila jawaban cocok divisualisasikan, `answer` menyertakan blok berpagar:

- ` ```chart ` berisi JSON spec (`type`, `labels`, `series`) → render dengan library chart pilihan Anda
  (mis. ECharts/Chart.js).
- ` ```artifact ` berisi satu dokumen HTML mandiri (interaktif) → tampilkan di dalam `<iframe sandbox>`.

Contoh mengekstrak blok chart dari `answer`:

```js
const m = answer.match(/```chart\s*([\s\S]*?)```/);
if (m) {
  const spec = JSON.parse(m[1]);   // { type, title, labels, series }
  renderChart(spec);               // gambar dengan library Anda
}
```

Bila Anda tidak ingin merender sendiri, cukup tampilkan `answer` sebagai teks — bloknya tetap terbaca
sebagai kode. (Console Poros merender chart/artifact secara otomatis; di aplikasi Anda, rendernya opsional.)

## Percakapan multi-turn & memory

**Multi-turn:** Poros stateless — aplikasi Anda yang membawa konteks. Kirim beberapa giliran terakhir
di `history` agar pertanyaan lanjutan nyambung:

```json
{
  "message": "kalau bulan lalu bagaimana?",
  "history": [
    { "role": "user",      "content": "total penjualan Shopee bulan ini?" },
    { "role": "assistant", "content": "Rp1,2 miliar." }
  ]
}
```

Cukup ±12 giliran terakhir. Strategi ringkasannya diatur admin per tenant (tab Memory).

**Auto-memory:** bila diaktifkan admin, Poros otomatis mencatat fakta jangka panjang tentang end-user
(identitas dari `sub`) dan me-recall-nya di chat berikutnya — tanpa perubahan di aplikasi Anda.

**Mode analisis:** kirim `analysis_mode: "auto"` untuk default yang direkomendasikan.
Pertanyaan keputusan kompleks seperti ekspansi gudang otomatis masuk `deep`. Consumer boleh
memaksa `"deep"` (kualitas maksimum, beberapa menit) atau `"fast"` (jawaban ringkas/lebih cepat).
Semua SQL tetap governed di ketiga mode.

## Streaming (opsional, UX terbaik)

Untuk UI chat, `POST /v1/chat_stream` mengalirkan progres lalu hasil akhir sebagai Server-Sent Events.
Body sama dengan `/chat`. Backend Anda mem-proxy stream-nya ke frontend. Tiap baris `data: {json}`:

```
data: {"type":"status","text":"Menulis & menjalankan SQL (governed)…"}
data: {"type":"done","result": { …payload /chat lengkap… }}
```

`type` = `status` (tampilkan sebagai progres) · `done` (hasil di `result`) · `error`.

Untuk tombol **Stop**, batalkan request stream aktif; tidak perlu endpoint cancel
terpisah. Di browser gunakan satu `AbortController` per prompt dan panggil
`controller.abort()` hanya ketika user menekan Stop. Jika request diproxy oleh backend
consumer, backend consumer wajib meneruskan disconnect tersebut dengan membatalkan
request upstream ke Poros. Perpindahan menu SPA jangan meng-abort stream jika respons
harus tetap dilanjutkan dan disimpan.

## Kode error yang perlu ditangani

| HTTP | `code` | Aksi di aplikasi Anda |
|---|---|---|
| 401 | `JWT_REQUIRED` / `JWT_INVALID` | Token hilang/kedaluwarsa/signature salah → tanda tangani ulang; cek jam server |
| 401 | `JWT_ASYMMETRIC_REQUIRED` / `JWT_ISSUER_AUDIENCE_REQUIRED` | Config tenant belum production-grade |
| 403 | `ROLE_NOT_FOUND` | Role tak dikenal → tampilkan "hubungi admin" (nilai tercatat di console Poros) |
| 403 | `CLAC_DENIED` / `ROLE_TABLE_DENIED` | User meminta data di luar hak role-nya → **bukan bug**, itu governance |
| 400 | `COST_LIMIT` | (BigQuery) query terlalu besar → sarankan user mempersempit (filter tanggal) |
| 429 | `RATE_LIMITED` | Backoff + retry dengan jitter (default 120/menit per key) |
| 429 | `BUDGET_EXCEEDED` | Hard budget bulanan habis; tunggu reset atau minta admin menaikkan |

Bentuk error selalu seragam: `{"detail": {"code": "...", "message": "..."}}`.

## SDK siap pakai

Bila tidak ingin menulis HTTP manual, ada SDK di folder `sdk/`:

```js
// JavaScript
import { AdminClient } from "./sdk/js/client.js";
const c = new AdminClient(POROS_BASE, POROS_KEY);
const res = await c.chat("total penjualan per region?", { jwt: signToken(user) });
```
```python
# Python
from sdk.python.client import Client
c = Client(POROS_BASE, POROS_KEY)
res = c.chat("total penjualan per region?", jwt=sign(user))
```

Keduanya menerima `jwt` sebagai token end-user yang Anda tandatangani.

## Checklist keamanan (wajib untuk consumer)

1. API key & private signing key **hanya di backend/secret manager** — tidak pernah di frontend/mobile.
2. Selalu lewat backend proxy Anda (browser → backend Anda → Poros).
3. TTL token **pendek** (15-30 menit) dan **satu token per user** — jangan pakai satu token bersama.
4. Jaga `sub` **unik dan stabil** per user asli (memory, isolasi, dan kartu user Audit Log bergantung
   padanya); jangan gunakan satu `sub` bersama untuk semua user.
5. Pakai **HTTPS/TLS** untuk mencapai Poros di produksi.
6. Tangani `429` (backoff) dan `403 ROLE_NOT_FOUND` (pesan ramah) dengan anggun.

## Ringkas: alur satu permintaan

1. User mengetik pertanyaan di UI Anda → kirim ke `/api/assistant` (backend Anda).
2. Backend Anda menandatangani JWT (sub + role user) → panggil Poros `/v1/chat` dengan API key + JWT.
3. Poros: gatekeeper → pilih data (SQL/dokumen/memory sesuai role) → jawaban ter-verifikasi + sitasi.
4. Backend Anda meneruskan jawaban → frontend menampilkan teks + grafik + link sumber.

Referensi field lengkap dan endpoint lain (`/v1/query`, `/v1/retrieve`, unduh dokumen) ada di
[consumer-api.md](consumer-api.md).
