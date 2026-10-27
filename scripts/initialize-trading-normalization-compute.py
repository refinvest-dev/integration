"""Apply the real Compute migrations atomically over one integration-only connection."""
import argparse
import os
from pathlib import Path
import time

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

parser = argparse.ArgumentParser()
parser.add_argument("--compute-path", type=Path, required=True)
parser.add_argument("--schema", required=True)
args = parser.parse_args()
settings = conninfo_to_dict(os.environ["REFINVEST_DATABASE_URL"])
if settings.get("dbname") != "refinvest_integration" or settings.get("host") not in {"localhost", "127.0.0.1", "::1"}:
    raise SystemExit("Integration-only database required")
if not args.schema.startswith("tnorm_") or not args.schema[6:].isalnum():
    raise SystemExit("Invalid runtime schema")
for attempt in range(3):
    try:
        connection = psycopg.connect(**settings, autocommit=True)
        break
    except psycopg.OperationalError as error:
        if attempt == 2 or not any(code in str(error) for code in ("Bad address", "10014")):
            raise SystemExit("Integration database connection failed") from None
        time.sleep(0.5)
with connection:
    if connection.execute("select current_database()").fetchone()[0] != "refinvest_integration":
        raise SystemExit("Integration database identity mismatch")
    connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(args.schema)))
    connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(args.schema)))
    connection.execute("CREATE TABLE IF NOT EXISTS integration_schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    migrations = sorted((args.compute_path / "migrations").glob("*.sql"))
    if not migrations:
        raise SystemExit("Compute migrations unavailable")
    for path in migrations:
        if connection.execute("select 1 from integration_schema_migrations where filename=%s", (path.name,)).fetchone():
            continue
        with connection.transaction():
            connection.execute(path.read_text(encoding="utf-8"), prepare=False)
            connection.execute("insert into integration_schema_migrations(filename) values (%s)", (path.name,))
        print("APPLY " + path.name)
print("Compute migrations ready in refinvest_integration/" + args.schema)
