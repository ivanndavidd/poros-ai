# Panduan Integrasi Consumer API (`/v1`)

Dokumen ini untuk developer **aplikasi konsumen** (web/mobile/internal tool) yang ingin memakai
kemampuan AI ter-governance dari Poros. Versi interaktif ada di console → menu **API Docs**;
skema mesin di `GET /docs` (OpenAPI).

## 1. Model autentikasi — Strict Pattern B

Setiap request `/v1` wajib membawa **dua identitas**:

```
X-API-Key: sk_xxxxxxxx                    ← identitas APLIKASI (diterbitkan admin, tab App Key)
Authorization: Bearer <JWT>               ← identitas + role END-USER (aplikasi yang menandatangani)
```

Yang perlu kamu minta dari admin Poros:

| Item | Dipakai untuk |
|---|---|
| Base URL (mis. `http://10.x.x.x:8000`) | Alamat API |
| API key (`sk_…`) | Header `X-API-Key` |
| Private key RSA/EC milik consumer/IdP | Menandatangani JWT end-user (RS256 direkomendasikan) |
| `issuer` dan `audience` yang disepakati | Claim wajib produksi; sama dengan config tenant di Poros |
| Daftar nama role ATAU konfirmasi **role mapping** | Nilai claim `role` di JWT |

Untuk staging sementara, Poros dapat menerima HS256 hanya jika operator menyalakan
`JWT_ALLOW_HS256_STAGING=1`. Secret harus disimpan per tenant (bukan fallback
`JWT_SECRET` global), algoritma harus tepat `HS256`, dan token tetap wajib membawa
`iss`, `aud`, `iat`, serta `exp`. Jangan gunakan pengecualian ini pada production riil.

### Membuat JWT (di backend aplikasimu — jangan di browser)

Claims minimum:
```json
{ "sub": "id-user-unik", "role": "analyst", "iss": "https://consumer-app.internal",
  "aud": "poros-acme", "iat": 1730000000, "exp": 1730003600 }
```
- `sub` — identitas end-user yang stabil, misalnya ID internal atau email kantor. Ini adalah
  **satu-satunya** sumber identitas untuk memory per-user dan pengelompokan kartu user di Audit Log
  (field `user_id` di body TIDAK dipercaya). Jangan memakai nilai generik yang sama seperti
  `console-user` untuk semua orang karena seluruh aktivitas akan terlihat sebagai satu user.
- `role` — menentukan hak akses data (RLAC/CLAC). Boleh berupa **string atau array**
  (nilai pertama yang dikenal yang dipakai). Nama claim bisa diganti admin (`role_claim`).
- Nilai `role` boleh nama role Poros **atau id role sistemmu sendiri** (mis. `SPV-01`)
  bila admin sudah membuat *role mapping*. Role yang tak dikenal → `403 ROLE_NOT_FOUND`
  dan tercatat di console agar admin bisa me-map-nya.
- Toleransi selisih jam server: ±120 detik (`JWT_LEEWAY_SEC`). Selalu isi `exp` yang pendek
  (mis. 1 jam) dan buat token baru per sesi/permintaan.

Contoh tanda tangan:

```php
// PHP (firebase/php-jwt)
use Firebase\JWT\JWT;
$token = JWT::encode(
  ['sub' => (string)$user->id, 'role' => $user->getRoleNames()->first(),
   'iss' => 'https://consumer-app.internal', 'aud' => 'poros-acme',
   'iat' => time(), 'exp' => time() + 3600],
  file_get_contents(env('POROS_JWT_PRIVATE_KEY_FILE')), 'RS256');
```
```python
# Python (pyjwt + cryptography)
import jwt, time
private_key = open("poros-jwt-private.pem", "rb").read()
token = jwt.encode({"sub": user_id, "role": role,
                    "iss": "https://consumer-app.internal", "aud": "poros-acme",
                    "iat": int(time.time()), "exp": int(time.time()) + 3600},
                   private_key, algorithm="RS256")
```
```js
// Node (jsonwebtoken)
import fs from "node:fs";
const privateKey = fs.readFileSync(process.env.POROS_JWT_PRIVATE_KEY_FILE);
const token = jwt.sign({ sub: userId, role }, privateKey,
  { algorithm: "RS256", issuer: "https://consumer-app.internal",
    audience: "poros-acme", expiresIn: "1h" });
```

## 2. Endpoint

| Endpoint | Fungsi | Catatan |
|---|---|---|
| `POST /v1/chat/jobs` | **Direkomendasikan**: antrekan agent/deep research, langsung menerima `job_id` (HTTP 202) | Aman terhadap timeout gateway 15 detik |
| `GET /v1/chat/jobs/{job_id}` | Poll status, progress/event baru, dan hasil akhir | Request pendek; gunakan parameter `after` |
| `DELETE /v1/chat/jobs/{job_id}` | Batalkan job secara kooperatif | Job queued langsung dibatalkan; job running berhenti di batas aman berikutnya |
| `POST /v1/chat` | Endpoint sinkron kompatibilitas lama | Jangan dipakai melalui gateway ber-timeout pendek |
| `POST /v1/chat_stream` | SSE kompatibilitas lama | Tidak cocok bila gateway membatasi total umur koneksi |
| `POST /v1/feedback` | Kirim penilaian user untuk satu hasil `/chat` berdasarkan `trace_id` | Tampil di panel Response Feedback |
| `POST /v1/query` | Eksekusi SQL mentah **yang tetap di-govern** | Untuk aplikasi yang menulis SQL sendiri |
| `POST /v1/retrieve` | Doc-RAG murni: potongan dokumen paling relevan + sitasi | Tanpa LLM — cepat (~1–2 dtk) |
| `GET /v1/documents/{id}/file` | Unduh/buka **dokumen asli** yang disitasi | ACL per dokumen ditegakkan |

### Memilih *business* (opsional, kecuali tenant mewajibkan)

Satu tenant bisa punya beberapa **business** — irisan data bernama, misalnya `1P`, `3P`,
`GDV` di Acme. Business menentukan tabel mana yang boleh dibaca **dan** kurasi mana yang
dipakai, karena satu tabel bisa berarti berbeda untuk dua lini bisnis.

Kirim `business` (id atau nama) pada `/v1/chat`, `/v1/chat/jobs`, `/v1/chat_stream`, dan
`/v1/query`:

```json
{ "message": "berapa gmv brand Philips bulan Juli", "business": "3P" }
```

Aturannya:

* **Tanpa `business`** — kalau tenant belum mewajibkan, jawaban diambil dari seluruh
  katalog seperti sebelumnya. Kalau tenant sudah mewajibkan, `/v1/query` ditolak `409` dan
  jalur chat BALIK DENGAN PERTANYAAN (lihat di bawah).
* **Dengan `business`** — hanya datasource dan tabel milik irisan itu yang terjangkau.
  Menyebut `datasource_id` tidak bisa dipakai untuk keluar dari irisan.
* Business yang tidak diberikan ke role di JWT diperlakukan seperti tidak ada.

Penolakan `409` membawa daftar pilihannya, jadi aplikasi bisa langsung menanyakannya ke
user:

```json
{"detail": {"code": "BUSINESS_REQUIRED",
            "message": "this tenant requires a business for every question; ...",
            "businesses": [{"id": "b_1a2b", "name": "3P", "descr": "Marketplace"}]}}
```

#### Pertanyaan konfirmasi (jalur chat)

Di `/v1/chat`, `/v1/chat/jobs`, dan `/v1/chat_stream`, tenant yang mewajibkan business
tidak menolak — turn-nya BERHENTI dan balasannya adalah sebuah pertanyaan. Statusnya tetap
`200`, `answer` berisi pertanyaan itu, dan ada field `confirmation`:

```json
{"answer": "Pertanyaan ini bisa dijawab dari lebih dari satu business (1P, 3P). Yang mana ...",
 "confirmation": {"kind": "business",
                  "options": [{"id": "b_1a2b", "name": "3P", "descr": "Marketplace"}],
                  "pending_question": "berapa gmv brand Philips bulan Juli"},
 "plan": ["ask_business"], "sql": null, "business": null}
```

Yang perlu dilakukan aplikasi: tampilkan `options` sebagai pilihan, lalu kirim ULANG
`pending_question` dengan `business` = pilihan user, dan simpan pilihan itu untuk sisa
percakapan supaya tidak ditanya lagi tiap giliran. Tidak ada model yang menebak di sini:
menebak business adalah kegagalan yang justru mau dicegah lapisan ini, dan tebakan yang
dibungkus konfirmasi tetap tebakan.

Dua hal yang TIDAK ditanyakan:

* role cuma punya SATU business — langsung dipakai, karena satu pilihan bukan pilihan.
  Yang terpakai selalu muncul di field `business` pada setiap jawaban.
* `business` yang disebut tidak dikenal, atau role belum punya grant sama sekali — itu
  tetap `409`, bukan menu. Menawarkan menu untuk business yang bukan haknya sama saja
  dengan mengiklankan irisan yang tidak pernah diberikan.

Aplikasi yang hanya menampilkan `answer` juga tetap jalan: kalau user membalas dengan nama
business-nya saja ("3P") dan `history` percakapan ikut dikirim, balasan itu dibaca sebagai
pilihan dan pertanyaan sebelumnya dijawab di irisan tersebut.

#### Asumsi alih-alih bertanya (`assumptions`)

Sejak chat overhaul item 5 hanya **business** yang masih ditanyakan (itu batas akses).
Tiga hal lain yang dulu menghentikan giliran dengan pertanyaan sekarang jadi **asumsi**:
modelnya memilih bacaan yang paling mungkin dimaksud pertanyaan dan percakapan, menyebutnya
di kalimat PERTAMA jawaban ("Saya membaca 'Philips' sebagai brand Philipsome."), dan user
mengoreksi di giliran berikutnya. Response membawa field `assumptions` (list; kosong kalau
tidak ada):

```json
{"answer": "Saya membaca 'Philips' sebagai brand Philipsome. GMV Juli 2026 ...",
 "status": "complete",
 "assumptions": [
   {"kind": "entity", "term": "Philips", "chosen": "Philipsome",
    "options": [{"id": "Philipsome", "name": "Philipsome", "descr": "brand · Acme L3",
                 "question": "berapa gmv brand Philipsome bulan Juli 2026?"},
                {"id": "Philips", "name": "Philips", "descr": "pakai persis yang saya tulis",
                 "question": "berapa gmv brand Philips bulan Juli 2026? (persis \"Philips\")",
                 "use_exact": true}]}]}
```

* `kind`: `entity` (nama yang cuma MIRIP nilai di data, atau nilai yang ada di lebih dari
  satu kolom), `brand` (mode brand analysis tanpa brand disebut; `term` kosong), `metric`
  (istilah KPI dengan lebih dari satu makna terkurasi, tenant `strict_metrics`).
* `chosen` = `id` opsi yang BENAR-BENAR dipakai giliran itu, dibaca dari yang dijalankan
  (nilai yang difilter SQL governed, brand yang baseline-nya dibaca, makna yang dipenuhi
  query) — bukan dari klaim jawaban. `null` kalau tidak bisa dipastikan.
* tiap `option.question` = pertanyaan yang sama dengan bacaan itu dieja jelas. Tampilkan
  opsi selain `chosen` sebagai koreksi satu klik: kirim `option.question` apa adanya (untuk
  `brand`, kirim dengan `analysis_mode: "brand"`). Opsi `use_exact` menjaga kata user
  persis seperti ditulis; nilai yang dipilih jadi persis sehingga tidak terbuka lagi.

Kapan sebuah nama TIDAK jadi asumsi (saklar `confirm_entities` di `config/business`,
default mati):

* nama yang cocok **persis** dan cuma punya satu arti, termasuk di tengah frasa panjang;
* pertanyaan yang sudah menyebut dimensinya ("gmv **brand** Samsung");
* kata milik skema (tabel, kolom, metrik, business concept);
* nama yang tidak mirip apa pun — "tidak ada datanya" sudah jawaban yang jujur.

Istilah KPI dengan beberapa makna di tabel yang BERBEDA tetap governed: makna-makna itu
dipaksakan bersama, jadi query yang membaca salah satu tabel harus menghitung makna itu
persis seperti dikurasi. `/v1/query` tetap menolak `409 SEMANTIC_AMBIGUOUS` (SQL mentah
tidak punya jawaban untuk menyatakan asumsi), dan konflik otoritas replika tetap `409`.
Field request `confirmation` ({token, choice_id}) masih diterima untuk klien lama tapi
tidak dipakai lagi: token tidak diterbitkan.

#### Status jawaban (`status` + `missing_requirements`)

Setiap response `/v1/chat` sekarang menyatakan apa yang benar-benar DIHASILKAN giliran itu:

| `status` | Arti |
|---|---|
| `complete` | Semua yang diminta pertanyaan (dimensi "per client", bucket "bulanan", top-N, chart) terpenuhi oleh bukti yang dihasilkan |
| `partial` | Ada bukti valid, tapi ada kebutuhan yang TIDAK terpenuhi — namanya ada di `missing_requirements` (mis. `["dimension:client"]`, `["governed_sql_evidence"]`) |
| `no_data` | Query governed jalan tapi tidak ada baris yang memenuhi — itu jawaban, bukan error |
| `awaiting_confirmation` | Giliran berakhir sebagai pertanyaan konfirmasi business, belum ada jawaban analitis |
| `blocked` | Ditolak gatekeeper/governance |
| `error` | Pipeline gagal tanpa hasil analitis yang valid (mis. semua percobaan SQL gagal) |

Kebutuhan diekstrak deterministik dan KONSERVATIF: hanya isyarat struktural yang membuat
kebutuhan ("per client", "top 10 seller", "bulanan", "chart") dan hanya kalau skema
mengenal dimensinya. Menyebut nilai ("brand Philips") itu filter, bukan permintaan
breakdown — jadi jawaban satu-angka yang benar tidak akan terdegradasi jadi `partial`.
Status yang sama tercatat di audit log per jawaban, jadi dashboard yang menghitung
keberhasilan tidak lagi menghitung jawaban kosong/parsial sebagai "ok".

Jawaban diskusi (sapaan, saran umum, penjelasan) yang tidak menjalankan query dan tidak
menyebut angka bisnis berstatus `complete`, dengan `verifier.basis: "general_knowledge"` dan
`confidence: {"level": "general_knowledge", "score": null, "signals": {"general_knowledge": true}}`.
Tampilkan "Pengetahuan umum" / "General knowledge", bukan badge keyakinan: tidak ada angka
yang perlu dinilai. Level lain tetap `high` | `medium` | `low` dengan `score` 0-100.

### `POST /v1/chat`
```bash
curl -X POST "$BASE/v1/chat" \
  -H "X-API-Key: $API_KEY" -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "area mana yang potensial untuk ekspansi gudang?",
    "analysis_mode": "auto"
  }'
```

### Chat/deep research tahan timeout: job + polling

Acme Assistant dan consumer lain harus memakai kontrak ini. Request pertama hanya
memvalidasi identitas, menyimpan payload terenkripsi, dan mengembalikan `202`:

```http
POST /v1/chat/jobs
X-API-Key: sk_...
Authorization: Bearer <JWT end-user>
Content-Type: application/json

{"message":"analisis penyebab SLA turun","analysis_mode":"deep","history":[]}
```

```json
{
  "job_id": "job_...",
  "status": "queued",
  "status_url": "/v1/chat/jobs/job_..."
}
```

Poll setiap 0,7–1,5 detik. Simpan `seq` event terbesar dan kirim sebagai `after`
agar event lama tidak diunduh ulang:

```js
const headers = {
  'Content-Type': 'application/json',
  'X-API-Key': process.env.POROS_API_KEY,
  Authorization: `Bearer ${endUserJwt}`,
}
const started = await fetch(`${POROS_BASE}/v1/chat/jobs`, {
  method: 'POST', headers,
  body: JSON.stringify({ message, history, analysis_mode: 'auto' }),
}).then(r => r.json())

let after = 0
for (;;) {
  const state = await fetch(
    `${POROS_BASE}/v1/chat/jobs/${started.job_id}?after=${after}`,
    { headers },
  ).then(r => r.json())
  for (const event of state.events || []) {
    after = Math.max(after, event.seq || 0)
    renderProgress(event)
  }
  if (state.status === 'succeeded') {
    renderAnswer(state.result)
    break
  }
  if (state.status === 'failed') throw new Error(state.error.message)
  if (state.status === 'cancelled') break
  await new Promise(resolve => setTimeout(resolve, 900))
}
```

Tombol Stop memanggil:

```http
DELETE /v1/chat/jobs/{job_id}
```

Job dan hasilnya berada di PostgreSQL, bukan di koneksi browser atau memori satu pod.
Karena itu refresh halaman, putusnya jaringan, restart gateway, atau respons AI selama
beberapa menit tidak menggagalkan proses. Consumer boleh melanjutkan polling dengan
`job_id` yang sama.

`analysis_mode`:

- `auto` (default) — satu agent chat menjawab semua giliran: model sendiri yang memutuskan
  perlu tidaknya memanggil tool (`run_sql` governed, juga ke datasource lain dalam slice
  business; `describe_table`; `search_docs`; `brand_baseline`). Sapaan dijawab model tanpa
  tool; pertanyaan yang belum jelas dijawab dengan pertanyaan balik dari model. Gatekeeper
  hanya menyaring keamanan. Deep Research TIDAK lagi dipilih otomatis.
- `deep` — paksa agen adaptif melakukan siklus `inspect → act → observe → replan`: model dapat
  mencari katalog, memeriksa schema, menjalankan beberapa SQL governed lintas datasource,
  mencari dokumen/web, lalu diperiksa critic sebelum menyusun jawaban. Tidak ada playbook domain
  atau daftar pertanyaan tetap. Key OpenAI tenant yang sama dipakai untuk LLM dan `web_search`.
  Kualitas diutamakan; lebih lambat/mahal.
- `fast` — sama dengan `auto` (dipertahankan untuk client lama).
- `brand` (alias `client` / `klien`) — paksa **analisa brand**: angka dasar tetap untuk satu
  brand (maks. 2) dan satu bulan. Dasarnya query template inti MBR (scorecard, tren,
  marketplace, produk, penggerak NMV, harga/paket, pembatalan, pembeli, fulfilment, provinsi),
  di-cache per brand + bulan. Di atasnya ditambah query governed + dokumen knowledge base untuk
  pertanyaan itu, lalu jawaban markdown bebas yang bentuknya mengikuti pertanyaan (pertanyaan
  sempit = jawaban pendek; "kenapa" = beberapa kemungkinan penyebab dengan alasan, seberapa
  yakin, penjelasan lain dan cara memastikan, ditulis sebagai uraian, bukan template), plus
  satu baris dasar analisa. Setiap angka dicek ke data per kalimat: kalimat yang angkanya
  tidak ada di data (atau yang menyebut nama teknis) TETAP ada di jawaban dan ditandai;
  angkanya masuk `unverified_figures` (tidak ada lagi putaran perbaikan atau kalimat dibuang).
  Tanpa nama brand di pertanyaan, Poros tidak bertanya balik: agent memilih brand yang
  dibicarakan percakapan (kalau tidak ada, yang tersibuk), menyebutnya di kalimat pertama,
  dan `assumptions` berisi bacaan `kind: "brand"` dengan brand lain sebagai koreksi. Di mode `auto` tidak ada deteksi kata kunci lagi: agent
  memanggil tool `brand_baseline` sendiri bila menilai pertanyaan butuh analisa brand, dan
  jawabannya ditulis dengan cara yang sama (angka dasar ter-cache sama, jawabannya tidak).
  Di mode `brand`, pertanyaan yang sama dalam `BRAND_ANALYSIS_CACHE_HOURS` (default 6)
  mendapat analisa yang sama. Matikan seluruhnya dengan env `BRAND_ANALYSIS=0`.

Governance tidak berubah di semua mode: setiap SQL tetap read-only, tenant/role-scoped,
dan PII tetap dimasking sebelum bukti masuk ke LLM.

**Percakapan multi-turn (conversation memory):** kirim beberapa giliran terakhir di field `history`
supaya pertanyaan lanjutan ("kalau bulan lalu?") nyambung — Poros stateless, aplikasilah yang membawa
konteks (cukup ±20 pesan terakhir; tiap pesan dipotong di 8.000 karakter). Poros memberikannya ke
model sebagai giliran chat asli (user/assistant, PII disamarkan) sebelum pertanyaan sekarang;
giliran yang lebih lama dari jendela masuk sebagai ringkasan:

```json
{
  "message": "kalau bulan lalu bagaimana?",
  "history": [
    {"role": "user",      "content": "total tiket gudang Jakarta bulan ini?"},
    {"role": "assistant", "content": "Ada 40 tiket."}
  ]
}
```
Strateginya diatur admin per tenant (tab Memory): `Buffer + summary` / `Buffer window` /
`Summary only` / `None`, jumlah pesan (`context_window`), dan plafon token (`max_memory_tokens`).

**Auto-memory:** bila diaktifkan admin, Poros otomatis menyimpan catatan jangka panjang tentang
end-user (identitas dari `sub` JWT) dari percakapannya — mis. "PIC gudang Jakarta" — lalu
me-recall-nya di chat berikutnya (`recalled_memory`). Tanpa perubahan apa pun di aplikasimu.

Field respons penting:

| Field | Isi |
|---|---|
| `answer` | Jawaban naratif (bisa berisi blok ```chart / ```artifact untuk divisualisasikan) |
| `sql` | `{ok, safe_sql, columns, rows}` — SQL final tervalidasi + hasilnya (null bila tidak relevan) |
| `documents` | Hit dokumen: `text`, `citation_label` ("file — Slide 3"), `document_id` (untuk link file), `page_label` |
| `citations` | Daftar sitasi ringkas |
| `analysis_mode` | Mode aktual yang dipakai: `deep`, `fast`, atau `brand` |
| `brand_analysis` | Hanya di mode `brand`: `brands`, `period`, `period_label`, `baseline` (per brand: dari cache atau baru, waktu data diambil, status tiap bagian), `flagged` (jumlah kalimat yang angkanya tidak ditemukan pemeriksa angka; kalimatnya TETAP ada di jawaban), `answer_cached`. Sejak 2026-10-30 tidak ada lagi `hypotheses` terstruktur: hipotesis ada di dalam `answer`. Sejak 2026-11-02 `rejected`/`repaired` hilang: tidak ada kalimat yang dibuang atau ditulis ulang |
| `coverage` | Status kelengkapan proses riset; `missing=[]` bila critic menyetujui jawaban berbasis bukti terbaik yang tersedia |
| `research` | Metadata Deep Research: status critic, alasan berhenti, jumlah putaran/tool, trace tindakan, dan `remaining_gaps` yang masih diperlukan untuk keputusan bisnis final |
| `hypotheses` | Hypothesis ledger Deep Research: `H1/H2`, pernyataan, sudut analisis, scope, tes falsifikasi, dampak keputusan, status, evidence ID pendukung/penyangkal, asumsi, dan tes berikutnya |
| `datasources_used` | Datasource SQL yang benar-benar menghasilkan bukti |
| `web_sources` | Sumber web resmi yang dipakai Deep Analysis (`title`, `url`, `label`) |
| `recalled_memory` | Memory end-user yang terpakai |
| `verifier` | `{ok, issues, checks, unverified_figures}` — hasil pemeriksaan grounding/sitasi/angka. Verifier hanya MELAPOR: jawaban tidak pernah ditulis ulang (sejak 2026-11-02 tidak ada lagi `repaired`) |
| `unverified_figures` | Angka di jawaban yang tidak bisa dicocokkan dengan data giliran itu (mis. `["987.654"]`; `[]` bila semua cocok). Angkanya tetap ada di teks; tampilkan catatan kecil ("2 angka tidak bisa dicocokkan dengan data"). Confidence ikut turun: satu angka = paling tinggi `medium`, tiga atau lebih = `low` |
| `usage` | `{model, prompt_tokens, completion_tokens, cost}` |
| `trace_id` | Untuk korelasi dengan audit log admin |
| `blocked` | `true` bila gatekeeper memblokir (prompt injection dsb.) |

### `POST /v1/feedback`

Kirim setelah user menilai hasil `/v1/chat`. Gunakan `trace_id` dari respons; jangan
kirim ulang teks pertanyaan atau jawaban karena Poros mengambil keduanya dari audit trail
tenant yang sama.

```bash
curl -X POST "$BASE/v1/feedback" \
  -H "X-API-Key: $API_KEY" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "trace_id": "TRACE_ID_DARI_CHAT",
    "rating": "unhelpful",
    "reason": "incorrect",
    "comment": "Angka total berbeda dari dashboard sumber."
  }'
```

`rating`: `helpful` atau `unhelpful`. `reason` opsional: `incorrect`, `incomplete`,
`irrelevant`, `unclear`, `outdated`, atau `other`. Pengiriman ulang oleh user yang sama
untuk `trace_id` yang sama memperbarui feedback lama, bukan membuat duplikat.

### `POST /v1/chat_stream` (SSE)
Kirim body sama seperti `/chat`; baca `text/event-stream`. Setiap event = satu baris `data: {json}`:
```
data: {"type":"status","text":"Menulis & menjalankan SQL (governed)…"}
data: {"type":"status","text":"Menyusun jawaban…"}
data: {"type":"done","result":{ …payload /chat lengkap… }}
```
`type` = `status` (progress untuk UI) · `done` (hasil akhir di `result`) · `error`.

**Stop/cancel:** tidak ada endpoint cancel terpisah. Batalkan request HTTP
`/v1/chat_stream` yang sedang aktif (di browser gunakan `AbortController`). Poros
mendeteksi koneksi stream terputus lalu menghentikan tahap pipeline berikutnya dan
mencatat status `CANCELLED`, bukan audit `answer`.

```js
const controller = new AbortController()
const response = await fetch(`${POROS_BASE}/v1/chat_stream`, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-API-Key': POROS_KEY,
    Authorization: `Bearer ${jwt}`,
  },
  body: JSON.stringify({ message, user_id, history, analysis_mode: 'auto' }),
  signal: controller.signal,
})

// Dipanggil hanya saat user menekan tombol Stop:
controller.abort()
```

Jika aplikasi consumer mem-proxy Poros melalui backend-nya sendiri, putusnya koneksi
browser harus diteruskan dengan membatalkan request upstream ke Poros. Mengganti menu
SPA tidak boleh meng-abort controller apabila proses memang harus tetap berjalan.
Panggilan provider yang sudah telanjur berjalan saat Stop ditekan mungkin tetap
terhitung oleh provider; Poros mencegah semua langkah dan biaya lanjutan.

### `POST /v1/retrieve`
```json
{"query": "prosedur retur barang", "top_k": 4}
```
Respons: `results[]` berisi `text`, `score`, `rerank_score`, `citation`, `citation_label`,
`page_label`, `document_id`, `filename`.

### `GET /v1/documents/{document_id}/file`
Header sama (API key + JWT). Mengembalikan file asli (PDF/PPTX/…) dengan `Content-Disposition`.
- `403 DOC_ACL_DENIED` → role user tidak diizinkan membuka dokumen itu.
- `404 NO_FILE` → dokumen di-upload sebelum fitur penyimpanan file; minta admin upload ulang.

### Laporan MBR (Monthly Business Review)
Paket laporan (deck PPTX, workbook XLSX, EVIDENCE + OPEN-ITEMS markdown, data JSON) untuk satu
brand dan satu bulan, dibuat sebagai job di belakang layar. Laporan hanya bisa dilihat oleh
API key + end user (`sub` JWT) yang membuatnya.
- `POST /v1/reports/mbr` `{"brand": "ACMESMILE", "period": "2026-09", "business": "MBR"}` → `202
  {report_id, status, status_url}`. Input salah → `400`; business tidak diberikan ke role → `409/403`.
- `GET /v1/reports?limit=20` → `{reports: [...]}` laporan milik pemanggil, terbaru dulu: `id`,
  `status` (queued|running|succeeded|failed|cancelled), `progress`, `phase`, `brand`, `period`,
  `business`, `delivery_status`, `error`, `files[]` (`name`, `mime`, `size`) bila sudah selesai.
- `GET /v1/reports/{id}` → status + `events` + `files[]` (poll tiap beberapa detik sampai selesai).
- `GET /v1/reports/{id}/files/{name}` → unduh satu file (`Content-Disposition: attachment`).
- `GET /v1/reports/mbr/brands?business=MBR` → `{brands: [{name, orders, last_month, configured}],
  source, since}` = brand yang ada di data order business itu (13 bulan terakhir, terbanyak dulu)
  plus brand di pengaturan MBR; untuk combo box, supaya user memilih, bukan mengetik. Query lewat
  govern(), di-cache 6 jam per tenant/role/business; lewat dari itu daftar lama tetap langsung
  dikirim sambil diperbarui di belakang (hanya permintaan pertama setelah restart yang menunggu,
  bisa ~10 detik di BigQuery — aman dipanggil saat aplikasi dibuka). Tidak kena rate limit
  pertanyaan. Tabel order tidak terjangkau = hanya brand dari pengaturan, `source: "none"` +
  `note`. `GET /v1/reports` tetap menyebut `brand`/`period` laporan yang dibatalkan/gagal.

#### File ekspor sebagai input laporan (sementara)
User bisa melampirkan file ekspor marketplace (zip/csv/xlsx: order, product performance,
affiliate, LIVE, video, settlement, inventory, PO service level, SRP master) untuk laporannya.
File dikirim per potongan, disimpan terenkripsi, dibaca oleh job laporan, dan DISIMPAN sampai
6 jam setelah upload (`REPORT_INPUT_TTL_SEC`) supaya laporan bisa dibuat ulang (setelah
perbaikan, atau bulan lain) tanpa melampirkan lagi; laporan yang dimulai menjelang batas itu
tetap memegang file-nya minimal 1 jam lagi. Sesudahnya file dihapus. Identitas pembeli (nama, username, telepon, alamat, resi, catatan)
tidak pernah ikut dibaca. Hanya API key + end user yang membuat input yang bisa memakainya.
- `POST /v1/reports/inputs` → `201 {input_id: "inp_…", expires_at, chunk_bytes: 4194304,
  max_file_bytes, max_total_bytes, max_files}`. Maks 3 input aktif per user: kalau penuh, set
  file tertua yang sudah dipakai laporan dan tidak sedang dibaca laporan mana pun dihapus untuk
  memberi tempat; kalau semuanya masih diupload/menunggu/dibaca = `429 TOO_MANY_INPUTS`.
- `PUT /v1/reports/inputs/{input_id}/files/{file_no}/chunks/{chunk_no}` body = byte mentah
  (`application/octet-stream`, ≤ `chunk_bytes`), header `X-File-Name` (URL-encoded, sama di setiap
  potongan file itu). Potongan boleh datang dalam urutan apa pun (kirim beberapa sekaligus supaya
  cepat; consumer-app mengirim 3), potongan yang dikirim ulang menggantikan yang lama. Error:
  `400 BAD_CHUNK|BAD_FILE_NAME|BAD_FILE_TYPE`, `409 INPUT_CLOSED`, `413 INPUT_TOO_LARGE`,
  `404 INPUT_NOT_FOUND`.
- `POST /v1/reports/inputs/{input_id}/close` → `{input_id, status: "closed", expires_at,
  files: [{file_no, name, size, recognized: [{path, kind, marketplace, month}], ignored: [{path,
  reason}]}], summary: {files, recognized, ignored, kinds, months, latest_month}}` = cek cepat dari
  nama file (isi dibaca penuh di job laporan). `409 INPUT_INCOMPLETE` bila ada file atau potongan
  yang hilang (file 0..n-1, potongan 0..k-1 per file).
- `DELETE /v1/reports/inputs/{input_id}` → hapus sekarang.
- `POST /v1/reports/mbr` + `"inputs": "inp_…"` → laporan memakai file itu (boleh dipakai
  lagi untuk laporan berikutnya selama belum kedaluwarsa; `409 INPUT_NOT_CLOSED` bila belum
  di-close, `404 INPUT_NOT_FOUND` bila sudah kedaluwarsa atau dihapus). Halaman yang
  biasanya membaca tabel warehouse yang sama (LIVE/video, affiliate, product funnel, settlement,
  inventory) membaca file untuk laporan itu; halaman baru: GMV dengan ongkir per marketplace +
  cocokkan dengan warehouse, waktu kirim per marketplace/provinsi, gudang, PO fill rate, harga
  jual vs SRP, dan lampiran "Attached files" (file mana yang dibaca/diabaikan dan kenapa). Hasil
  job memuat `uploads {files_used, files_ignored, tables, replaced, error}`.

## 3. Kode error

| HTTP | `code` | Arti / aksi |
|---|---|---|
| 401 | `JWT_REQUIRED` / `JWT_INVALID` | Token hilang / signature salah / kedaluwarsa → periksa key & jam server |
| 401 | `JWT_ASYMMETRIC_REQUIRED` / `JWT_ISSUER_AUDIENCE_REQUIRED` | Config JWT tenant belum production-grade |
| 403 | `ROLE_NOT_FOUND` | Role di JWT tak dikenal & tak ter-map → hubungi admin (nilai tercatat di console) |
| 403 | `CLAC_DENIED` / `ROLE_TABLE_DENIED` / `MODEL_NOT_FOUND` | Query menyentuh kolom PII / tabel di luar hak role → bukan bug, itu governance |
| 403 | `BUSINESS_TABLE_DENIED` | Tabel ada di database itu, tapi di luar business yang dipilih → tanya di business yang mencakupnya |
| 409 | `BUSINESS_REQUIRED` | `/v1/query` tanpa business padahal tenant mewajibkan → pilih dari `businesses` di body error (di chat ini jadi pertanyaan, bukan error) |
| 409 | `BUSINESS_NOT_FOUND` | Nilai `business` tidak dikenal untuk role ini → jangan retry tanpa business, itu tidak melebarkan akses |
| 409 | `BUSINESS_NOT_GRANTED` | Role belum diberi business apa pun → hubungi admin (Knowledge Base di console) |
| 409 | `SEMANTIC_AMBIGUOUS` | (tenant strict, `/v1/query`) istilah KPI punya >1 makna terkurasi → `concepts` di body berisi pilihannya; di chat ini jadi asumsi `kind: "metric"` |
| 409 | `SEMANTIC_AUTHORITY_CONFLICT` | Konsep yang sama dikurasi di >1 datasource tanpa kontrak replika yang sah → hanya admin yang bisa membereskan (tandai satu primary) |
| 400 | `READ_ONLY` | Hanya SELECT yang diizinkan |
| 400 | `COST_LIMIT` | (BigQuery) estimasi scan melebihi batas → persempit query (filter tanggal) |
| 400 | `NO_DATASOURCE` | Tenant belum punya database |
| 429 | `RATE_LIMITED` | Terlalu banyak request → backoff + retry (default 120/menit per key) |
| 429 | `BUDGET_EXCEEDED` | Hard budget bulanan habis; jangan retry sampai budget dinaikkan/reset |

Bentuk error seragam: `{"detail": {"code": "...", "message": "..."}}`.

## 4. Praktik yang disarankan

1. **Simpan API key & private signing key di server/secret manager** — jangan pernah di frontend.
2. **Proxy-kan** panggilan Poros lewat backend-mu (browser → backend-mu → Poros); JWT
   ditandatangani di backend.
3. Pakai `/v1/chat/jobs` + polling untuk UI chat; tampilkan `events` sebagai progres.
4. Beri timeout pendek (mis. 5–10 detik) pada setiap request enqueue/poll. Durasi deep
   research boleh beberapa menit karena tidak lagi berada di dalam satu request HTTP.
5. Tangani `429` dengan retry + jitter; tangani `403 ROLE_NOT_FOUND` dengan pesan "hubungi admin".
6. Render `citation_label` sebagai link ke `GET /v1/documents/{document_id}/file` supaya user bisa
   memverifikasi sumber.
7. SDK siap pakai tersedia di folder `sdk/` (JS & Python) bila tidak ingin menulis HTTP manual.
