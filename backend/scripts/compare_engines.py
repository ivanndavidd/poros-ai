"""Ask the same questions through both chat engines and write the outcomes side by side.

The agentic engine (config/chat engine=agentic) lets the model read the question and the
Knowledge Base itself; the classic one picks business, values and tables with fixed rules
first. Before a tenant switches, run its real questions through both and read the CSV:
answer, business used, plan (tool steps), the SQL, status, confidence, tokens, cost and
time per engine.

Runs the real pipeline with the tenant's real models and warehouse (it costs what the
questions cost, twice). Nothing is changed: the tenant's engine setting is not touched
(each run passes the engine explicitly), but every turn is audited and metered like any
chat turn. Run it where the backend runs (the pod: the warehouse identity lives there).

  cd backend
  python scripts/compare_engines.py --tenant "Acme Assistant" --role analyst \
      --questions questions.txt --out compare.csv [--business 3P] [--user compare-script]

questions.txt: one question per line; blank lines and lines starting with # are skipped.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENGINES = ("classic", "agentic")
FIELDS = ("question", "engine", "seconds", "status", "business", "confidence", "plan",
          "sql", "rows", "prompt_tokens", "completion_tokens", "cost", "answer", "error")


def _tenant_id(store, ref):
    for tenant in store.list_tenants():
        if ref in (tenant["id"], tenant["name"]):
            return tenant["id"]
    raise SystemExit(f"no tenant {ref!r}")


def _questions(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _row(question, engine, seconds, out=None, error=""):
    out = out or {}
    usage, sql = out.get("usage") or {}, out.get("sql") or {}
    return {"question": question, "engine": engine, "seconds": round(seconds, 1),
            "status": out.get("status", "error" if error else ""),
            "business": (out.get("business") or {}).get("name", ""),
            "confidence": (out.get("confidence") or {}).get("level", ""),
            "plan": " > ".join(out.get("plan") or []),
            "sql": sql.get("safe_sql") or sql.get("generated_sql") or "",
            "rows": len(sql.get("rows") or []),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cost": usage.get("cost", 0.0), "answer": (out.get("answer") or "")[:4000],
            "error": error}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tenant", required=True, help="tenant id or name")
    parser.add_argument("--role", required=True, help="tenant role the questions run as")
    parser.add_argument("--questions", required=True, help="text file, one question per line")
    parser.add_argument("--out", required=True, help="CSV to write")
    parser.add_argument("--business", default=None, help="pin a business (both engines)")
    parser.add_argument("--user", default="compare-engines", help="end-user id for audit")
    args = parser.parse_args()

    from app import store
    from app.agent.orchestrator import run_agent
    store.init()
    principal = {"tenant_id": _tenant_id(store, args.tenant), "role": args.role}
    rows = []
    for question in _questions(args.questions):
        for engine in ENGINES:
            started = time.monotonic()
            try:
                out = run_agent(principal, question, user_id=args.user,
                                business=args.business, engine=engine)
                rows.append(_row(question, engine, time.monotonic() - started, out))
            except Exception as exc:          # one failing turn must not end the comparison
                rows.append(_row(question, engine, time.monotonic() - started,
                                 error=f"{type(exc).__name__}: {str(exc)[:300]}"))
            print(f"[{engine}] {rows[-1]['status'] or 'error'} "
                  f"{rows[-1]['seconds']}s ${rows[-1]['cost']} :: {question[:70]}")
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
