"""Change-request history, publication receipts and repair proposals."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from .run_cycle import CycleReport, Reported

class PublicationHistory:
    def lock_change_request(self: Any, request: str) -> CycleReport | None:
        """Hold the per-machine writer lock on `request` until the cycle ends."""
        if self.state.history_store is None or self.state.history_lock is not None:
            return None
        try:
            self.state.history_lock = (self.state.history_store, request, self.state.history_store.acquire(self.repo_id, request))
        except self.api.LockHeld as error:
            return self.stop(f"{error}; another cycle is driving it", "BLOCKED")
        except (self.api.sqlite3.Error, self.api.HistoryError):
            # An unusable store is skipped; the comments remain the record.
            self.state.history_store = None
        return None

    def release_lock(self: Any) -> None:
        # Released through the store that took it, even if the history was
        # given up on since.
        if self.state.history_lock is not None:
            store, request, owner = self.state.history_lock
            with self.api.contextlib.suppress(self.api.sqlite3.Error):
                store.release(self.repo_id, request, owner)

    def expect_publication(self: Any, role: str, request: str | None, attempt_id) -> None:
        if (not self.tracking or self.state.history_store is None or role not in self.api.COMMENT_STAGES
                or not request or not isinstance(attempt_id, str)):
            return
        try:
            self.state.history_store.expect_publication(attempt_id, self.repo_id, request, role)
        except (self.api.sqlite3.Error, self.api.HistoryError):
            self.state.history_store = None

    def retry_directive(self: Any, role: str, request: str | None) -> str:
        """Name earlier attempts of `role` whose publication was never observed."""
        if self.state.history_store is None or role not in self.api.COMMENT_STAGES or not request:
            return ""
        try:
            pending = self.state.history_store.pending_publications(self.repo_id, request, role)
        except self.api.sqlite3.Error:
            self.state.history_store = None
            return ""
        if not pending:
            return ""
        return " " + self.api.RETRY_PUBLICATION.format(
            role=role, ids=", ".join(item["attempt_id"] for item in pending))

    def repair_proposals(self: Any, request: str):
        """Find uniquely provable repairs in trusted GitHub comments."""
        if not self.trusted_authors or self.state.history_store is None or self.code_host != "github":
            return []
        try:
            comments = self.remote.comments(self.repo_id, request)
        except (self.api.CycleDriverError, self.api.HistoryError):
            return []
        trusted = {author.casefold() for author in self.trusted_authors}
        candidates = []
        for comment in comments:
            if (not comment.author or comment.author.casefold() not in trusted
                    or not comment.id):
                continue
            try:
                receipts = self.api.stage_evidence(comment.body)
            except self.api.ContractError:
                continue
            if len(receipts) != 1:
                continue
            receipt = receipts[0]
            if receipt.get("repo") != self.repo_id or receipt.get("stage") != "resolve":
                continue
            start_sha = self.api._safe_repair_start_head(
                self.state.history_store, receipt["attempt_id"], self.repo_id, request)
            proposal = self.api.triage_repair.propose_with_start(
                self.repo_id, comment.id, comment.body, start_sha)
            if proposal is None:
                continue
            url = comment.url or (
                f"https://github.com/{self.repo_id}/pull/{request}#issuecomment-{comment.id}")
            diff = "\n".join(self.api.difflib.unified_diff(
                proposal.original.splitlines(), proposal.repaired.splitlines(),
                fromfile="original", tofile="proposed", lineterm=""))
            record = {
                "repo": self.repo_id, "comment_id": comment.id, "comment_url": url,
                "original_content": proposal.original, "repaired_content": proposal.repaired,
                "original_line": proposal.run_line, "repaired_line": proposal.repaired_line,
                "sha": proposal.sha, "evidence": proposal.evidence, "diff": diff,
                "digest": proposal.digest,
            }
            candidates.append((record, proposal))
        return candidates

    def audit_repair(self: Any, record: dict[str, Any], decision: str, result: str) -> None:
        self.telemetry.record_triage_repair(
            repo=self.repo_id, pull_number=str(self.state.change_request_id),
            comment_id=record["comment_id"],
            original_sha256=self.api.hashlib.sha256(record["original_content"].encode("utf-8")).hexdigest(),
            repaired_sha256=self.api.hashlib.sha256(record["repaired_content"].encode("utf-8")).hexdigest(),
            proposal_sha256=record["digest"], triaged_sha=record["sha"],
            repair_type=record["evidence"], decision=decision, result=result)

    def attach_repair_proposal(self: Any, request: str) -> None:
        """Attach one bounded, attempt-proven GitHub comment repair proposal."""
        candidates = self.repair_proposals(request)
        if len(candidates) != 1:
            return
        record, _ = candidates[0]
        if len(self.api.json.dumps(record, ensure_ascii=False)) <= 12000:
            self.report.repair_proposal = record
            self.audit_repair(record, "proposed", "unverified")
            self.journal.record("decision_asked", component="repair",
                           decision_key=f"repair:{record['comment_id']}:{record['digest']}",
                           status="unanswered", reason="Approve the content-bound comment repair",
                           detail=record["diff"])
        else:
            self.report.note("assisted repair proposal exceeds report bounds; use manual repair")

    def after_publication(self: Any, role: str, request: str, attempt_id,
                          reported: Reported) -> CycleReport | None:
        """Reconcile the history after a publishing stage and check its outbox.

        A receipt from an attempt nobody here dispatched is another writer:
        the history is already reconciled, and the cycle stops for whoever
        owns the change request. Failed history reconciliation blocks after
        publication; when reconciliation succeeds but this attempt's
        publication is absent, the next dispatch of the same stage retries it.
        """
        if not self.tracking:
            return None
        details = self.report.stage_details[-1]
        if self.state.history_store is None:
            try:
                comments = self.remote.comments(self.repo_id, request)
                for comment in comments:
                    if comment.author and comment.author.casefold() in {
                            author.casefold() for author in self.trusted_authors}:
                        self.api.validate_triage_comment(comment.body)
            except self.api.ContractError as error:
                self.attach_repair_proposal(request)
                return self.stop(
                    f"published {role} comment cannot be read: contract validation failed: {error}",
                    "BLOCKED", reported=reported)
            except self.api.CycleDriverError as error:
                return self.stop(
                    f"published {role} comment validation could not complete: {error}",
                    "BLOCKED", reported=reported)
            return None
        try:
            synced = self.state.history_store.sync(self.repo_id, request, trusted_authors=self.trusted_authors,
                                        remote=self.remote)
            published = (self.state.history_store.publication(attempt_id)
                         if isinstance(attempt_id, str) else None)
        except self.api.ContractError as error:
            self.attach_repair_proposal(request)
            return self.stop(
                f"published {role} comment cannot be read: contract validation failed: {error}",
                "BLOCKED", reported=reported)
        except (self.api.CycleDriverError, self.api.HistoryError) as error:
            return self.stop(
                f"published {role} comment validation could not complete: {error}",
                "BLOCKED", reported=reported)
        except self.api.sqlite3.Error:
            self.state.history_store = None
            return None
        if synced.foreign:
            return self.stop(
                f"change request {request} received a publication from attempt(s) "
                f"{', '.join(synced.foreign)}, which this machine did not dispatch, "
                f"while {role} ran; the local history was reconciled, resume once "
                "the other writer has finished", "BLOCKED", reported=reported)
        if isinstance(attempt_id, str) and (published is None
                                            or published["state"] != "published"):
            details.warnings.append(
                f"the {role} publication of attempt {attempt_id} was not observed on "
                f"change request {request}; the next {role} retries it")
        return None

    def seed_history(self: Any) -> CycleReport | None:
        """Rebuild the claim and survival history of a resumed change request.

        An unreadable history is never an empty one. Without trusted authors
        nobody can say whose comments count, so a person decides; a failed
        fetch, a failed freshness check, a comment without an author, or a
        trusted contract comment that cannot be parsed or placed blocks. No
        contract comment at all is an empty history. A local history is used
        only when verified; an unusable database is skipped and the history
        rebuilt from the comments. Nothing is dispatched on a stop.
        """
        if not any(author.strip() for author in self.trusted_authors):
            return self.stop(
                "the claimed-fix history of the resumed change request cannot be "
                "rebuilt: review.trusted_authors is empty",
                self.api.UNRESOLVED_END)
        stopped = self.lock_change_request(self.state.change_request_id)
        if stopped is not None:
            return stopped
        try:
            # A remote that cannot be checked could never verify the store.
            if self.state.history_store is not None and not isinstance(self.remote, self.api.ReaderRemote):
                try:
                    self.history.extend(self.state.history_store.sync(
                        self.repo_id, self.state.change_request_id, trusted_authors=self.trusted_authors,
                        remote=self.remote).runs)
                    return None
                except (self.api.sqlite3.Error, self.api.HistoryError):
                    # Unusable or unstorable: the comments remain the record.
                    self.state.history_store = None
            self.history.extend(self.api.comment_run_history(
                self.remote.comments(self.repo_id, self.state.change_request_id),
                trusted_authors=self.trusted_authors))
        except (self.api.CycleDriverError, self.api.ContractError, self.api.HistoryError) as error:
            return self.stop(
                f"the claimed-fix history of the resumed change request is "
                f"unreadable: {error}", "BLOCKED")
        return None
