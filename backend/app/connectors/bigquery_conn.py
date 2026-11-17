import json
from .base import Connector


class BigQueryConnector(Connector):
    """BigQuery connector. Auth options (combinable, no key file required):

      1. conn['credentials'] — dict OR JSON string OR file path. Accepts BOTH a
         service-account KEY and a keyless **WIF credential-config**
         (type "external_account"): dispatched on the JSON "type" field.
      2. blank credentials -> Application Default Credentials (ADC):
         GOOGLE_APPLICATION_CREDENTIALS env (may itself point to a WIF
         credential-config), the attached service account on GCP, or
         `gcloud auth application-default login`.
      3. conn['impersonate_service_account'] (optional, on top of 1 or 2):
         exchange the base identity for short-lived tokens of that SA via the
         IAM Credentials API (base needs roles/iam.serviceAccountTokenCreator
         on the target SA). With 2+3 TenantOS stores NO secret at all.

    Honors conn['location'] (e.g. "US", "asia-southeast2") for the query region.
    """
    dialect = "bigquery"
    SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

    def __init__(self, conn: dict):
        self.c = conn
        # cost ceiling for execute() + dry-run guard. Default 2 GiB; override via conn['max_bytes_billed'].
        try:
            self.max_bytes_billed = int(conn.get("max_bytes_billed") or 2 * 1024 ** 3)
        except (TypeError, ValueError):
            self.max_bytes_billed = 2 * 1024 ** 3

    def namespace(self) -> dict:
        # `default_dataset` only resolves UNQUALIFIED names; an explicit
        # `other-project.other_ds.t` in the SQL overrides it per BigQuery semantics.
        return {"catalog": self.c.get("project") or "", "schema": self.c.get("dataset") or ""}

    def _base_credentials(self):
        """Explicit credentials from conn['credentials'], or None -> ADC.

        `conn` is a free-form dict accepted from any authenticated console user via
        POST /admin/datasources, PATCH /admin/datasources/{did}/conn and
        POST /admin/test-connection, so everything reachable from this value is an
        attacker-controlled sink. Three were, and are now refused:

        * a STRING was treated as a filesystem PATH (`os.path.exists` -> `open`), i.e.
          an existence oracle plus an arbitrary-file read for any credential file on the
          pod — another tenant's key, or the platform's own service account.
        * `external_account` + `credential_source.url` makes google-auth FETCH that URL
          on the next query (cloud metadata, any in-cluster service). `network_policy`
          guards LLM endpoints and JWKS URLs but is never consulted for connector config.
        * `external_account` + `credential_source.file` reads a local file and POSTs its
          contents to Google STS as the subject token.
        * `external_account` + `credential_source.executable` runs a command line taken
          from the request. google-auth refuses this unless
          GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES=1, which is the only reason it was
          not already remote code execution.

        Keyless access — the documented, org-policy-compliant path — is unaffected: it
        uses host ADC plus `conn['impersonate_service_account']` and stores no secret.
        """
        creds = self.c.get("credentials")
        if not creds:
            return None  # -> ADC (the supported keyless path)
        if isinstance(creds, str):
            try:
                creds = json.loads(creds)           # inline JSON only — never a path
            except ValueError as exc:
                raise ValueError("credentials must be inline JSON, not a file path") from exc
        if not isinstance(creds, dict):
            raise ValueError("credentials must be a JSON object")
        ctype = creds.get("type")
        if ctype == "service_account":
            from google.oauth2 import service_account
            return service_account.Credentials.from_service_account_info(creds)
        if ctype == "authorized_user":              # gcloud-style user credentials
            from google.oauth2.credentials import Credentials as UserCredentials
            return UserCredentials.from_authorized_user_info(creds)
        if ctype == "external_account":
            raise ValueError(
                "external_account credential configs are not accepted here: they can be "
                "sourced from a URL, a local file, or an executable. Use host ADC plus "
                "conn['impersonate_service_account'] for keyless access.")
        raise ValueError(f"unsupported credentials JSON type: {ctype!r} "
                         "(expected service_account / authorized_user)")

    def _credentials(self):
        base = self._base_credentials()
        target = (self.c.get("impersonate_service_account") or "").strip()
        if not target:
            return base  # None lets the client fall back to ADC
        import google.auth
        from google.auth import impersonated_credentials
        if base is None:
            base, _project = google.auth.default(scopes=self.SCOPES)
        return impersonated_credentials.Credentials(
            source_credentials=base, target_principal=target, target_scopes=self.SCOPES)

    def _client(self):
        from google.cloud import bigquery
        location = self.c.get("location") or None
        credentials = self._credentials()
        # credentials=None lets the client fall back to ADC.
        return bigquery.Client(project=self.c["project"], credentials=credentials, location=location)

    def _job_config(self, bigquery, dry_run=False):
        # Set a default dataset so unqualified table names (e.g. `acme_store_order_mp`,
        # which is how the MDL/allowlist + NL->SQL reference them) resolve to
        # project.dataset.table instead of erroring "must be qualified with a dataset".
        jc = bigquery.QueryJobConfig()
        project, dataset = self.c.get("project"), self.c.get("dataset")
        if project and dataset:
            jc.default_dataset = f"{project}.{dataset}"
        if dry_run:
            jc.dry_run = True
            jc.use_query_cache = False
        elif self.max_bytes_billed:
            jc.maximum_bytes_billed = int(self.max_bytes_billed)   # hard cost ceiling at execute
        return jc

    def dry_run(self, sql: str):
        """Estimate bytes the query would scan, WITHOUT running it (cost guard)."""
        from google.cloud import bigquery
        job = self._client().query(sql, job_config=self._job_config(bigquery, dry_run=True))
        return int(job.total_bytes_processed or 0)

    def run(self, sql: str):
        from google.cloud import bigquery
        res = self._client().query(sql, job_config=self._job_config(bigquery)).result()
        cols = [f.name for f in res.schema]
        rows = [list(r.values()) for r in res]
        return cols, rows

    def introspect(self) -> dict:
        client = self._client()
        q = (f"SELECT table_name, column_name FROM "
             f"`{self.c['project']}.{self.c['dataset']}.INFORMATION_SCHEMA.COLUMNS` "
             f"ORDER BY table_name, ordinal_position")
        out = {}
        for r in client.query(q).result():
            out.setdefault(r.table_name, []).append(r.column_name)
        return out

    def qualified_relation(self, namespace: str, name: str) -> str:
        project = self.c.get("project")
        dataset = namespace or self.c.get("dataset")
        return "`" + ".".join(
            str(part).replace("`", "") for part in (project, dataset, name) if part
        ) + "`"

    def sample_rows(self, namespace: str, table: str, columns: list[str], limit: int):
        """Use the BigQuery tabledata/list path rather than a LIMIT query.

        LIMIT does not reduce BigQuery bytes scanned. ``list_rows`` provides a bounded
        preview without turning discovery profiling into an unbounded analytical scan.
        """
        client = self._client()
        table_ref = client.get_table(
            f"{self.c['project']}.{namespace or self.c['dataset']}.{table}"
        )
        selected = [field for field in table_ref.schema if field.name in set(columns)]
        rows = client.list_rows(
            table_ref,
            selected_fields=selected,
            max_results=max(1, min(int(limit), 1000)),
        )
        return [field.name for field in selected], [list(row.values()) for row in rows]

    def discover(self) -> dict:
        client = self._client()
        dataset = self.c["dataset"]
        primary, foreign = self._declared_relationships(client, dataset)
        relations = []
        for item in client.list_tables(f"{self.c['project']}.{dataset}"):
            table = client.get_table(item.reference)
            partitioning = None
            if getattr(table, "time_partitioning", None):
                partitioning = {
                    "type": str(table.time_partitioning.type_ or ""),
                    "field": table.time_partitioning.field,
                }
            relations.append({
                "namespace": dataset,
                "name": table.table_id,
                "kind": str(getattr(table, "table_type", "TABLE") or "TABLE").lower(),
                "description": str(getattr(table, "description", "") or "")[:1000],
                "estimated_rows": (
                    int(table.num_rows) if getattr(table, "num_rows", None) is not None else None
                ),
                "estimated_bytes": (
                    int(table.num_bytes) if getattr(table, "num_bytes", None) is not None else None
                ),
                "partitioning": partitioning,
                "clustering": list(getattr(table, "clustering_fields", None) or []),
                "columns": [{
                    "name": field.name,
                    "data_type": field.field_type,
                    "nullable": field.mode != "REQUIRED",
                    "ordinal": index,
                    "description": str(field.description or "")[:500],
                } for index, field in enumerate(table.schema, 1)],
                "primary_key": primary.get(table.table_id, []),
                "foreign_keys": foreign.get(table.table_id, []),
            })
        return {"dialect": self.dialect, "relations": relations}

    def _declared_relationships(self, client, dataset: str):
        """Read BigQuery's declared (not enforced) primary/foreign-key constraints.

        Many warehouses do not declare constraints, and some service accounts cannot
        read these INFORMATION_SCHEMA views. Relationship discovery is therefore
        best-effort and never prevents ordinary schema discovery.
        """
        project = self.c["project"]
        q = f"""
            SELECT
              kcu.table_name,
              kcu.column_name,
              tc.constraint_type,
              ref_kcu.table_name AS referenced_table_name,
              ref_kcu.column_name AS referenced_column_name,
              kcu.ordinal_position
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS` tc
            JOIN `{project}.{dataset}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE` kcu
              ON tc.constraint_catalog = kcu.constraint_catalog
             AND tc.constraint_schema = kcu.constraint_schema
             AND tc.constraint_name = kcu.constraint_name
             AND tc.table_name = kcu.table_name
            LEFT JOIN `{project}.{dataset}.INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS` rc
              ON tc.constraint_catalog = rc.constraint_catalog
             AND tc.constraint_schema = rc.constraint_schema
             AND tc.constraint_name = rc.constraint_name
            LEFT JOIN `{project}.{dataset}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE` ref_kcu
              ON rc.unique_constraint_catalog = ref_kcu.constraint_catalog
             AND rc.unique_constraint_schema = ref_kcu.constraint_schema
             AND rc.unique_constraint_name = ref_kcu.constraint_name
             AND ref_kcu.ordinal_position = kcu.position_in_unique_constraint
            WHERE tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
            ORDER BY kcu.table_name, tc.constraint_name, kcu.ordinal_position
        """
        try:
            rows = client.query(q).result()
        except Exception:
            return {}, {}
        primary, foreign = {}, {}
        for row in rows:
            table = str(row.table_name)
            if str(row.constraint_type).upper() == "PRIMARY KEY":
                primary.setdefault(table, []).append(str(row.column_name))
            elif row.referenced_table_name and row.referenced_column_name:
                foreign.setdefault(table, []).append({
                    "column": str(row.column_name),
                    "references_namespace": dataset,
                    "references_table": str(row.referenced_table_name),
                    "references_column": str(row.referenced_column_name),
                })
        return primary, foreign
