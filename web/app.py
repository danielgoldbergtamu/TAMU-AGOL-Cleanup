"""The dashboard site: the dashboard page and its data, behind Auth0 sign-in.

The page (dashboard/full_template.html, in the image) holds no data. It asks /api/v1/<view> for each view, which
this app answers from the read-only copy of the database that the daily job (pipeline_daily.py) leaves in
AGOL_OUTPUT_DIR (agol-serve.duckdb, tools/publish_serving_db.py; the queries are dashboard/queries.py). This app
never opens a database for writing and never touches ArcGIS Online.

Who gets in (fail closed: with neither setting, nobody does):
  AUTH_REQUIRED_ROLE    a role the Auth0 user must have, read from the ID token's AUTH_ROLES_CLAIM
  AUTH_ALLOWED_EMAILS   comma-separated email addresses let in without the role

Settings (environment, or .env beside the repository):
  AUTH0_DOMAIN, AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET   the Auth0 application (Regular Web Application)
  AUTH_ROLES_CLAIM      the custom claim that carries roles (an Auth0 Action adds it), e.g. https://example.edu/roles
  SESSION_SECRET        a long random string that signs the session cookie
  PUBLIC_URL            the site's address, for the sign-in callback and sign-out (e.g. https://agol.example.edu)
  AGOL_OUTPUT_DIR       where the daily job leaves the site's copy of the database (default data/)
  AGOL_WEB_CACHE_DIR    where this app keeps its own copy of it (default: the system's temporary folder)
  AGOL_WEB_DB_MEMORY    DuckDB's memory limit (default 512MB)
  AGOL_WEB_ENV=development and AUTH_DISABLED=1         sign-in off, for a local test only; refused otherwise

Run:  uvicorn web.app:app --host 0.0.0.0 --port 8000
"""
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time

import duckdb

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO, ".env"), override=False)

OUTPUT_DIR = os.environ.get("AGOL_OUTPUT_DIR") or os.path.join(REPO, "data")
TEMPLATE = os.path.join(REPO, "dashboard", "full_template.html")
EDITIONS = ("full", "published")
SERVE_DB = "agol-serve.duckdb"
CACHE_DIR = os.environ.get("AGOL_WEB_CACHE_DIR") or tempfile.gettempdir()   # this pod's own copy of the database
DB_MEMORY = os.environ.get("AGOL_WEB_DB_MEMORY", "512MB")                    # DuckDB's limit, below the pod's
sys.path.insert(0, os.path.join(REPO, "dashboard"))
import queries  # noqa: E402

DEV = os.environ.get("AGOL_WEB_ENV") == "development"
AUTH_DISABLED = os.environ.get("AUTH_DISABLED") == "1"
if AUTH_DISABLED and not DEV:
    sys.exit("AUTH_DISABLED=1 is only allowed with AGOL_WEB_ENV=development (a local test). Refusing to start.")

AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
REQUIRED_ROLE = os.environ.get("AUTH_REQUIRED_ROLE", "").strip()
ROLES_CLAIM = os.environ.get("AUTH_ROLES_CLAIM", "").strip()
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("AUTH_ALLOWED_EMAILS", "").split(",") if e.strip()}
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or (secrets.token_urlsafe(48) if DEV else "")
if not SESSION_SECRET:
    sys.exit("SESSION_SECRET is not set. Refusing to start.")
SESSION_HOURS = 8

app = FastAPI(title="AGOL Cleanup Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=SESSION_HOURS * 3600,
                   same_site="lax", https_only=not DEV)

oauth = None
if not AUTH_DISABLED:
    from authlib.integrations.starlette_client import OAuth
    if not (AUTH0_DOMAIN and os.environ.get("AUTH0_CLIENT_ID") and os.environ.get("AUTH0_CLIENT_SECRET")):
        sys.exit("AUTH0_DOMAIN, AUTH0_CLIENT_ID and AUTH0_CLIENT_SECRET must be set (or AUTH_DISABLED=1 in development).")
    oauth = OAuth()
    oauth.register("auth0", client_id=os.environ["AUTH0_CLIENT_ID"], client_secret=os.environ["AUTH0_CLIENT_SECRET"],
                   server_metadata_url=f"https://{AUTH0_DOMAIN}/.well-known/openid-configuration",
                   client_kwargs={"scope": "openid profile email"})


def allowed(claims):
    """True if the signed-in user may see the dashboards: the required role, or an allowed email. Fail closed."""
    roles = claims.get(ROLES_CLAIM) if ROLES_CLAIM else None
    roles = roles if isinstance(roles, list) else ([roles] if roles else [])
    email = (claims.get("email") or "").lower()
    by_role = bool(REQUIRED_ROLE) and REQUIRED_ROLE in roles
    by_email = bool(email) and claims.get("email_verified", True) and email in ALLOWED_EMAILS
    return by_role or by_email


def current_user(request: Request):
    if AUTH_DISABLED:
        return {"email": "local test (sign-in off)", "name": "local test"}
    user = request.session.get("user")
    if user and user.get("expires", 0) > time.time():
        return user
    return None


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return user


@app.exception_handler(401)
async def unauthorized(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "sign in first"}, status_code=401)
    return RedirectResponse(f"/login?next={request.url.path}")


def callback_url(request: Request):
    return (PUBLIC_URL or str(request.base_url).rstrip("/")) + "/callback"


@app.get("/login")
async def login(request: Request, next: str = "/"):
    if AUTH_DISABLED:
        return RedirectResponse("/")
    request.session["next"] = next if next.startswith("/") and not next.startswith("//") else "/"
    return await oauth.auth0.authorize_redirect(request, callback_url(request))


@app.get("/callback")
async def callback(request: Request):
    if AUTH_DISABLED:
        return RedirectResponse("/")
    token = await oauth.auth0.authorize_access_token(request)
    claims = token.get("userinfo") or {}
    if not allowed(claims):
        request.session.clear()
        return HTMLResponse("<h1>Not authorized</h1><p>Your account is signed in but has not been given access to this "
                            "dashboard. Ask the maintainer for access.</p><p><a href='/logout'>Sign out</a></p>", status_code=403)
    request.session["user"] = {"email": claims.get("email"), "name": claims.get("name"), "expires": time.time() + SESSION_HOURS * 3600}
    return RedirectResponse(request.session.pop("next", "/"))


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    if AUTH_DISABLED:
        return RedirectResponse("/")
    from urllib.parse import urlencode
    back = PUBLIC_URL or str(request.base_url).rstrip("/")
    return RedirectResponse(f"https://{AUTH0_DOMAIN}/v2/logout?" + urlencode({"client_id": os.environ["AUTH0_CLIENT_ID"], "returnTo": back}))


def output_file(name):
    path = os.path.join(OUTPUT_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=503, detail="No data yet: the daily job has not published the site's database.")
    return path


def page(request: Request, edition: str):
    user = require_user(request)
    # the page carries only its edition; it asks /api/v1/... for everything else
    html = open(TEMPLATE, encoding="utf-8").read().replace("/*__DATA__*/null", json.dumps({"edition": edition}))
    who = (user.get("email") or user.get("name") or "").replace("<", "&lt;")
    bar = (f'<div style="position:fixed;top:6px;right:10px;font:12px system-ui;opacity:.8;z-index:99">{who} · '
           f'<a href="/{"published" if edition == "full" else ""}">{"published edition" if edition == "full" else "full edition"}</a> · '
           f'<a href="/logout">sign out</a></div>')
    head, sep, tail = html.rpartition("</body>")
    html = head + bar + sep + tail if sep else html + bar   # the built page may have no </body>
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
async def full(request: Request):
    return page(request, "full")


@app.get("/published", response_class=HTMLResponse)
async def published(request: Request):
    return page(request, "published")


_db = {"mtime": None, "con": None, "local": None}
_db_lock = threading.Lock()


def serving_db():
    """A cursor on the newest copy of the site's database. Each new copy the daily job publishes is copied to this
    pod's own disk and opened there, read-only: the shared volume is NFS underneath, where DuckDB's file locks are
    not to be trusted, and the job can then replace its file at any time. The previous connection closes when its
    last cursor is done."""
    path = output_file(SERVE_DB)
    mtime = os.path.getmtime(path)
    with _db_lock:
        if _db["mtime"] != mtime:
            local = os.path.join(CACHE_DIR, f"agol-serve-{int(mtime * 1000)}.duckdb")
            if not os.path.exists(local):
                shutil.copyfile(path, local + ".tmp")
                os.replace(local + ".tmp", local)
            old = _db["local"]
            _db.update(con=duckdb.connect(local, read_only=True, config={"memory_limit": DB_MEMORY}), mtime=mtime, local=local)
            if old and old != local:
                try:
                    os.remove(old)   # Linux removes it once the last cursor on it is closed; Windows keeps it while open
                except OSError:
                    pass
        return _db["con"].cursor()


@app.get("/api/v1/selection")
def selection_data(request: Request, date: str = "created", year_from: int | None = None, year_to: int | None = None,
                   tiers: str = "", never: bool = False):
    """Layers for proposed deletion by period (full edition only): tiers is a list separated by "|". Read-only."""
    require_user(request)
    cur = serving_db()
    try:
        data = queries.selection(cur, True, date=date, year_from=year_from, year_to=year_to,
                                 tiers=[x for x in tiers.split("|") if x], never=never)
    finally:
        cur.close()
    if data is None:
        raise HTTPException(status_code=404)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/{view}")
def view_data(request: Request, view: str, edition: str = "full"):
    """One view's data from the database. A plain def: FastAPI runs it in a worker thread, as DuckDB blocks."""
    require_user(request)
    if view not in queries.VIEWS or edition not in EDITIONS:
        raise HTTPException(status_code=404)
    cur = serving_db()
    try:
        data = queries.VIEWS[view](cur, named=edition == "full")
    finally:
        cur.close()
    if data is None:   # e.g. a view the database has no data for yet
        raise HTTPException(status_code=404)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/{view}/{key}")
def keyed_view_data(request: Request, view: str, key: str, edition: str = "full"):
    """One item of a view, e.g. an account's page. 404 when there is no such item or the edition does not show it."""
    require_user(request)
    if view not in queries.KEYED_VIEWS or edition not in EDITIONS:
        raise HTTPException(status_code=404)
    cur = serving_db()
    try:
        data = queries.KEYED_VIEWS[view](cur, named=edition == "full", key=key)
    finally:
        cur.close()
    if data is None:
        raise HTTPException(status_code=404)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/api/status")
async def status(request: Request):
    require_user(request)
    path = os.path.join(OUTPUT_DIR, "last_run.json")
    return JSONResponse(json.load(open(path)) if os.path.exists(path) else {"run_id": None})


@app.get("/healthz")
async def healthz():
    """For the cluster's probes: up, and whether the daily job has published the site's database. No data."""
    return {"ok": True, "database": os.path.exists(os.path.join(OUTPUT_DIR, SERVE_DB))}
