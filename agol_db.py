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
#   AGOL_OUTPUT_DIR   the web app's read-only copy of the database, and last_run.json (default data/)

import os
import re

import duckdb
import pandas as pd
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# The workstation's settings (.env beside this file, git-ignored), for every script that imports this module, so
# none can forget them and sign in some other way (21 October 2026: two tools did, and fell back to the
# maintainer's own administrator sign-in). Settings already in the environment win; the container has no .env.
load_dotenv(os.path.join(SCRIPT_DIR, ".env"), override=False)


def db_path():
    return os.environ.get("AGOL_DUCKDB") or os.path.join(SCRIPT_DIR, "data", "agol.duckdb")


def reports_dir():
    return os.environ.get("AGOL_REPORTS_DIR") or os.path.join(SCRIPT_DIR, "reports")


def config_dir():
    return os.environ.get("AGOL_CONFIG_DIR") or os.path.join(SCRIPT_DIR, "private", "config")


def output_dir():
    return os.environ.get("AGOL_OUTPUT_DIR") or os.path.join(SCRIPT_DIR, "data")


# The only privileges the service account may have: viewing users, groups and items (as administrator, which the
# organization reports need, and as user), and basemaps. Nothing that creates, changes, shares or deletes.
READ_ONLY_PRIVILEGES = frozenset({
    "portal:admin:viewUsers", "portal:admin:viewGroups", "portal:admin:viewItems",
    "portal:user:viewOrgUsers", "portal:user:viewOrgGroups", "portal:user:viewOrgItems",
    "premium:user:basemaps"})


class NotReadOnly(SystemExit):
    """The service account can do more than view: refuse to go on."""


def check_read_only(gis):
    """Refuses (NotReadOnly) unless the signed-in account's privileges are all in READ_ONLY_PRIVILEGES. Fails closed:
    any privilege not on the list stops the run, even one that only looks harmless (21 October 2026, Dan: deletes
    are only ever run deliberately, never automatically)."""
    me = gis.users.me
    extra = sorted(set(me.privileges or []) - READ_ONLY_PRIVILEGES)
    if extra:
        raise NotReadOnly(f"REFUSED: {me.username} (role {me.role}) can do more than view in ArcGIS Online: {', '.join(extra)}. "
                          "Nothing was read or changed. Put the account back on its read-only role.")
    return me


def agol_gis(personal=False):
    """Signs in to ArcGIS Online as the read-only service account (AGOL_USERNAME, AGOL_PASSWORD, at AGOL_URL,
    default https://www.arcgis.com), and refuses to go on unless the account can only view (check_read_only).
    Without those settings it refuses: it never falls back to another sign-in on its own.

    personal=True signs in with the ArcGIS Pro sign-in on this machine (GIS("home")), the maintainer's
    administrator account, instead. Only a tool the maintainer runs deliberately may ask for it; nothing in the
    pipeline does (CLAUDE.md, "Deleting")."""
    from arcgis.gis import GIS
    if personal:
        gis = GIS("home")
        print(f"connected to ArcGIS Online as {gis.users.me.username} (the ArcGIS Pro sign-in on this machine, asked for)")
        return gis
    user, password = os.environ.get("AGOL_USERNAME"), os.environ.get("AGOL_PASSWORD")
    if not (user and password):
        raise SystemExit("REFUSED: AGOL_USERNAME and AGOL_PASSWORD are not set (.env beside agol_db.py, or the "
                         "environment). Sign-in is only as the read-only service account; nothing was read.")
    gis = GIS(os.environ.get("AGOL_URL") or "https://www.arcgis.com", user, password)
    me = check_read_only(gis)
    print(f"connected to ArcGIS Online as {me.username} (read-only: {len(me.privileges)} view privileges, checked)")
    return gis


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
