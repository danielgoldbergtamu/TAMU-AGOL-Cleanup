# File Name: agol_db.py
# Description: The pipeline's database: one DuckDB file, and where the pipeline keeps its other files.
# Date: 2026-10-20
#
# Replaces SQL Server (docs/data-model.md, section 7). DuckDB allows one writer at a time: a pipeline step opens
# the file read-write, and the web app opens it read-only. Every location can be set in the environment, so the
# same code runs on a workstation and in a container:
#
#   AGOL_DUCKDB       the database file             (default data/agol.duckdb)
#   AGOL_REPORTS_DIR  run folders and report files  (default reports/)
#   AGOL_CONFIG_DIR   reviewed lookup tables: department-to-unit, degree-to-department, college-level names
#                     (default private/config/; organization data, so never in the public repository)

import os
import re

import duckdb
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def db_path():
    return os.environ.get("AGOL_DUCKDB") or os.path.join(SCRIPT_DIR, "data", "agol.duckdb")


def reports_dir():
    return os.environ.get("AGOL_REPORTS_DIR") or os.path.join(SCRIPT_DIR, "reports")


def config_dir():
    return os.environ.get("AGOL_CONFIG_DIR") or os.path.join(SCRIPT_DIR, "private", "config")


def connect(read_only=False, path=None):
    path = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return duckdb.connect(path, read_only=read_only)


def q(name):
    """A quoted identifier (table and column names contain spaces, dashes and #)."""
    return '"' + str(name).replace('"', '""') + '"'


def exists(con, name):
    return con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0] > 0


def newest(con, prefix):
    """Newest dated table whose name is prefix + a date, e.g. newest(con, 'OrganizationMembers_')."""
    names = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_name LIKE ?",
                                       [prefix + "20%"]).fetchall() if re.fullmatch(re.escape(prefix) + r"\d{4}_\d{2}_\d{2}", r[0])]
    return max(names) if names else None


def tables_like(con, pattern):
    """Table names matching a regular expression, sorted."""
    return sorted(r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall() if re.fullmatch(pattern, r[0]))


def read(con, sql, params=None):
    return con.execute(sql, params or []).df()


def write(con, name, df, mode="replace", types=None):
    """Write a DataFrame to a table. mode: replace (the table becomes df) or append (columns matched by name).
    types: optional {column: DuckDB type} to cast to, e.g. {"updated_date": "TIMESTAMP"}."""
    con.register("_frame", df)
    try:
        cols = ", ".join(f"CAST({q(c)} AS {types[c]}) AS {q(c)}" if types and c in types else q(c) for c in df.columns)
        if mode == "replace" or not exists(con, name):
            con.execute(f"CREATE OR REPLACE TABLE {q(name)} AS SELECT {cols} FROM _frame")
        else:
            con.execute(f"INSERT INTO {q(name)} BY NAME SELECT {cols} FROM _frame")
    finally:
        con.unregister("_frame")


def columns(con, table):
    """[(name, DuckDB type)] in column order."""
    return [(r[0], r[1]) for r in con.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
        [table]).fetchall()]
