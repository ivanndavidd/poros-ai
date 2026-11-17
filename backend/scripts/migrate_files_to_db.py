"""One-shot: move original uploaded documents from the pod disk into Postgres.

Why: `store.FILES_DIR` sits on a ReadWriteOnce PVC, which pins the deploy to ONE
replica (a second pod cannot attach the volume, and an upload on pod A answers
NO_FILE on pod B). With the originals in the `document_files` table the app is
stateless and can scale horizontally; the PVC can then be dropped.

What it does, per `documents` row whose file_path is a filesystem path:
  1. read the file (already an AES-GCM envelope, or legacy plaintext — then it is
     encrypted first, same as the at-rest migration did);
  2. insert the envelope into `document_files`;
  3. point file_path at the ref "db:<document_id>";
  4. VERIFY round-trip: `store.read_document_file` must return the same plaintext
     as the disk file — only then does the row's transaction commit.
The disk file itself is LEFT IN PLACE (rollback = repoint file_path; reads are
ref-driven so untouched rows keep working either way). Idempotent: "db:" rows and
rows whose file is already migrated are skipped; missing disk files are reported,
not failed — those documents never had a downloadable original anyway (NO_FILE).

Run WHERE THE DISK IS VISIBLE (the pod, or a host sharing DOCS_FILES_DIR):
  kubectl -n <ns> exec deploy/poros -- python -m scripts.migrate_files_to_db            # dry-run
  kubectl -n <ns> exec deploy/poros -- python -m scripts.migrate_files_to_db --apply

Order of operations for going stateless:
  1. deploy a build with this script + FILES_BACKEND support (env still unset/disk);
  2. run this script with --apply on the pod;
  3. set FILES_BACKEND=db (new uploads go straight to Postgres);
  4. drop the poros-documents PVC + its volumeMounts, raise replicas.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # backend/ on sys.path
from app import document_crypto, store             # noqa: E402


def _write_row(row, envelope: bytes, plaintext: bytes) -> str | None:
    """Move one original into document_files; returns an error string, or None on success
    (success includes the round-trip through the real read path)."""
    c = store._c()
    try:
        c.execute("DELETE FROM document_files WHERE document_id=? AND tenant_id=?",
                  (row["id"], row["tenant_id"]))
        c.execute("INSERT INTO document_files VALUES(?,?,?)",
                  (row["id"], row["tenant_id"], envelope))
        c.execute("UPDATE documents SET file_path=?, file_size=? WHERE id=? AND tenant_id=?",
                  (store._DB_FILE_REF + row["id"],
                   row["file_size"] or len(plaintext), row["id"], row["tenant_id"]))
        c.commit()
    except Exception as exc:
        c.rollback()
        return str(exc)
    finally:
        c.close()
    if store.read_document_file(row["tenant_id"], row["id"]) != plaintext:
        return "round-trip mismatch"
    return None


def _report(apply_mode: bool, moved: int, already: int, missing: list, failed: list) -> None:
    label = "moved" if apply_mode else "would move"
    print(f"\n{label}: {moved} · already in db: {already} · "
          f"missing on disk: {len(missing)} · failed: {len(failed)}")
    for doc_id, name, ref in missing:
        print(f"  MISSING {doc_id} ({name}): {ref}")
    for doc_id, name, err in failed:
        print(f"  FAILED  {doc_id} ({name}): {err}")
    if not apply_mode:
        print("\nDry-run only — rerun with --apply to write.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry-run report)")
    args = parser.parse_args()

    store.init()
    c = store._c()
    rows = c.execute(
        """SELECT id, tenant_id, filename, file_path, file_size FROM documents
           WHERE file_path IS NOT NULL AND file_path<>''"""
    ).fetchall()
    c.close()

    moved, already, missing, failed = 0, 0, [], []
    for row in rows:
        ref = row["file_path"]
        if ref.startswith(store._DB_FILE_REF):
            already += 1
            continue
        if not os.path.exists(ref):
            missing.append((row["id"], row["filename"], ref))
            continue
        with open(ref, "rb") as source:
            data = source.read()
        plaintext = document_crypto.decrypt(data, row["tenant_id"])
        envelope = (data if document_crypto.encrypted(data)
                    else document_crypto.encrypt(plaintext, row["tenant_id"]))
        if not args.apply:
            print(f"DRY-RUN would move {row['id']} ({row['filename']}, {len(plaintext)}B)")
            moved += 1
            continue
        error = _write_row(row, envelope, plaintext)
        if error:
            failed.append((row["id"], row["filename"], error))
            continue
        print(f"moved {row['id']} ({row['filename']}, {len(plaintext)}B)")
        moved += 1

    _report(args.apply, moved, already, missing, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
