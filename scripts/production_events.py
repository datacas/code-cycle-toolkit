"""Import attributable deployment and incident events into the telemetry store.

This module deliberately stores only allow-listed event fields. In particular,
GitHub deployment payloads are not retained wholesale because their ``payload``
member is caller controlled and may contain secrets or personal data.
"""

from __future__ import annotations

import json
import argparse
import hashlib
from contextlib import closing
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping


class ProductionEventError(ValueError):
    """An event or configuration is invalid and cannot be imported safely."""


EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS production_deployments (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    repository TEXT NOT NULL,
    external_id TEXT NOT NULL,
    environment TEXT,
    classification TEXT NOT NULL,
    service_id TEXT,
    sha TEXT,
    ref TEXT,
    task TEXT,
    production_environment INTEGER,
    created_at TEXT NOT NULL,
    source_updated_at TEXT,
    provenance TEXT NOT NULL,
    UNIQUE(source, repository, external_id)
);
CREATE TABLE IF NOT EXISTS production_deployment_statuses (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    repository TEXT NOT NULL,
    deployment_external_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_updated_at TEXT,
    environment TEXT,
    provenance TEXT NOT NULL,
    UNIQUE(source, repository, deployment_external_id, external_id),
    FOREIGN KEY(source, repository, deployment_external_id)
      REFERENCES production_deployments(source, repository, external_id)
      ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS production_deployment_classifications (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    repository TEXT NOT NULL,
    deployment_external_id TEXT NOT NULL,
    mapping_version INTEGER NOT NULL,
    mapping_fingerprint TEXT NOT NULL,
    classification TEXT NOT NULL,
    service_id TEXT,
    classified_at TEXT NOT NULL,
    provenance TEXT NOT NULL,
    UNIQUE(source, repository, deployment_external_id, mapping_fingerprint),
    FOREIGN KEY(source, repository, deployment_external_id)
      REFERENCES production_deployments(source, repository, external_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS production_deployment_commits (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    repository TEXT NOT NULL,
    deployment_external_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    relation_kind TEXT NOT NULL CHECK(relation_kind IN ('observed','derived')),
    method TEXT NOT NULL,
    references_json TEXT NOT NULL,
    provenance TEXT NOT NULL,
    UNIQUE(source, repository, deployment_external_id, commit_sha, relation_kind),
    FOREIGN KEY(source, repository, deployment_external_id)
      REFERENCES production_deployments(source, repository, external_id)
      ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS production_deployment_links (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    repository TEXT NOT NULL,
    deployment_external_id TEXT NOT NULL,
    target_external_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN ('rollback','corrective','related')),
    evidence TEXT NOT NULL,
    provenance TEXT NOT NULL,
    UNIQUE(source, repository, deployment_external_id, target_external_id, relation),
    FOREIGN KEY(source, repository, deployment_external_id)
      REFERENCES production_deployments(source, repository, external_id) ON DELETE CASCADE,
    FOREIGN KEY(source, repository, target_external_id)
      REFERENCES production_deployments(source, repository, external_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS production_incidents (
    id INTEGER PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    resolved_at TEXT,
    status TEXT NOT NULL,
    provenance TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE TABLE IF NOT EXISTS production_incident_links (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    incident_external_id TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('deployment','commit')),
    target_repository TEXT,
    target_external_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN ('related_to','affected_by','recovered_by')),
    evidence TEXT NOT NULL,
    provenance TEXT NOT NULL,
    UNIQUE(source, incident_external_id, target_type, target_repository, target_external_id, relation),
    FOREIGN KEY(source, incident_external_id)
      REFERENCES production_incidents(source, external_id)
      ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS production_deployments_by_service_time
  ON production_deployments(service_id, created_at);
CREATE INDEX IF NOT EXISTS production_incidents_by_service_time
  ON production_incidents(service_id, started_at);
"""

_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CLASSIFICATIONS = {"production", "staging", "ci", "other"}
_INCIDENT_STATUSES = {"open", "resolved", "monitoring", "closed", "unknown"}


def _timestamp(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProductionEventError(f"{field} must be an ISO 8601 timestamp")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ProductionEventError(f"{field} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductionEventError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProductionEventError(f"{field} must be a non-empty string or integer")
    result = str(value).strip()
    if not result or len(result) > 200:
        raise ProductionEventError(f"{field} must be 1 to 200 characters")
    return result


def _optional_text(value: Any, field: str, *, maximum: int = 500) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ProductionEventError(f"{field} must be a string of at most {maximum} characters or null")
    return value


def _provenance(source: str, endpoint: str, imported_at: str) -> str:
    # Never persist request headers, tokens, API payloads, or arbitrary source data.
    return json.dumps({"source": source, "endpoint": endpoint,
                       "imported_at": imported_at, "schema_version": 1},
                      sort_keys=True, separators=(",", ":"))


def load_deployment_mapping(path: str | Path) -> dict[str, Any]:
    """Load and validate the versioned, explicit service/environment mapping."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProductionEventError(f"cannot read deployment mapping: {error}") from error
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ProductionEventError("deployment mapping schema_version must be 1")
    mapping = data.get("deployment_mapping")
    if not isinstance(mapping, dict):
        raise ProductionEventError("deployment_mapping must be an object")
    github = mapping.get("github", {})
    if not isinstance(github, dict):
        raise ProductionEventError("deployment_mapping.github must be an object")
    repositories = github.get("repositories", {})
    if not isinstance(repositories, dict):
        raise ProductionEventError("deployment_mapping.github.repositories must be an object")
    for repository, entry in repositories.items():
        if not _REPOSITORY.fullmatch(repository) or not isinstance(entry, dict):
            raise ProductionEventError("each mapped repository must be owner/name with an object value")
        environments = entry.get("environments", {})
        if not isinstance(environments, dict):
            raise ProductionEventError("repository environments must be an object")
        for environment, config in environments.items():
            if not isinstance(environment, str) or not environment or not isinstance(config, dict):
                raise ProductionEventError("each environment mapping must have a name and object value")
            if config.get("classification") not in _CLASSIFICATIONS:
                raise ProductionEventError("environment classification is invalid")
            if not isinstance(config.get("service_id"), str) or not config["service_id"].strip():
                raise ProductionEventError("mapped environments require a service_id")
    return data


def classify_environment(mapping: Mapping[str, Any], repository: str,
                          environment: Any) -> tuple[str, str | None]:
    """Unknown/unconfigured environments remain unknown and have no service."""
    if not isinstance(environment, str) or not environment:
        return "unknown", None
    deployment_mapping = mapping.get("deployment_mapping", {})
    github = deployment_mapping.get("github", {}) if isinstance(deployment_mapping, Mapping) else {}
    repositories = github.get("repositories", {}) if isinstance(github, Mapping) else {}
    repo_config = repositories.get(repository, {}) if isinstance(repositories, Mapping) else {}
    environments = repo_config.get("environments", {}) if isinstance(repo_config, Mapping) else {}
    config = environments.get(environment) if isinstance(environments, Mapping) else None
    if not isinstance(config, Mapping):
        return "unknown", None
    return str(config["classification"]), str(config["service_id"])


class ProductionEventStore:
    """Store normalized events in the existing telemetry SQLite database."""

    def __init__(self, database: str | Path):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(EVENT_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def import_github(self, repository: str, deployments: Iterable[Mapping[str, Any]],
                      statuses_by_deployment: Mapping[str, Iterable[Mapping[str, Any]]],
                      mapping: Mapping[str, Any], *, imported_at: str | None = None) -> int:
        """Idempotently upsert GitHub deployment and status snapshots.

        The deployment's SHA is retained as one observed inclusion. No ancestor
        commits are inferred from Git history.
        """
        if not _REPOSITORY.fullmatch(repository):
            raise ProductionEventError("repository must be owner/name")
        if type(mapping.get("schema_version")) is not int or mapping.get("schema_version") != 1:
            raise ProductionEventError("deployment mapping schema_version must be 1")
        mapping_bytes = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("utf-8")
        mapping_fingerprint = hashlib.sha256(mapping_bytes).hexdigest()
        when = _timestamp(imported_at or datetime.now(timezone.utc).isoformat(), "imported_at")
        assert when is not None
        source = "github-deployments"
        count = 0
        with closing(self._connect()) as connection, connection:
            try:
                for deployment in deployments:
                    external_id = _identifier(deployment.get("id"), "deployment id")
                    environment = deployment.get("environment")
                    if environment is not None and not isinstance(environment, str):
                        raise ProductionEventError("deployment environment must be a string or null")
                    classification, service_id = classify_environment(mapping, repository, environment)
                    created = _timestamp(deployment.get("created_at"), "deployment created_at")
                    updated = _timestamp(deployment.get("updated_at"), "deployment updated_at", optional=True)
                    sha = deployment.get("sha")
                    if sha is not None and (not isinstance(sha, str) or not _SHA.fullmatch(sha)):
                        raise ProductionEventError("deployment sha must be a full hexadecimal commit SHA")
                    ref = _optional_text(deployment.get("ref"), "deployment ref")
                    task = _optional_text(deployment.get("task"), "deployment task")
                    production_flag = deployment.get("production_environment")
                    if production_flag is not None and not isinstance(production_flag, bool):
                        raise ProductionEventError("production_environment must be boolean or null")
                    provenance = _provenance(source, f"repos/{repository}/deployments/{external_id}", when)
                    connection.execute("""INSERT INTO production_deployments
                      (source,repository,external_id,environment,classification,service_id,sha,ref,task,
                       production_environment,created_at,source_updated_at,provenance)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(source,repository,external_id) DO UPDATE SET
                       environment=excluded.environment,classification=excluded.classification,
                       service_id=excluded.service_id,sha=excluded.sha,ref=excluded.ref,task=excluded.task,
                       production_environment=excluded.production_environment,
                       created_at=excluded.created_at,source_updated_at=excluded.source_updated_at,
                       provenance=excluded.provenance""",
                       (source, repository, external_id, environment, classification, service_id,
                        sha, ref, task,
                        None if production_flag is None else int(production_flag), created, updated, provenance))
                    connection.execute("""INSERT INTO production_deployment_classifications
                      (source,repository,deployment_external_id,mapping_version,mapping_fingerprint,
                       classification,service_id,classified_at,provenance) VALUES (?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(source,repository,deployment_external_id,mapping_fingerprint)
                      DO UPDATE SET classification=excluded.classification,service_id=excluded.service_id,
                       classified_at=excluded.classified_at,provenance=excluded.provenance""",
                      (source, repository, external_id, 1, mapping_fingerprint, classification,
                       service_id, when, provenance))
                    # A corrected source SHA replaces only the direct API-SHA
                    # relation; independent manifest evidence stays intact.
                    connection.execute("""DELETE FROM production_deployment_commits
                      WHERE source=? AND repository=? AND deployment_external_id=? AND method='deployment_sha'""",
                      (source, repository, external_id))
                    if sha:
                        connection.execute("""INSERT INTO production_deployment_commits
                          (source,repository,deployment_external_id,commit_sha,relation_kind,method,
                           references_json,provenance) VALUES (?,?,?,?,?,?,?,?)
                          ON CONFLICT(source,repository,deployment_external_id,commit_sha,relation_kind)
                          DO UPDATE SET method=excluded.method,references_json=excluded.references_json,
                                        provenance=excluded.provenance""",
                           (source, repository, external_id, sha.lower(), "observed", "deployment_sha",
                            "[]", provenance))
                    for status in statuses_by_deployment.get(external_id, ()):
                        status_id = _identifier(status.get("id"), "deployment status id")
                        state = status.get("state")
                        if not isinstance(state, str) or not state.strip() or len(state) > 100:
                            raise ProductionEventError("deployment status state must be a non-empty string")
                        status_created = _timestamp(status.get("created_at"), "status created_at")
                        status_updated = _timestamp(status.get("updated_at"), "status updated_at", optional=True)
                        status_environment = status.get("environment")
                        if status_environment is not None and not isinstance(status_environment, str):
                            raise ProductionEventError("status environment must be a string or null")
                        status_provenance = _provenance(
                            source, f"repos/{repository}/deployments/{external_id}/statuses/{status_id}", when)
                        connection.execute("""INSERT INTO production_deployment_statuses
                          (source,repository,deployment_external_id,external_id,state,created_at,
                           source_updated_at,environment,provenance) VALUES (?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(source,repository,deployment_external_id,external_id) DO UPDATE SET
                           state=excluded.state,created_at=excluded.created_at,
                           source_updated_at=excluded.source_updated_at,environment=excluded.environment,
                           provenance=excluded.provenance""",
                           (source, repository, external_id, status_id, state.strip(), status_created,
                            status_updated, status_environment, status_provenance))
                    count += 1
            except (sqlite3.Error, ProductionEventError):
                connection.rollback()
                raise
        return count

    @staticmethod
    def _require_deployments(connection: sqlite3.Connection, repository: str,
                             *deployment_ids: str) -> None:
        """Reject a relation to a deployment that was never imported."""
        for deployment_id in deployment_ids:
            found = connection.execute("""SELECT 1 FROM production_deployments
              WHERE source='github-deployments' AND repository=? AND external_id=?""",
              (repository, deployment_id)).fetchone()
            if found is None:
                raise ProductionEventError(
                    f"deployment {deployment_id} of {repository} has not been imported")

    def add_commit_relation(self, repository: str, deployment_id: str | int, commit_sha: str,
                            *, relation_kind: str, method: str, references: Iterable[str],
                            evidence_source: str) -> None:
        """Record explicit commit evidence or a reproducible derived relation."""
        if relation_kind not in {"observed", "derived"}:
            raise ProductionEventError("relation_kind must be observed or derived")
        refs = list(references)
        if relation_kind == "derived" and not refs:
            raise ProductionEventError("derived relations require their reference commits")
        if not isinstance(commit_sha, str) or not _SHA.fullmatch(commit_sha):
            raise ProductionEventError("commit_sha must be a full hexadecimal commit SHA")
        if any(not isinstance(ref, str) or not _SHA.fullmatch(ref) for ref in refs):
            raise ProductionEventError("references must contain full hexadecimal commit SHAs")
        if not isinstance(method, str) or not method.strip() or len(method) > 100:
            raise ProductionEventError("method must be a non-empty name of at most 100 characters")
        if not isinstance(evidence_source, str) or not evidence_source.strip() or len(evidence_source) > 500:
            raise ProductionEventError("evidence_source must be a safe, non-empty reference of at most 500 characters")
        when = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        provenance = _provenance("explicit-inclusion-evidence", evidence_source, when)
        with closing(self._connect()) as connection, connection:
            self._require_deployments(connection, repository, _identifier(deployment_id, "deployment id"))
            if relation_kind == "derived":
                target = connection.execute("""SELECT service_id,environment,created_at
                  FROM production_deployments WHERE source='github-deployments' AND repository=?
                  AND external_id=?""", (repository, _identifier(deployment_id, "deployment id"))).fetchone()
                if target is None or target["service_id"] is None or target["environment"] is None:
                    raise ProductionEventError("derived inclusion requires a configured target service and environment")
                placeholders = ",".join("?" for _ in refs)
                prior = connection.execute(f"""SELECT 1 FROM production_deployments
                  WHERE source='github-deployments' AND repository=? AND service_id=? AND environment=?
                  AND created_at < ? AND sha IN ({placeholders}) LIMIT 1""",
                  (repository, target["service_id"], target["environment"], target["created_at"],
                   *(ref.lower() for ref in refs))).fetchone()
                if prior is None:
                    raise ProductionEventError("derived inclusion requires a reference from an earlier deployment of the same service and environment")
            connection.execute("""INSERT INTO production_deployment_commits
              (source,repository,deployment_external_id,commit_sha,relation_kind,method,references_json,provenance)
              VALUES ('github-deployments',?,?,?,?,?,?,?)
              ON CONFLICT(source,repository,deployment_external_id,commit_sha,relation_kind) DO UPDATE SET
               method=excluded.method,references_json=excluded.references_json,provenance=excluded.provenance""",
              (repository, _identifier(deployment_id, "deployment id"), commit_sha.lower(), relation_kind,
               method, json.dumps(refs, separators=(",", ":")), provenance))

    def link_deployments(self, repository: str, deployment_id: str | int,
                         target_deployment_id: str | int, *, relation: str,
                         evidence: str) -> None:
        """Link rollback/corrective deployments only when a source names the relation."""
        if relation not in {"rollback", "corrective", "related"}:
            raise ProductionEventError("deployment relation must be rollback, corrective, or related")
        if (not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500):
            raise ProductionEventError("deployment links require explicit evidence of at most 500 characters")
        deployment_id = _identifier(deployment_id, "deployment id")
        target_deployment_id = _identifier(target_deployment_id, "target deployment id")
        if deployment_id == target_deployment_id:
            raise ProductionEventError("a deployment cannot link to itself")
        when = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        provenance = _provenance("explicit-deployment-relation", evidence.strip(), when)
        with closing(self._connect()) as connection, connection:
            self._require_deployments(connection, repository, deployment_id, target_deployment_id)
            connection.execute("""INSERT INTO production_deployment_links
              (source,repository,deployment_external_id,target_external_id,relation,evidence,provenance)
              VALUES ('github-deployments',?,?,?,?,?,?)
              ON CONFLICT(source,repository,deployment_external_id,target_external_id,relation)
              DO UPDATE SET evidence=excluded.evidence,provenance=excluded.provenance""",
              (repository, deployment_id, target_deployment_id, relation, evidence.strip(), provenance))

    def import_incident_document(self, document: Mapping[str, Any], *, source: str,
                                 imported_at: str | None = None) -> int:
        """Import version-1 neutral incident JSON; links require explicit evidence."""
        if (type(document.get("schema_version")) is not int or document["schema_version"] != 1 or
                not isinstance(document.get("incidents"), list)):
            raise ProductionEventError("incident document requires schema_version 1 and an incidents array")
        if not isinstance(source, str) or not source.strip() or len(source) > 200:
            raise ProductionEventError("incident source must be a non-empty name")
        when = _timestamp(imported_at or datetime.now(timezone.utc).isoformat(), "imported_at")
        assert when is not None
        count = 0
        with closing(self._connect()) as connection, connection:
            try:
                for incident in document["incidents"]:
                    if not isinstance(incident, Mapping):
                        raise ProductionEventError("each incident must be an object")
                    external_id = _identifier(incident.get("id"), "incident id")
                    service_id = incident.get("service_id")
                    if not isinstance(service_id, str) or not service_id.strip() or len(service_id) > 200:
                        raise ProductionEventError("incident service_id must be explicitly provided")
                    started = _timestamp(incident.get("started_at"), "incident started_at")
                    resolved = _timestamp(incident.get("resolved_at"), "incident resolved_at", optional=True)
                    assert started is not None
                    if resolved is not None and (
                            datetime.fromisoformat(resolved.replace("Z", "+00:00")) <
                            datetime.fromisoformat(started.replace("Z", "+00:00"))):
                        raise ProductionEventError("incident resolved_at must not precede started_at")
                    status = incident.get("status")
                    if status not in _INCIDENT_STATUSES:
                        raise ProductionEventError("incident status is invalid")
                    provenance = _provenance(source, "neutral-json/v1", when)
                    connection.execute("""INSERT INTO production_incidents
                      (schema_version,source,external_id,service_id,started_at,resolved_at,status,provenance)
                      VALUES (1,?,?,?,?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET
                       service_id=excluded.service_id,started_at=excluded.started_at,
                       resolved_at=excluded.resolved_at,status=excluded.status,provenance=excluded.provenance""",
                      (source, external_id, service_id.strip(), started, resolved, status, provenance))
                    # Corrections replace link evidence for this imported incident.
                    connection.execute("DELETE FROM production_incident_links WHERE source=? AND incident_external_id=?",
                                       (source, external_id))
                    links = incident.get("links", [])
                    if not isinstance(links, list):
                        raise ProductionEventError("incident links must be an array")
                    seen_links: set[tuple[str, str, str, str]] = set()
                    for link in links:
                        if not isinstance(link, Mapping):
                            raise ProductionEventError("incident link must be an object")
                        target_type = link.get("type")
                        if target_type not in {"deployment", "commit"}:
                            raise ProductionEventError("incident link type must be deployment or commit")
                        evidence = link.get("evidence")
                        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500:
                            raise ProductionEventError("incident links require explicit evidence")
                        target_repository = link.get("repository")
                        if target_type == "deployment" and (not isinstance(target_repository, str) or
                                                              not _REPOSITORY.fullmatch(target_repository)):
                            raise ProductionEventError("deployment links require an owner/name repository")
                        target_id = _identifier(link.get("id"), "incident link target id")
                        relation = link.get("relation", "related_to")
                        if relation not in {"related_to", "affected_by", "recovered_by"}:
                            raise ProductionEventError("incident link relation is invalid")
                        if target_type == "commit":
                            if not _SHA.fullmatch(target_id):
                                raise ProductionEventError("commit links require a full hexadecimal SHA")
                            # Deployment commits are stored lowercase; match them.
                            target_id = target_id.lower()
                        # SQLite treats NULL repositories as distinct in the unique key,
                        # so commit-link duplicates are rejected here.
                        link_key = (str(target_type), str(target_repository or ""), target_id, str(relation))
                        if link_key in seen_links:
                            raise ProductionEventError("incident has a duplicate link")
                        seen_links.add(link_key)
                        connection.execute("""INSERT INTO production_incident_links
                          (source,incident_external_id,target_type,target_repository,target_external_id,relation,evidence,provenance)
                          VALUES (?,?,?,?,?,?,?,?)""",
                          (source, external_id, target_type, target_repository, target_id, relation,
                           evidence.strip(), provenance))
                    count += 1
            except (sqlite3.Error, ProductionEventError):
                connection.rollback()
                raise
        return count

    def deployments(self, *, service_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM production_deployments"
        values: tuple[Any, ...] = ()
        if service_id is not None:
            query += " WHERE service_id=?"
            values = (service_id,)
        query += " ORDER BY created_at, repository, external_id"
        with closing(self._connect()) as connection, connection:
            return [dict(row) for row in connection.execute(query, values)]

    def incidents(self, *, service_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM production_incidents"
        values: tuple[Any, ...] = ()
        if service_id is not None:
            query += " WHERE service_id=?"
            values = (service_id,)
        query += " ORDER BY started_at, source, external_id"
        with closing(self._connect()) as connection, connection:
            return [dict(row) for row in connection.execute(query, values)]


def github_api_json(path: str) -> Any:
    """Read one GitHub REST response using the user's existing ``gh`` login."""
    if not path.startswith("repos/") or ".." in path:
        raise ProductionEventError("GitHub API path must be a repository endpoint")
    result = subprocess.run(["gh", "api", path], check=False, capture_output=True, text=True)
    if result.returncode:
        raise ProductionEventError(f"gh api failed ({result.returncode}): {result.stderr.strip()[:300]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProductionEventError("gh api returned invalid JSON") from error


def _paginated(fetch: Callable[[str], Any], endpoint: str) -> list[Mapping[str, Any]]:
    results: list[Mapping[str, Any]] = []
    page = 1
    while True:
        data = fetch(f"{endpoint}?per_page=100&page={page}")
        if not isinstance(data, list) or any(not isinstance(row, Mapping) for row in data):
            raise ProductionEventError("GitHub list endpoint must return an array of objects")
        results.extend(data)
        if len(data) < 100:
            return results
        page += 1


def import_github_deployments(store: ProductionEventStore, repository: str,
                              mapping: Mapping[str, Any], *,
                              fetch_json: Callable[[str], Any] = github_api_json,
                              imported_at: str | None = None) -> int:
    """Fetch deployments and each status history, then atomically import them."""
    if not _REPOSITORY.fullmatch(repository):
        raise ProductionEventError("repository must be owner/name")
    deployments = _paginated(fetch_json, f"repos/{repository}/deployments")
    statuses: dict[str, list[Mapping[str, Any]]] = {}
    for deployment in deployments:
        external_id = _identifier(deployment.get("id"), "deployment id")
        statuses[external_id] = _paginated(
            fetch_json, f"repos/{repository}/deployments/{external_id}/statuses")
    return store.import_github(repository, deployments, statuses, mapping, imported_at=imported_at)


def import_incident_file(store: ProductionEventStore, path: str | Path, *,
                         source: str) -> int:
    """Read a neutral, versioned incident file and reconcile its events."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProductionEventError(f"cannot read incident document: {error}") from error
    if not isinstance(document, Mapping):
        raise ProductionEventError("incident document must be a JSON object")
    return store.import_incident_document(document, source=source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import production deployment and incident events")
    parser.add_argument("--database", help="telemetry SQLite database (default: configured telemetry database)")
    commands = parser.add_subparsers(dest="command", required=True)
    github = commands.add_parser("import-github", help="import GitHub deployment and status events")
    github.add_argument("repository", help="GitHub owner/name repository")
    github.add_argument("--mapping", required=True, help="versioned JSON environment mapping")
    incidents = commands.add_parser("import-incidents", help="import versioned neutral incident JSON")
    incidents.add_argument("file", help="incident JSON file")
    incidents.add_argument("--source", required=True, help="stable external incident source name")
    args = parser.parse_args(argv)
    try:
        from telemetry import Telemetry, default_database_path

        database = Path(args.database) if args.database else default_database_path()
        # Initialize or migrate the existing telemetry database before adding
        # the event tables; no second database or service is introduced.
        Telemetry(database)
        store = ProductionEventStore(database)
        if args.command == "import-github":
            count = import_github_deployments(
                store, args.repository, load_deployment_mapping(args.mapping))
        else:
            count = import_incident_file(store, args.file, source=args.source)
        print(json.dumps({"imported": count, "database": str(database)}, sort_keys=True))
        return 0
    except (ProductionEventError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
