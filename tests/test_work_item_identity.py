"""Characterization and specification tests for WorkItemIdentity (Issue #185).

Pins normalization rules, unknown identity handling, separation by work-item type,
host scope, and grouping identity semantics before and after extraction.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from telemetry import WorkItemIdentity, WORK_ITEM_PROVIDERS


class WorkItemIdentityCharacterizationTests(unittest.TestCase):
    def test_canonical_creation_and_attributes(self) -> None:
        identity = WorkItemIdentity("github", "owner/repo", "120")
        self.assertEqual("github", identity.provider)
        self.assertEqual("owner/repo", identity.repository)
        self.assertEqual("120", identity.work_item_id)
        self.assertEqual("issue", identity.work_item_type)
        self.assertIsNone(identity.host)
        self.assertFalse(identity.is_unknown)

    def test_normalization_repository_case_and_whitespace(self) -> None:
        # Repositories on github are normalized to lowercase and whitespace trimmed
        identity = WorkItemIdentity("GitHub ", " Owner/Repo ", " 120 ")
        self.assertEqual("github", identity.provider)
        self.assertEqual("owner/repo", identity.repository)
        self.assertEqual("120", identity.work_item_id)

    def test_normalization_strip_url_prefixes(self) -> None:
        # HTTPS URL normalized to owner/repo and host extracted
        identity = WorkItemIdentity("github", "https://github.com/owner/repo", "120")
        self.assertEqual("owner/repo", identity.repository)
        self.assertEqual("github.com", identity.host)

        # Trailing .git stripped
        identity_git = WorkItemIdentity("github", "https://github.com/owner/repo.git", "120")
        self.assertEqual("owner/repo", identity_git.repository)

    def test_normalization_strip_git_ssh_url(self) -> None:
        identity = WorkItemIdentity("github", "git@github.com:owner/repo.git", "120")
        self.assertEqual("owner/repo", identity.repository)
        self.assertEqual("github.com", identity.host)

    def test_normalization_strip_hash_from_work_item_id(self) -> None:
        identity = WorkItemIdentity("github", "owner/repo", "#120")
        self.assertEqual("120", identity.work_item_id)

    def test_unknown_identity(self) -> None:
        unknown = WorkItemIdentity.unknown()
        self.assertTrue(unknown.is_unknown)
        self.assertEqual("unknown", unknown.provider)
        self.assertEqual("unknown", unknown.repository)
        self.assertEqual("unknown", unknown.work_item_id)

        empty = WorkItemIdentity("", "", "")
        self.assertTrue(empty.is_unknown)

    def test_separation_by_work_item_type(self) -> None:
        issue = WorkItemIdentity("github", "owner/repo", "120", work_item_type="issue")
        pr = WorkItemIdentity("github", "owner/repo", "120", work_item_type="pull_request")
        self.assertNotEqual(issue, pr)
        self.assertEqual("issue", issue.work_item_type)
        self.assertEqual("pull_request", pr.work_item_type)

    def test_host_scope_separation(self) -> None:
        cloud = WorkItemIdentity("github", "owner/repo", "120", host="github.com")
        enterprise = WorkItemIdentity("github", "owner/repo", "120", host="github.internal.net")
        self.assertNotEqual(cloud, enterprise)
        self.assertEqual("github.com", cloud.host)
        self.assertEqual("github.internal.net", enterprise.host)

    def test_frozen_hashable_for_dict_and_set(self) -> None:
        id1 = WorkItemIdentity("github", "owner/repo", "120")
        id2 = WorkItemIdentity("github", "owner/repo", "120")
        self.assertEqual(id1, id2)
        s = {id1, id2}
        self.assertEqual(1, len(s))
        d = {id1: "resolved"}
        self.assertEqual("resolved", d[id2])

    def test_as_tuple_and_scoped_tuple(self) -> None:
        identity = WorkItemIdentity("github", "owner/repo", "120")
        self.assertEqual(("github", "owner/repo", "120"), identity.as_tuple())
        self.assertEqual(
            ("my-repo-id", "github", "owner/repo", "120"),
            identity.as_scoped_tuple("my-repo-id"),
        )

    def test_string_representation(self) -> None:
        identity = WorkItemIdentity("github", "owner/repo", "120")
        self.assertEqual("github:owner/repo#120", str(identity))

        with_host = WorkItemIdentity("jira", "proj-key", "120", host="jira.corp.com")
        self.assertEqual("jira:jira.corp.com/proj-key#120", str(with_host))

        self.assertEqual("unknown", str(WorkItemIdentity.unknown()))


if __name__ == "__main__":
    unittest.main()
