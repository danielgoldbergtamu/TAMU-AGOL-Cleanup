# Running on Kubernetes

`agol-cleanup.yaml` is a starting point for the cluster-config repository (the same pattern as GIS Day): one image,
`ghcr.io/danielgoldbergtamu/tamu-agol-cleanup`, built by `.github/workflows/image.yml`; a web Deployment and a
daily CronJob sharing one volume. Lines marked `CHANGE` depend on the cluster.

Nothing here, and nothing in the image, holds data or secrets. They are created in the cluster:

## 1. Secrets

The daily job (`agol-cleanup-secrets`):

```
kubectl -n agol-cleanup create secret generic agol-cleanup-secrets \
  --from-literal=AGOL_USERNAME=... --from-literal=AGOL_PASSWORD=... \
  --from-literal=MQS_CLIENT_ID=... --from-literal=MQS_SHARED_SECRET=...
```

The web app (`agol-cleanup-web`), from the Auth0 application (Regular Web Application; allowed callback URL
`<PUBLIC_URL>/callback`, allowed logout URL `<PUBLIC_URL>`):

```
kubectl -n agol-cleanup create secret generic agol-cleanup-web \
  --from-literal=AUTH0_DOMAIN=... --from-literal=AUTH0_CLIENT_ID=... --from-literal=AUTH0_CLIENT_SECRET=... \
  --from-literal=AUTH_ROLES_CLAIM=... --from-literal=AUTH_REQUIRED_ROLE=... \
  --from-literal=AUTH_ALLOWED_EMAILS=... --from-literal=SESSION_SECRET="$(openssl rand -base64 48)"
```

Who gets in is decided only by `AUTH_REQUIRED_ROLE` (a role in the `AUTH_ROLES_CLAIM` claim, which an Auth0 Action
adds to the ID token) and `AUTH_ALLOWED_EMAILS`. With neither, nobody does.

Typing a secret on a command line leaves it in the shell history; prefer `--from-env-file` with a file that is
deleted afterwards, or the cluster's secret manager.

## 2. The private lookup tables

The department-to-unit, degree-to-department and college-level tables live in the private repository
(`private/config/`), not here:

```
kubectl -n agol-cleanup create configmap agol-cleanup-config \
  --from-file=department_unit_map_DRAFT.csv --from-file=major_department_map.csv \
  --from-file=college_level_departments.csv
```

## 3. The database

Copy the DuckDB file (made by `tools/migrate_to_duckdb.py`) onto the volume once, before the first run, e.g. from a
pod that mounts it: `kubectl cp data/agol.duckdb agol-cleanup/<pod>:/data/agol.duckdb`.

## 4. The Entra lookup, until an app registration allows it unattended

On a workstation: `python TAMU_AGOL_EntraID.py --input_csv_path <member report> --output_csv_path
AGOL_EntraID_Status_<date>.csv --no-history`, then copy both files it writes (the `.csv` and the `.people.csv`) to
`/data/entra-inbox/` on the volume. The next daily run records their history and uses them.

## 5. Checking a run

`kubectl -n agol-cleanup logs job/<name>`; each step's log is in `/data/reports/runs/<run id>/`; the site's
`/api/status` shows the last finished run.
