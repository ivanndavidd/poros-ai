# Consumer SDKs

Thin clients for the governed consumer API (`/v1/*`). A consumer app needs **only an API key** —
every call is screened, governed (allowlist + RLAC/CLAC), metered and logged server-side.

## JavaScript (`sdk/js/client.js`) — browser or Node 18+
```js
import { AdminClient } from "./sdk/js/client.js"

const client = new AdminClient("http://localhost:8000", "sk_xxx")
const jwt = "<END_USER_JWT>"
const res = await client.chat("area mana yang potensial untuk ekspansi gudang?", {
  userId: "u1",
  analysisMode: "deep",
  jwt,
})
console.log(res.answer)        // grounded, cited answer
console.log(res.citations)     // ["refund_sop.md#chunk0", ...]
console.log(res.sql?.safe_sql) // the validated SQL behind any numbers
console.log(res.coverage)      // evidence angles that succeeded / are still missing

await client.feedback(res.trace_id, {
  rating: "unhelpful",
  reason: "incorrect",
  comment: "Angka total berbeda dari dashboard sumber.",
  jwt,
})
```

## Python (`sdk/python/client.py`) — zero dependencies
```python
from sdk.python.client import Client

client = Client("http://localhost:8000", "sk_xxx")
jwt = "<END_USER_JWT>"
res = client.chat(
    "area mana yang potensial untuk ekspansi gudang?",
    user_id="u1",
    jwt=jwt,
    analysis_mode="deep",
)
print(res["answer"], res["citations"])

client.feedback(res["trace_id"], "helpful", jwt=jwt)

# raw governed SQL
print(client.query("SELECT region, SUM(amount) FROM orders GROUP BY region"))

# doc retrieval with citations
print(client.retrieve("what is the refund policy?", top_k=3))
```

## Methods
| Method | Endpoint | Returns |
|--------|----------|---------|
| `chat(message, user_id?, datasource_id?, analysis_mode?, history?, business?, confirmation?)` | `POST /v1/chat` | agent answer + `sql`, `coverage`, `datasources_used`, `web_sources`, `citations`, `verifier`, `usage` |
| `feedback(trace_id, rating, reason?, comment?)` | `POST /v1/feedback` | menyimpan/memperbarui feedback user untuk satu jawaban |
| `query(sql, datasource_id?, business?)` | `POST /v1/query` | `safe_sql`, `columns`, `rows` |
| `retrieve(query, top_k?, datasource_id?)` | `POST /v1/retrieve` | `results[]` with `citation` + `score` |

`business` (id or name, e.g. `"3P"`) picks the knowledge slice: which tables are reachable **and**
which curation describes them, because one table can mean different things to two business lines.
Optional by default. When a tenant makes it mandatory:

* **chat** comes back with a QUESTION rather than an error — status `200`, `answer` holds the
  question and `confirmation.options` holds the choices. Show them, resend
  `confirmation.pending_question` with `business` set, and keep the pick for the rest of the
  conversation. Every answer also reports the slice it used in `business`.
* **`query`** raises `ApiError("BUSINESS_REQUIRED", ...)`, whose `.businesses` is that same list.

The business is the only thing chat still asks. A name the data only RESEMBLES ("Philips" where
the values say "Philipsome"), a brand analysis with no brand named, and a KPI phrase with more than
one curated meaning (strict-metrics tenants) are answered with an ASSUMPTION instead: the answer's
first sentence says which reading it took, and `result.assumptions` lists each one as
`{kind: "entity"|"brand"|"metric", term, chosen, options}`. `chosen` is the option the turn actually
used (read from the governed SQL or the brand baseline it ran), or null. Every option carries a
ready-made `question`, so a correction is just `chat(option.question)` (a brand one with
`analysis_mode="brand"`); the `use_exact: true` option keeps the word exactly as typed. The
`confirmation` argument is still accepted for older clients and ignored. A business question-turn
carries `status == "awaiting_confirmation"`, so dashboards can tell questions from answers.

Every finished chat result also states what it delivered: `status` is one of `complete`,
`partial`, `no_data`, `blocked`, `error` (plus `awaiting_confirmation` above), and a `partial`
result names its gaps in `missing_requirements` (e.g. `["dimension:client"]` when "GMV per client"
came back without the client breakdown). Zero governed-SQL evidence is never `complete`.
`unverified_figures` lists the figures in the answer the verifier could not match to the data
that turn used (empty when all of them matched). Poros never rewrites or cuts the answer for
them: show a small note ("2 figures could not be matched to the data") next to it.

Nothing guesses the business for you: guessing it is the failure this layer exists to prevent, and a
guess wrapped in a confirmation is still a guess. See
[`docs/consumer-api.md`](../docs/consumer-api.md).

Errors raise `ApiError(code, message, status)` (e.g. `CLAC_DENIED`, `MODEL_NOT_FOUND`,
`BUSINESS_TABLE_DENIED`); `.businesses` is filled on the `BUSINESS_*` refusals.
