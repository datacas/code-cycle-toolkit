"""Tests for production delivery event imports and reconciliation."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import production_events as pe  # noqa: E402


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

    def rows(self, sql: str) -> list[tuple]:
        # Close explicitly: Windows cannot delete a database file that is still open.
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql).fetchall()

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
        history = self.rows("SELECT classification,service_id FROM production_deployment_classifications "
                            "ORDER BY classified_at")
        self.assertEqual(history, [("production", "checkout"), ("production", "payments")])

    def test_returning_to_an_earlier_mapping_keeps_it_as_the_latest_classification(self) -> None:
        event = self.deployment(8, "prod-east")
        other = json.loads(json.dumps(self.mapping))
        other["deployment_mapping"]["github"]["repositories"]["acme/service"][
            "environments"]["prod-east"]["service_id"] = "payments"
        for mapping, when in ((self.mapping, "2026-10-27T00:00:00Z"), (other, "2026-10-28T00:00:00Z"),
                              (self.mapping, "2026-10-29T00:00:00Z")):
            self.store.import_github("acme/service", [event], {}, mapping, imported_at=when)
        history = self.rows("SELECT service_id,classified_at FROM production_deployment_classifications "
                            "ORDER BY classified_at")
        self.assertEqual(history, [("payments", "2026-10-28T00:00:00Z"), ("checkout", "2026-10-29T00:00:00Z")])
        self.assertEqual(self.store.deployments()[0]["service_id"], "checkout")

    def test_deployment_and_statuses_are_distinct_and_idempotent(self) -> None:
        deployment = self.deployment(17, "prod-east")
        statuses = {"17": [
            {"id": 101, "state": "in_progress", "created_at": NOW},
            {"id": 102, "state": "success", "created_at": "2026-10-27T12:05:00Z"},
        ]}
        self.assertEqual(self.import_deployments([deployment], statuses), 1)
        self.assertEqual(self.import_deployments([deployment], statuses), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployments"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_statuses"), [(2,)])
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_commits"), [(1,)])

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
        rows = self.rows("SELECT deployment_external_id,commit_sha,relation_kind,method,references_json "
                         "FROM production_deployment_commits ORDER BY deployment_external_id,commit_sha")
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
        rows = self.rows("SELECT deployment_external_id,state FROM production_deployment_statuses "
                         "ORDER BY deployment_external_id,created_at")
        self.assertEqual(rows, [("1", "success"), ("2", "failure"), ("2", "success"), ("3", "success")])
        self.assertEqual(self.rows("SELECT relation FROM production_deployment_links"), [("rollback",)])

    def test_relations_to_unknown_deployments_are_rejected_as_event_errors(self) -> None:
        self.import_deployments([self.deployment(1, "prod-east")])
        with self.assertRaisesRegex(pe.ProductionEventError, "has not been imported"):
            self.store.link_deployments("acme/service", 1, 404, relation="rollback", evidence="names it")
        with self.assertRaisesRegex(pe.ProductionEventError, "has not been imported"):
            self.store.add_commit_relation("acme/service", 404, SHA_B, relation_kind="observed",
                                           method="manifest", references=(), evidence_source="fixture:manifest")
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_links"), [(0,)])

    def incident_doc(self, *, incident_id: str = "INC-1", links: list | None = None,
                     started_at: str = NOW, resolved_at: str | None = None) -> dict:
        return {"schema_version": 1, "incidents": [{"id": incident_id,
                "service_id": "checkout", "started_at": started_at,
                "resolved_at": resolved_at, "status": "open", "links": links or []}]}

    def test_incident_without_known_deployment_is_preserved(self) -> None:
        self.store.import_incident_document(self.incident_doc(), source="incident-feed", imported_at=NOW)
        self.assertEqual(len(self.store.incidents(service_id="checkout")), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_commits"), [(0,)])

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
        self.assertEqual(self.rows("SELECT count(*) FROM production_incidents"), [(1,)])
        self.assertEqual(self.rows("SELECT target_type,target_external_id,relation FROM production_incident_links"),
                         [("deployment", "17", "recovered_by")])
        self.assertEqual(self.rows("SELECT resolved_at FROM production_incidents"), [("2026-10-27T13:00:00Z",)])

    def test_incident_commit_links_are_lowercased_and_duplicates_rejected(self) -> None:
        link = {"type": "commit", "id": SHA_A.upper(), "evidence": "report names this SHA"}
        self.store.import_incident_document(self.incident_doc(links=[link]), source="incident-feed",
                                            imported_at=NOW)
        self.assertEqual(self.rows("SELECT target_external_id FROM production_incident_links"), [(SHA_A,)])
        with self.assertRaisesRegex(pe.ProductionEventError, "duplicate link"):
            self.store.import_incident_document(self.incident_doc(links=[link, link | {"id": SHA_A}]),
                                                source="incident-feed", imported_at=NOW)
        self.assertEqual(self.rows("SELECT count(*) FROM production_incident_links"), [(1,)])

    def test_incident_resolved_before_started_is_rejected(self) -> None:
        with self.assertRaisesRegex(pe.ProductionEventError, "must not precede"):
            self.store.import_incident_document(
                self.incident_doc(started_at="2026-10-27T12:00:00Z", resolved_at="2026-10-27T11:00:00Z"),
                source="incident-feed", imported_at=NOW)
        self.assertEqual(self.store.incidents(), [])

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
        self.assertEqual(self.rows("SELECT state FROM production_deployment_statuses"), [("success",)])

    def test_pagination_follows_full_pages_until_a_short_one(self) -> None:
        pages = {1: [{"id": index} for index in range(100)], 2: [{"id": 100}]}
        calls: list[str] = []
        def fetch(path: str):
            calls.append(path)
            return pages[int(path.rsplit("page=", 1)[1])]
        result = pe._paginated(fetch, "repos/acme/service/deployments")
        self.assertEqual(len(result), 101)
        self.assertEqual(calls, ["repos/acme/service/deployments?per_page=100&page=1",
                                 "repos/acme/service/deployments?per_page=100&page=2"])

    def test_github_api_json_runs_gh_without_a_shell_and_validates_output(self) -> None:
        with self.assertRaises(pe.ProductionEventError):
            pe.github_api_json("user")
        done = subprocess.CompletedProcess(["gh"], 0, stdout='[{"id": 1}]', stderr="")
        with mock.patch.object(pe.subprocess, "run", return_value=done) as run:
            self.assertEqual(pe.github_api_json("repos/acme/service/deployments"), [{"id": 1}])
        self.assertEqual(run.call_args.args[0], ["gh", "api", "repos/acme/service/deployments"])
        failed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="HTTP 404")
        with mock.patch.object(pe.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(pe.ProductionEventError, "gh api failed"):
                pe.github_api_json("repos/acme/service/deployments")

    def test_cli_imports_incidents_into_the_telemetry_database(self) -> None:
        database = Path(self.temporary.name) / "cli.sqlite"
        document = Path(self.temporary.name) / "incidents.json"
        document.write_text(json.dumps(self.incident_doc()), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            status = pe.main(["--database", str(database), "import-incidents", str(document),
                              "--source", "incident-feed"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["imported"], 1)
        self.assertEqual(len(pe.ProductionEventStore(database).incidents()), 1)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            pe.main(["--database", str(database), "import-incidents", str(document.with_name("missing.json")),
                     "--source", "incident-feed"])

    def relations_doc(self, **overrides: object) -> dict:
        document = {
            "schema_version": 1, "repository": "acme/service",
            "commit_inclusions": [
                {"deployment_id": 1, "commit_sha": SHA_B.upper(), "kind": "observed",
                 "method": "release_manifest", "evidence": "manifest of release 1 lists the commit"},
                {"deployment_id": 2, "commit_sha": SHA_A, "kind": "derived", "method": "explicit_compare",
                 "references": [SHA_A], "evidence": "compare view between deployments 1 and 2"}],
            "deployment_links": [
                {"deployment_id": 3, "target_deployment_id": 2, "relation": "rollback",
                 "evidence": "deployment 3 description names it as a rollback of 2"},
                {"deployment_id": 4, "target_deployment_id": 2, "relation": "corrective",
                 "evidence": "deployment 4 description names it as the fix for 2"}]}
        return document | overrides

    def import_relation_fixtures(self) -> None:
        later = {"created_at": "2026-10-27T12:01:00Z"}
        self.import_deployments([self.deployment(1, "prod-east", SHA_A),
                                 self.deployment(2, "prod-east", SHA_B) | later,
                                 self.deployment(3, "prod-east", SHA_A) | later,
                                 self.deployment(4, "prod-east", SHA_B) | later])

    def test_relations_import_records_inclusion_rollback_and_corrective_with_evidence(self) -> None:
        self.import_relation_fixtures()
        self.assertEqual(self.store.import_relations_document(
            self.relations_doc(), source="release-notes", imported_at=NOW), 4)
        commits = self.rows("SELECT deployment_external_id,commit_sha,relation_kind,method,references_json "
                            "FROM production_deployment_commits WHERE method!='deployment_sha' "
                            "ORDER BY deployment_external_id")
        self.assertEqual(commits, [("1", SHA_B, "observed", "release_manifest", "[]"),
                                   ("2", SHA_A, "derived", "explicit_compare", json.dumps([SHA_A]))])
        links = self.rows("SELECT deployment_external_id,target_external_id,relation,evidence "
                          "FROM production_deployment_links ORDER BY deployment_external_id")
        self.assertEqual([row[:3] for row in links], [("3", "2", "rollback"), ("4", "2", "corrective")])
        self.assertIn("rollback of 2", links[0][3])
        provenance = self.rows("SELECT provenance FROM production_deployment_links LIMIT 1")[0][0]
        self.assertEqual(json.loads(provenance)["source"], "release-notes")

    def test_relations_import_is_idempotent_and_keeps_original_provenance(self) -> None:
        self.import_relation_fixtures()
        self.store.import_relations_document(self.relations_doc(), source="notes", imported_at=NOW)
        before = self.rows("SELECT provenance FROM production_deployment_links ORDER BY id")
        self.store.import_relations_document(
            self.relations_doc(), source="notes", imported_at="2026-10-30T00:00:00Z")
        self.assertEqual(self.rows("SELECT provenance FROM production_deployment_links ORDER BY id"), before)
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_links"), [(2,)])
        self.assertEqual(self.rows("SELECT count(*) FROM production_relation_history"), [(0,)])

    def test_relation_correction_keeps_the_original_evidence_in_history(self) -> None:
        self.import_relation_fixtures()
        self.store.import_relations_document(self.relations_doc(), source="notes", imported_at=NOW)
        corrected = self.relations_doc()
        corrected["deployment_links"][0]["evidence"] = "revised: the incident review names 3 as the rollback"
        corrected["commit_inclusions"][0]["method"] = "release_manifest_v2"
        self.store.import_relations_document(corrected, source="notes", imported_at="2026-10-30T00:00:00Z")
        current = self.rows("SELECT evidence FROM production_deployment_links WHERE deployment_external_id='3'")
        self.assertIn("revised", current[0][0])
        history = self.rows("SELECT relation_table,subject,relation,method,evidence "
                            "FROM production_relation_history ORDER BY relation_table")
        self.assertEqual(history, [
            ("commit", SHA_B, "observed", "release_manifest", "manifest of release 1 lists the commit"),
            ("deployment_link", "2", "rollback", None, "deployment 3 description names it as a rollback of 2")])
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_links"), [(2,)])

    def test_observed_and_derived_inclusion_of_one_commit_stay_distinct(self) -> None:
        self.import_relation_fixtures()
        document = self.relations_doc(deployment_links=[], commit_inclusions=[
            {"deployment_id": 2, "commit_sha": SHA_A, "kind": "observed", "method": "release_manifest",
             "evidence": "manifest"},
            {"deployment_id": 2, "commit_sha": SHA_A, "kind": "derived", "method": "explicit_compare",
             "references": [SHA_A], "evidence": "compare"}])
        self.store.import_relations_document(document, source="notes", imported_at=NOW)
        self.assertEqual(self.rows("SELECT relation_kind FROM production_deployment_commits "
                                   "WHERE deployment_external_id='2' AND commit_sha=%r AND method!='deployment_sha' "
                                   "ORDER BY relation_kind" % SHA_A), [("derived",), ("observed",)])

    def test_relations_to_unimported_deployments_are_reported_and_nothing_is_stored(self) -> None:
        self.import_relation_fixtures()
        document = self.relations_doc()
        document["deployment_links"].append({"deployment_id": 9, "target_deployment_id": 404,
                                             "relation": "rollback", "evidence": "names it"})
        with self.assertRaisesRegex(pe.ProductionEventError, "not been imported: 404, 9"):
            self.store.import_relations_document(document, source="notes", imported_at=NOW)
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_links"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_commits "
                                   "WHERE method!='deployment_sha'"), [(0,)])
        # A late deployment makes the same document importable without any guessing.
        self.import_deployments([self.deployment(9, "prod-east"), self.deployment(404, "prod-east")])
        self.assertEqual(self.store.import_relations_document(document, source="notes", imported_at=NOW), 5)

    def test_relations_without_evidence_or_with_unknown_fields_are_rejected(self) -> None:
        self.import_relation_fixtures()
        broken = self.relations_doc()
        broken["deployment_links"][0]["evidence"] = " "
        with self.assertRaisesRegex(pe.ProductionEventError, "explicit evidence"):
            self.store.import_relations_document(broken, source="notes")
        broken = self.relations_doc()
        broken["commit_inclusions"][1]["references"] = []
        with self.assertRaisesRegex(pe.ProductionEventError, "reference commits"):
            self.store.import_relations_document(broken, source="notes")
        broken = self.relations_doc()
        broken["commit_inclusions"][0]["inferred_from"] = "git ancestry"
        with self.assertRaisesRegex(pe.ProductionEventError, "unknown keys"):
            self.store.import_relations_document(broken, source="notes")
        for bad in (self.relations_doc(schema_version=2), self.relations_doc(repository="nope"),
                    self.relations_doc(deployment_links="x"), self.relations_doc(unexpected=[])):
            with self.assertRaises(pe.ProductionEventError):
                self.store.import_relations_document(bad, source="notes")
        with self.assertRaises(pe.ProductionEventError):
            self.store.import_relations_document(self.relations_doc(), source=" ")
        self.assertEqual(self.rows("SELECT count(*) FROM production_deployment_links"), [(0,)])

    def test_derived_inclusion_in_a_relations_file_still_needs_an_earlier_reference(self) -> None:
        self.import_relation_fixtures()
        document = self.relations_doc(deployment_links=[], commit_inclusions=[
            {"deployment_id": 1, "commit_sha": SHA_B, "kind": "derived", "method": "explicit_compare",
             "references": [SHA_B], "evidence": "compare"}])
        with self.assertRaisesRegex(pe.ProductionEventError, "earlier deployment"):
            self.store.import_relations_document(document, source="notes", imported_at=NOW)

    def test_cli_imports_relations_into_the_telemetry_database(self) -> None:
        database = Path(self.temporary.name) / "cli.sqlite"
        store = pe.ProductionEventStore(database)
        store.import_github("acme/service", [self.deployment(1, "prod-east"), self.deployment(2, "prod-east")],
                            {}, self.mapping, imported_at=NOW)
        document = Path(self.temporary.name) / "relations.json"
        document.write_text(json.dumps(self.relations_doc(commit_inclusions=[], deployment_links=[
            {"deployment_id": 2, "target_deployment_id": 1, "relation": "rollback", "evidence": "names it"}])),
            encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            status = pe.main(["--database", str(database), "import-relations", str(document),
                              "--source", "release-notes"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["imported"], 1)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            pe.main(["--database", str(database), "import-relations", str(document.with_name("missing.json")),
                     "--source", "release-notes"])

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
