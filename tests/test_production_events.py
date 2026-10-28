"""Tests for production delivery event imports and reconciliation."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import production_events as pe


NOW = "2026-10-27T12:00:00Z"
SHA_A = "a" * 40
SHA_B = "b" * 40


class ProductionEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "telemetry.sqlite"
        self.store = pe.ProductionEventStore(self.database)
        self.mapping = {
            "schema_version": 1,
            "deployment_mapping": {"github": {"repositories": {
                "acme/service": {"environments": {
                    "prod-east": {"classification": "production", "service_id": "checkout"},
                    "staging": {"classification": "staging", "service_id": "checkout"},
                    "ci": {"classification": "ci", "service_id": "builds"},
                    "canary": {"classification": "other", "service_id": "checkout"},
                }}
            }}}
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def deployment(self, deployment_id: int, environment: str, sha: str = SHA_A,
                   *, production_environment: bool | None = None) -> dict:
        return {"id": deployment_id, "environment": environment, "sha": sha,
                "ref": "main", "task": "deploy", "created_at": NOW,
                "updated_at": NOW, "production_environment": production_environment,
                "payload": {"token": "must-not-be-stored"}}

    def import_deployments(self, deployments: list[dict], statuses: dict | None = None) -> int:
        return self.store.import_github("acme/service", deployments, statuses or {},
                                        self.mapping, imported_at=NOW)

    def test_environment_mapping_is_explicit_and_github_flag_is_not_authoritative(self) -> None:
        rows = [self.deployment(1, "prod-east", production_environment=False),
                self.deployment(2, "staging", production_environment=True),
                self.deployment(3, "ci"), self.deployment(4, "canary"),
                self.deployment(5, "unconfigured", production_environment=True)]
        self.import_deployments(rows)
        result = {row["external_id"]: (row["classification"], row["service_id"])
                  for row in self.store.deployments()}
        self.assertEqual(result["1"], ("production", "checkout"))
        self.assertEqual(result["2"], ("staging", "checkout"))
        self.assertEqual(result["3"], ("ci", "builds"))
        self.assertEqual(result["4"], ("other", "checkout"))
        self.assertEqual(result["5"], ("unknown", None))
        stored = json.dumps(self.store.deployments())
        self.assertNotIn("must-not-be-stored", stored)

    def test_mapping_corrections_are_traceable_without_rewriting_original_environment(self) -> None:
        event = self.deployment(8, "prod-east", production_environment=False)
        self.import_deployments([event])
        corrected_mapping = json.loads(json.dumps(self.mapping))
        repository_config = corrected_mapping["deployment_mapping"]["github"]["repositories"]["acme/service"]
        repository_config["environments"]["prod-east"]["service_id"] = "payments"
        self.store.import_github("acme/service", [event], {}, corrected_mapping, imported_at="2026-10-28T00:00:00Z")
        current = self.store.deployments()[0]
        self.assertEqual((current["environment"], current["classification"], current["service_id"]),
                         ("prod-east", "production", "payments"))
        with sqlite3.connect(self.database) as connection:
            history = connection.execute("SELECT classification,service_id FROM production_deployment_classifications "
                                         "ORDER BY id").fetchall()
        self.assertEqual(history, [("production", "checkout"), ("production", "payments")])

    def test_deployment_and_statuses_are_distinct_and_idempotent(self) -> None:
        deployment = self.deployment(17, "prod-east")
        statuses = {"17": [
            {"id": 101, "state": "in_progress", "created_at": NOW},
            {"id": 102, "state": "success", "created_at": "2026-10-27T12:05:00Z"},
        ]}
        self.assertEqual(self.import_deployments([deployment], statuses), 1)
        self.assertEqual(self.import_deployments([deployment], statuses), 1)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM production_deployments").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM production_deployment_statuses").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM production_deployment_commits").fetchone()[0], 1)

    def test_many_to_many_commit_relations_keep_evidence_kind_and_references(self) -> None:
        first = self.deployment(1, "prod-east", SHA_A)
        second = self.deployment(2, "prod-east", SHA_B) | {"created_at": "2026-10-27T12:01:00Z"}
        self.import_deployments([first, second])
        self.store.add_commit_relation("acme/service", 1, SHA_B, relation_kind="observed",
                                       method="deployment_manifest", references=(),
                                       evidence_source="fixture:manifest")
        self.store.add_commit_relation("acme/service", 2, SHA_A, relation_kind="derived",
                                       method="explicit_compare", references=[SHA_A, SHA_B],
                                       evidence_source="fixture:comparison")
        with sqlite3.connect(self.database) as connection:
            rows = connection.execute("SELECT deployment_external_id,commit_sha,relation_kind,method,references_json "
                                      "FROM production_deployment_commits ORDER BY deployment_external_id,commit_sha").fetchall()
        self.assertEqual(len(rows), 4)
        derived = next(row for row in rows if row[0] == "2" and row[2] == "derived")
        self.assertEqual(derived[3], "explicit_compare")
        self.assertEqual(json.loads(derived[4]), [SHA_A, SHA_B])
        with self.assertRaises(pe.ProductionEventError):
            self.store.add_commit_relation("acme/service", 2, SHA_B, relation_kind="derived",
                                           method="git_ancestry", references=(), evidence_source="")

    def test_failed_deployment_can_be_followed_by_verified_rollback(self) -> None:
        rollback = self.deployment(3, "prod-east", SHA_A) | {"created_at": "2026-10-27T12:02:00Z"}
        deployments = [self.deployment(1, "prod-east", SHA_A),
                       self.deployment(2, "prod-east", SHA_B), rollback]
        statuses = {"1": [{"id": 1, "state": "success", "created_at": NOW}],
                    "2": [{"id": 2, "state": "failure", "created_at": "2026-10-27T12:01:00Z"},
                          {"id": 3, "state": "success", "created_at": "2026-10-27T12:02:00Z"}],
                    "3": [{"id": 4, "state": "success", "created_at": "2026-10-27T12:03:00Z"}]}
        self.import_deployments(deployments, statuses)
        with self.assertRaisesRegex(pe.ProductionEventError, "explicit evidence"):
            self.store.link_deployments("acme/service", 3, 2, relation="rollback", evidence="")
        self.store.link_deployments("acme/service", 3, 2, relation="rollback",
                                    evidence="deployment record describes rollback of deployment 2")
        with sqlite3.connect(self.database) as connection:
            rows = connection.execute("SELECT deployment_external_id,state FROM production_deployment_statuses "
                                      "ORDER BY deployment_external_id,created_at").fetchall()
        self.assertEqual(rows, [("1", "success"), ("2", "failure"), ("2", "success"), ("3", "success")])
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT relation FROM production_deployment_links").fetchone()[0],
                             "rollback")

    def incident_doc(self, *, incident_id: str = "INC-1", links: list | None = None,
                     started_at: str = NOW, resolved_at: str | None = None) -> dict:
        return {"schema_version": 1, "incidents": [{"id": incident_id,
                "service_id": "checkout", "started_at": started_at,
                "resolved_at": resolved_at, "status": "open", "links": links or []}]}

    def test_incident_without_known_deployment_is_preserved(self) -> None:
        self.store.import_incident_document(self.incident_doc(), source="incident-feed", imported_at=NOW)
        self.assertEqual(len(self.store.incidents(service_id="checkout")), 1)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM production_deployment_commits").fetchone()[0], 0)

    def test_incident_links_require_explicit_evidence_and_reconcile_corrections(self) -> None:
        bad = self.incident_doc(links=[{"type": "commit", "id": SHA_A}])
        with self.assertRaisesRegex(pe.ProductionEventError, "explicit evidence"):
            self.store.import_incident_document(bad, source="incident-feed", imported_at=NOW)
        self.assertEqual(self.store.incidents(), [])
        good = self.incident_doc(links=[{"type": "commit", "id": SHA_A,
                                         "evidence": "incident report names this SHA"}])
        self.store.import_incident_document(good, source="incident-feed", imported_at=NOW)
        corrected = self.incident_doc(links=[{"type": "deployment", "id": "17",
                                              "repository": "acme/service",
                                              "relation": "recovered_by",
                                              "evidence": "incident report links deployment 17"}],
                                      resolved_at="2026-10-27T13:00:00Z")
        self.store.import_incident_document(corrected, source="incident-feed", imported_at="2026-10-28T00:00:00Z")
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM production_incidents").fetchone()[0], 1)
            row = connection.execute("SELECT target_type,target_external_id,relation FROM production_incident_links").fetchone()
            self.assertEqual(row, ("deployment", "17", "recovered_by"))
            self.assertEqual(connection.execute("SELECT resolved_at FROM production_incidents").fetchone()[0],
                             "2026-10-27T13:00:00Z")

    def test_invalid_timestamps_and_incomplete_batches_do_not_partially_import(self) -> None:
        with self.assertRaisesRegex(pe.ProductionEventError, "timezone"):
            self.import_deployments([self.deployment(1, "prod-east") | {"created_at": "2026-10-27T12:00:00"}])
        self.assertEqual(self.store.deployments(), [])
        with self.assertRaisesRegex(pe.ProductionEventError, "started_at"):
            self.store.import_incident_document(self.incident_doc(started_at="not-a-time"),
                                                source="incident-feed", imported_at=NOW)
        self.assertEqual(self.store.incidents(), [])

    def test_late_events_are_imported_without_requiring_temporal_links(self) -> None:
        late = self.incident_doc(incident_id="INC-late", started_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.store.import_incident_document(late, source="incident-feed", imported_at=NOW), 1)
        self.assertIsNone(self.store.incidents()[0]["resolved_at"])

    def test_github_api_adapter_paginates_and_fetches_status_histories(self) -> None:
        calls: list[str] = []
        deployment = self.deployment(99, "prod-east")
        def fetch(path: str):
            calls.append(path)
            if path == "repos/acme/service/deployments?per_page=100&page=1":
                return [deployment]
            if path == "repos/acme/service/deployments/99/statuses?per_page=100&page=1":
                return [{"id": 9, "state": "success", "created_at": NOW}]
            raise AssertionError(path)
        self.assertEqual(pe.import_github_deployments(self.store, "acme/service", self.mapping,
                                                      fetch_json=fetch, imported_at=NOW), 1)
        self.assertEqual(len(calls), 2)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT state FROM production_deployment_statuses").fetchone()[0],
                             "success")

    def test_mapping_validation_rejects_implicit_or_invalid_configuration(self) -> None:
        path = Path(self.temporary.name) / "mapping.json"
        path.write_text(json.dumps(self.mapping), encoding="utf-8")
        self.assertEqual(pe.load_deployment_mapping(path), self.mapping)
        broken = {"schema_version": 1, "deployment_mapping": {"github": {"repositories": {
            "acme/service": {"environments": {"prod": {"classification": "production"}}}
        }}}}
        path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaisesRegex(pe.ProductionEventError, "service_id"):
            pe.load_deployment_mapping(path)


if __name__ == "__main__":
    unittest.main()
