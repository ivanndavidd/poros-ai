"""Fail-fast deployment preflight for Poros production configuration.

Run from ``backend/`` with the exact environment and database identity that the
container will use. No secret value is printed.
"""
from app import security, store


def main():
    security.startup_check()
    store.init()
    security.post_store_check()
    print("OK: Poros production environment, PostgreSQL, RLS, and tenant JWT checks passed.")


if __name__ == "__main__":
    main()
