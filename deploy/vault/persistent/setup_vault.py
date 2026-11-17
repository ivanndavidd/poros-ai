"""Setup + auto-unseal untuk Vault PERSISTEN lokal (stdlib saja, tanpa vault CLI).

Idempotent — aman dijalankan berulang:
  1. tunggu Vault merespon;
  2. kalau belum di-init  -> operator init (1 unseal key, cukup untuk lokal),
     simpan unseal key + root token ke .vault-init.json (JAGA FILE INI);
  3. kalau sealed         -> unseal pakai key dari file itu;
  4. pastikan KV v2 ter-mount di `secret/` + tulis policy `tenantos`;
  5. tulis vault.env.bat (dipakai otomatis oleh run_backend_vault.bat).
"""
import json, os, sys, time
import urllib.request, urllib.error

ADDR = (os.getenv("VAULT_ADDR") or "http://localhost:8200").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
INIT_FILE = os.path.join(HERE, ".vault-init.json")
ENV_BAT = os.path.join(HERE, "vault.env.bat")
POLICY_FILE = os.path.join(HERE, "..", "tenantos-policy.hcl")


def call(method, path, body=None, token=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    req = urllib.request.Request(ADDR + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:            # 501/503 dari sys/health tetap bawa body JSON
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except Exception:
            return e.code, {}


def wait_health(seconds=45):
    for _ in range(seconds):
        try:
            return call("GET", "/v1/sys/health")
        except Exception:
            time.sleep(1)
    print(f"ERROR: Vault tidak merespon di {ADDR} setelah {seconds}s. Container jalan?")
    sys.exit(1)


_, h = wait_health()
print(f"health: initialized={h.get('initialized')} sealed={h.get('sealed')} version={h.get('version')}")

init = None
if os.path.exists(INIT_FILE):
    with open(INIT_FILE) as f:
        init = json.load(f)

if not h.get("initialized"):
    print("Vault belum di-init -> operator init (1 unseal key, mode lokal)...")
    # init raft bisa lambat; kalau respons ini hilang, kunci hilang selamanya -> timeout panjang
    code, resp = call("PUT", "/v1/sys/init", {"secret_shares": 1, "secret_threshold": 1}, timeout=90)
    if code != 200 or not resp.get("root_token"):
        print(f"ERROR init (HTTP {code}): {resp}")
        sys.exit(1)
    init = resp
    with open(INIT_FILE, "w") as f:
        json.dump(init, f, indent=2)
    print(f"  unseal key + root token disimpan: {INIT_FILE}")
    print("  JAGA FILE INI - siapa pun yang memegangnya bisa membuka seluruh Vault.")

_, h = call("GET", "/v1/sys/health")
if h.get("sealed"):
    if not init:
        print(f"ERROR: Vault sealed tapi {INIT_FILE} tidak ada - tidak bisa unseal tanpa key.")
        sys.exit(1)
    key = (init.get("keys_base64") or init.get("keys"))[0]
    code, resp = call("PUT", "/v1/sys/unseal", {"key": key})
    if resp.get("sealed", True):
        print(f"ERROR unseal (HTTP {code}): {resp}")
        sys.exit(1)
    print("Vault di-unseal.")
else:
    print("Vault sudah unsealed.")

# node raft perlu beberapa detik untuk terpilih jadi ACTIVE setelah unseal;
# operasi mount/policy sebelum itu gagal 500 "local node not active".
for _ in range(30):
    code, _h = call("GET", "/v1/sys/health")
    if code == 200:
        break
    time.sleep(1)
else:
    print("WARNING: node belum active setelah 30s - mount/policy bisa gagal, coba jalankan ulang.")

token = (init or {}).get("root_token")
if not token:
    print("WARNING: root token tidak diketahui (file init hilang?) - lewati mount/policy/env.")
    sys.exit(0)

code, mounts = call("GET", "/v1/sys/mounts", token=token)
data = mounts.get("data", mounts)
if "secret/" not in data:
    code, resp = call("POST", "/v1/sys/mounts/secret", {"type": "kv", "options": {"version": "2"}}, token=token)
    print("mount KV v2 'secret/':", "OK" if code in (200, 204) else f"ERROR {code} {resp}")
else:
    print("mount KV v2 'secret/': sudah ada")

try:
    with open(POLICY_FILE) as f:
        policy = f.read()
    code, _ = call("PUT", "/v1/sys/policies/acl/tenantos", {"policy": policy}, token=token)
    print("policy 'tenantos':", "OK" if code in (200, 204) else f"ERROR {code}")
except FileNotFoundError:
    print("policy file tidak ditemukan - lewati (root token tidak membutuhkannya)")

with open(ENV_BAT, "w") as f:
    f.write('@echo off\nset "SECRETS_BACKEND=vault"\nset "VAULT_ADDR=%s"\nset "VAULT_TOKEN=%s"\n' % (ADDR, token))
print(f"env launcher ditulis: {ENV_BAT}")
print()
print("ENV untuk backend (run_backend_vault.bat memakai ini otomatis):")
print("  set SECRETS_BACKEND=vault")
print(f"  set VAULT_ADDR={ADDR}")
print(f"  set VAULT_TOKEN={token[:8]}... (lengkap di vault.env.bat)")
print("Status: SIAP.")
