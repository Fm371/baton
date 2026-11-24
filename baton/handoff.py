"""Handoff between attempts and providers: WIP commits, Codex's result
files, the progress notes, the hardcoded-key check and the attempt prompt."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import branches, gitutil, services
from .checks import keycheck
from .pathcheck import expand
from .results import HEADER_MAX, read_commit_message
from .state import atomic_write
from .tasks import Task


class HandoffMixin:
    """Methods of baton.runner.Runner, split out of it; they use its attributes."""

    def recover(self, tasks: list[Task]) -> None:
        """A task left 'running' by a crash or reboot: commit its leftovers as
        WIP so the next attempt sees them in git log."""
        for t in tasks:
            ts = self.state.task(t.id)
            if t.runs_agent and ts.status == "running" and gitutil.is_repo(t.repo) and gitutil.is_dirty(t.repo):
                if t.branch and gitutil.current_branch(t.repo) != t.branch:
                    self.log(f"task {t.id}: {t.repo.name} is on '{gitutil.current_branch(t.repo)}', not on "
                             f"{t.branch}; its uncommitted changes are left alone")
                    ts.last_end = ts.last_end or "the runner itself was interrupted (crash, reboot or stop)"
                    continue
                try:
                    h = gitutil.commit_all(t.repo, self.wip_message(t, "work left by an interrupted run"),
                                       secret_patterns=self.cfg.wip_secret_patterns)
                except gitutil.GitError as e:
                    detail = " ".join(str(e).split())[:300]
                    self.commit_failed[str(t.repo)] = detail
                    self.log(f"task {t.id}: ERROR could not recover the uncommitted work in {t.repo.name}, "
                             f"it stays as it is: {detail}")
                    ts.last_end = ts.last_end or "the runner itself was interrupted (crash, reboot or stop)"
                    continue
                self.log(f"task {t.id}: recovered uncommitted work as {h[:8] if h else '?'}")
                ts.last_end = ts.last_end or "the runner itself was interrupted (crash, reboot or stop)"
        self.save()

    # Codex gets one writable folder, its own task's results/<id>/, instead of the
    # whole results/ tree (other tasks' results, and verifier results, stay out
    # of reach). It writes its result file there; baton copies it to results/<id>.md.
    CODEX_RESULT = "codex-result.md"
    CODEX_PROGRESS = "codex-progress.md"
    PROGRESS_MAX = 12_000           # characters of the previous agent's notes put in a prompt

    def agent_result_path(self, task: Task, prov: str, result_path: Path) -> Path:
        if self.cfg.provider_kind(prov) == "codex":
            return self.cfg.results_dir / task.id / self.CODEX_RESULT
        return result_path

    def progress_path(self, task: Task) -> Path:
        """The notes file the agents keep for each other; the next agent's prompt carries it."""
        return self.cfg.results_dir / f"{task.id}.progress.md"

    def agent_progress_path(self, task: Task, prov: str) -> Path:
        return self.cfg.results_dir / task.id / self.CODEX_PROGRESS if self.cfg.provider_kind(prov) == "codex" else self.progress_path(task)

    def clear_codex_files(self, task: Task) -> None:
        for name in (self.CODEX_RESULT, self.CODEX_PROGRESS):
            (self.cfg.results_dir / task.id / name).unlink(missing_ok=True)

    def promote_codex_files(self, task: Task, result_path: Path, since: float) -> None:
        """Copy the files Codex wrote in its task folder to where baton and the next agent read them."""
        for name, dest in ((self.CODEX_RESULT, result_path), (self.CODEX_PROGRESS, self.progress_path(task))):
            src = self.cfg.results_dir / task.id / name
            try:
                if src.is_file() and src.stat().st_mtime >= since - 1:
                    shutil.copyfile(src, dest)
            except OSError as e:
                self.log(f"task {task.id}: could not copy {name}: {e}")

    def previous_notes(self, task: Task) -> str:
        paths = [self.progress_path(task), self.agent_progress_path(task, "codex")]
        path = max((p for p in paths if p.is_file()), key=lambda p: p.stat().st_mtime, default=paths[0])
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return text if len(text) <= self.PROGRESS_MAX else "[... earlier notes cut ...]\n" + text[-self.PROGRESS_MAX:]

    def hardcoded_keys(self, task: Task, result_path: Path) -> list[keycheck.Finding]:
        """String literals the task added that look like data keys, unless the
        result file explains them. Written to results/<id>/hardcoded-keys.md."""
        ts = self.state.task(task.id)
        rc = self.cfg.repos.get(task.repo)
        forbid = rc.forbid_literals if rc else []
        if not ((self.cfg.hardcoded_key_check or forbid) and ts.start_head and gitutil.is_repo(task.repo)):
            return []
        try:
            found = keycheck.scan_repo(task.repo, ts.start_head, self.cfg.verifier_test_paths,
                                       forbid, self.cfg.hardcoded_key_check, started=ts.started)
            text = result_path.read_text(encoding="utf-8", errors="replace")
        except (gitutil.GitError, OSError):
            return []
        path = self.cfg.results_dir / task.id / "hardcoded-keys.md"
        if not found or keycheck.justified(text):
            path.unlink(missing_ok=True)
            return []
        atomic_write(path, "# Possible hardcoded data keys\n\n"
                     "Data keys belong in config or the database. Resolve each one from there, or justify it "
                     "under `## Hardcoded keys` in the result file.\n\n" + "\n".join(f"- `{f}`" for f in found) + "\n")
        return found

    def suspect_now(self, task: Task) -> list[str]:
        """Files the next commit will take that look like they may hold a secret (the warn tier)."""
        try:
            return [gitutil.printable(p) for p in gitutil.suspect_looking(
                task.repo, self.cfg.wip_secret_patterns, self.cfg.wip_warn_patterns)]
        except gitutil.GitError:
            return []

    def note_suspect(self, task: Task, files: list[str]) -> None:
        """Say loudly that secret-looking files went into a commit: log line per file, then the task's
        note, SUMMARY's Warnings block and the on_event payload (all read `suspect_files`)."""
        if not files:
            return
        ts = self.state.task(task.id)
        ts.suspect_files = sorted(set(ts.suspect_files) | set(files))
        for f in files:
            self.log(f"task {task.id}: WARNING committed a file that looks like it may hold a secret: {f} "
                     "(run.wip_warn_patterns); check it before anything is pushed")

    def _commit_wip(self, task: Task, why: str) -> str | None:
        """Commit the WIP (secret-looking files stay out, large files are reported); the commit hash.
        Raises GitError when the commit fails."""
        skipped = [gitutil.printable(p) for p in gitutil.secret_looking(task.repo, self.cfg.wip_secret_patterns)]
        if skipped:
            self.refused[task.id] = sorted(set(self.refused.get(task.id, [])) | set(skipped))
            self.log(f"task {task.id}: WARNING not committing files that look like secrets (they are added "
                     f"to .git/info/exclude when untracked): {', '.join(skipped[:8])}"
                     f"{' …' if len(skipped) > 8 else ''}")
        huge = [gitutil.printable(p) for p in gitutil.big_files(task.repo)]
        if huge:
            self.log(f"task {task.id}: WARNING committing large files (over "
                     f"{gitutil.BIG_FILE_BYTES // 2**20} MB): {', '.join(huge[:5])}")
        suspect = self.suspect_now(task)
        h = gitutil.commit_all(task.repo, self.wip_message(task, why),
                               secret_patterns=self.cfg.wip_secret_patterns)
        if h:
            self.note_suspect(task, suspect)
        return h

    def _uncommittable(self, task: Task, key: str) -> str | None:
        """What a commit cannot take (changes inside a submodule, tracked files that look like secrets),
        when it is new since an earlier task in this repository; None otherwise."""
        try:
            rest = [gitutil.printable(p) for p in gitutil.leftovers(task.repo)]
        except gitutil.GitError:
            rest = []
        known = self.known_leftovers.get(key, set())        # already reported for an earlier task here
        self.known_leftovers[key] = set(rest)
        fresh = [p for p in rest if p not in known]
        if not fresh:
            return None
        return (f"{len(fresh)} change(s) could not be committed and stay in {task.repo.name}: "
                f"{', '.join(fresh[:5])}{' …' if len(fresh) > 5 else ''}")

    def commit_leftovers(self, task: Task, why: str) -> str | None:
        """Commit what the agent left as WIP. Returns why some of the work could not be committed
        (None when all of it was, or when nothing was attempted)."""
        if not self.cfg.commit_leftovers:
            return None
        key = str(task.repo)
        here = gitutil.current_branch(task.repo)
        if task.branch and here != task.branch:
            # never put WIP on a branch that is not the task's: it may be the user's own
            self.log(f"task {task.id}: {task.repo.name} is on '{here or 'a detached HEAD'}', not on {task.branch}; "
                     "its uncommitted changes are not committed")
            return None
        try:
            h = self._commit_wip(task, why)
        except gitutil.GitError as e:
            # the work stays uncommitted, and the next task in this repository cannot switch branch:
            # say so now, and name this failure in that task's note
            detail = " ".join(str(e).split())[:300]
            self.commit_failed[key] = detail
            self.log(f"task {task.id}: ERROR could not commit leftovers, so {task.repo.name} stays dirty and "
                     f"later tasks there will fail: {detail}")
            return detail
        if h:
            self.log(f"task {task.id}: committed leftovers as {h[:8]} ({why})")
        detail = self._uncommittable(task, key)
        if detail:
            self.commit_failed[key] = detail
            self.log(f"task {task.id}: WARNING {detail}")
            return detail
        self.commit_failed.pop(key, None)
        return None

    def note_initial_branch(self, repo: Path) -> None:
        """Remember the branch a repository was on before baton first switched it."""
        key = str(repo)
        if key in self.state.initial_branches:
            return
        here = gitutil.current_branch(repo)
        if here:
            if here == self.state.stray_branches.get(key):
                # an agent left it here earlier: that is not where the user had it
                self.log(f"{repo.name}: on '{here}', a branch an agent left it on; not remembered as yours")
                return
            self.state.initial_branches[key] = here
        else:                                           # a detached HEAD is remembered by its commit
            sha = gitutil.head(repo)
            if sha:
                self.state.initial_branches[key] = f"detached:{sha}"

    def note_stray_branch(self, repo: Path, branch: str) -> None:
        """An agent left the repository on a branch the task did not choose. It is not the user's, so it is
        never recorded as the branch to return to."""
        self.state.stray_branches[str(repo)] = branch
        remembered = self.state.initial_branches.get(str(repo))
        self.log(f"{repo.name}: left on '{branch}' by an agent"
                 + (f"; baton still remembers it was on {remembered.removeprefix('detached:')[:12]} before the run"
                    if remembered else ""))

    def _restore_one(self, key: str, branch: str, ours: set[str]) -> bool:
        """Put one repository back where it was. True: the record can be dropped; False: keep it (the
        repository was left as it is, and the next run can try again)."""
        repo = Path(key)
        sha = branch.removeprefix("detached:") if branch.startswith("detached:") else None
        label = f"the detached HEAD at {sha[:12]}" if sha else branch
        try:
            here = gitutil.current_branch(repo)
            at_start = (not here and gitutil.head(repo) == sha) if sha else (here == branch)
            if at_start:
                return True
            if here not in ours and here != self.state.stray_branches.get(key):
                # the user (or another tool) moved the repository since baton recorded where it was:
                # that branch is theirs, never switch away from it
                self.log(f"{repo.name}: on '{here or 'a detached HEAD'}', which baton did not choose; "
                         f"not returning it to {label}")
                return True
            if gitutil.is_dirty(repo) or not (gitutil.ref_exists(repo, sha) if sha
                                               else gitutil.branch_exists(repo, branch)):
                self.log(f"{repo.name}: left on {here or 'a detached HEAD'}; it was on {label} before "
                         f"the run (check it out yourself)")
                return False
            if sha:
                gitutil.git(repo, "checkout", "-q", "--detach", sha)
                self.log(f"{repo.name}: back on the detached HEAD at {sha[:12]}")
            else:
                gitutil.git(repo, "checkout", "-q", branch)
                self.log(f"{repo.name}: back on {branch} (it was on {here}; set [run] "
                         "restore_branches = false to leave repositories where the run ends)")
            return True
        except gitutil.GitError as e:
            self.log(f"{repo.name}: could not return to {label}: {e}")
            return False

    def restore_branches(self) -> None:
        """When the run ends, put each repository back on the branch it was on before baton touched
        it (a detached HEAD goes back to its commit), so the user's checkout is where they left it.
        A repository with uncommitted changes is left alone (the next run continues there).
        `[run] restore_branches = false` leaves every repository on the last task branch instead."""
        if not self.cfg.restore_branches:
            kept = [f"{Path(k).name} (was on {b.removeprefix('detached:')[:12]})" for k, b in self.state.initial_branches.items()
                    if gitutil.current_branch(Path(k)) != b]
            if kept:
                self.log("restore_branches = false: left where the run ended: " + ", ".join(kept))
            self.state.initial_branches.clear()
            self.state.stray_branches.clear()
            self.save()
            return
        ours = {t.branch for t in getattr(self, "ordered", []) if t.branch}
        for key, branch in list(self.state.initial_branches.items()):
            if self._restore_one(key, branch, ours):
                self.state.initial_branches.pop(key, None)
                self.state.stray_branches.pop(key, None)
        self.save()

    def check_existing_branch(self, task: Task) -> None:
        """A task's branch that already exists is used as it is, which is only right when it holds the work
        the task builds on. `[run] existing_branch` decides what happens when it does not: warn and go on,
        rebase it onto the dependency, or refuse the task (GitError)."""
        if gitutil.current_branch(task.repo) == task.branch:
            return
        by_id = {t.id: t for t in getattr(self, "ordered", [])}
        rep = branches.assess(task.repo, task.branch, task.base or ("HEAD" if task.branch_auto else None),
                              branches.dependency_branches(task, by_id))
        if rep is None:
            return
        self.log(f"task {task.id}: branch {task.branch} already exists ({rep.describe()})")
        if rep.ok:
            return
        mode = self.cfg.existing_branch
        if mode == "warn":
            for problem in rep.problems:
                self.log(f"task {task.id}: WARNING {problem}; using it anyway "
                         f"([run] existing_branch = \"warn\"). {branches.fix_hint(rep)}")
            return
        if mode == "rebase" and rep.rebase_target:
            gitutil.checkout(task.repo, task.branch, None)
            gitutil.rebase_onto(task.repo, task.branch, rep.rebase_target)
            self.log(f"task {task.id}: rebased {task.branch} onto {rep.rebase_target}")
            return
        raise gitutil.GitError(f"{'; '.join(rep.problems)}. {branches.fix_hint(rep)}")

    def keep_attempt_result(self, task: Task, n: int, clear_result: bool = True) -> None:
        """Archive reports/notes before the next attempt; an old verdict must never count as new."""
        if n < 1:
            return
        folder = self.cfg.results_dir / task.id
        folder.mkdir(parents=True, exist_ok=True)
        for src in (self.cfg.results_dir / f"{task.id}.md", folder / self.CODEX_RESULT,
                    folder / self.CODEX_PROGRESS, self.progress_path(task)):
            dest = folder / f"attempt-{n:02d}.{src.name}"
            if src.is_file() and not dest.exists():
                shutil.copy2(src, dest)
        notes = self.previous_notes(task)
        if notes:
            atomic_write(self.progress_path(task), notes)
        if clear_result:
            (self.cfg.results_dir / f"{task.id}.md").unlink(missing_ok=True)

    def archive_attempts(self, task: Task) -> None:
        """Move the attempt files of an earlier run of this task into results/<id>/earlier-N/."""
        folder = self.cfg.results_dir / task.id
        old = [p for p in folder.glob("attempt-*") if p.is_file()] if folder.is_dir() else []
        if not old:
            return
        n = 1
        while (folder / f"earlier-{n}").exists():
            n += 1
        dest = folder / f"earlier-{n}"
        dest.mkdir()
        for p in old:
            shutil.move(str(p), str(dest / p.name))
        self.log(f"task {task.id}: moved {len(old)} files of the earlier run to {dest.name}/")

    def commit_codex_attempt(self, task: Task, result_path: Path, since: float, why: str,
                             status: str | None = None) -> None:
        """Codex cannot write the repository's read-only .git mount, so the
        runner commits its work after the sandboxed process has exited. A real
        commit (the COMMIT line, or a done task) runs the repository's hooks; if
        a hook refuses it, the work is kept as a WIP commit and the task cannot
        end `done`."""
        message = read_commit_message(result_path, since)
        real = bool(message) or status == "done"
        if not message:
            # a finished task is not "unfinished work"
            message = (f"chore({task.id}): codex changes" if status == "done"
                       else self.wip_message(task, why))
        try:
            try:
                suspect = self.suspect_now(task)
                h = gitutil.commit_all(task.repo, message, verify=real,
                                       secret_patterns=self.cfg.wip_secret_patterns)
                if h:
                    self.note_suspect(task, suspect)
            except gitutil.CommitRejected as e:
                detail = " ".join(str(e).split())[:300]
                self.hook_failed[task.id] = detail
                self.log(f"task {task.id}: commit hook refused the Codex commit; keeping the work as WIP: {detail}")
                h = gitutil.commit_all(task.repo, self.wip_message(task, "commit hook failed"),
                                       secret_patterns=self.cfg.wip_secret_patterns)
        except gitutil.GitError as e:
            self.log(f"task {task.id}: could not commit Codex changes: {e}")
            return
        if h:
            self.log(f"task {task.id}: committed Codex changes as {h[:8]}")

    @staticmethod
    def wip_message(task: Task, why: str) -> str:
        """`chore(<id>): wip, <why> (unverified)`: `wip` is not a commitlint type,
        and the header stays within 100 characters."""
        head, tail = f"chore({task.id}): wip, ", " (unverified)"
        room = HEADER_MAX - len(head) - len(tail)
        if len(why) > room:
            why = why[:max(room - 1, 0)].rstrip() + "…"
        return head + why + tail

    def build_prompt(self, task: Task, prov: str, attempt: int, last_end: str,
                     result_path: Path) -> str:
        parts = []
        if self.cfg.common_prompt:
            parts.append(expand(self.cfg.common_prompt.read_text(encoding="utf-8"), self.cfg.base_dir).strip())
        parts.append(task.body.strip())
        foot = [f"Your task id is {task.id}. Your working directory is {task.repo}"
                + (f", on branch {task.branch}" if task.branch else "") + ".",
                f"When you finish, write your result file to {result_path}. Its first line must be "
                "exactly one of `STATUS: done`, `STATUS: partial` or `STATUS: blocked`. "
                "Write it last, after every command has finished.",
                "Run every command in the foreground and wait for it; never leave work running in the background.",
                f"While you work, keep {self.agent_progress_path(task, prov)} up to date: your plan, what is done, "
                "what is next, and open questions or decisions. If you hit a usage limit another agent takes "
                "over from this file and the commits, and has nothing else of your reasoning. Keep it short.",
                "In the result file add a `## Not verified` section: list what you checked only with mocks or "
                "fixtures, and any check you skipped or could not run, one bullet each (write `None.` if there is "
                "nothing). QA starts from that list.",
                "In the result file add a `## Needs from you` section for what a person must do or provide that "
                "you could not: new permission keys, environment variables or settings, migrations, manual "
                "steps, services to start (one bullet each; write `None.` if there is nothing). baton gathers "
                "these at the top of the summary."]
        svc_line = services.prompt_line(self.cfg, services.needed(self.cfg, task))
        if svc_line:
            foot.append(svc_line)
        if self.cfg.provider_kind(prov) == "codex":
            foot.append("Do not run `git commit`: the Codex sandbox mounts `.git` read-only, so baton will "
                        "commit your changes after you exit. Add a line `COMMIT: <conventional commit message>` "
                        "to the result file so baton can use that message.")
        if task.is_verifier:
            foot.append("You are verifying work done by someone else. You may add or change test files only "
                        f"(paths matching {', '.join(self.cfg.verifier_test_paths)}). A change to any other "
                        "file blocks this verification. Your commits stay in this repository, where later "
                        "agents can read them: never commit canary or mutation descriptions or this "
                        "verification file, and name each test by the behavior it checks.")
        if attempt > 1:
            foot.append(f"Preserved HEAD {gitutil.head(task.repo)} on branch {task.branch}; "
                        "inspect the preserved work before repeating any commands.")
            since = self.state.task(task.id).start_head
            rng = f"{since[:12]}..HEAD" if since else "HEAD"
            foot.append(
                f"This task was started before ({last_end or 'the previous attempt did not finish'}). "
                f"Before doing anything, run `git log --stat {rng}` and read any notes left in the "
                f"repository, then continue from there instead of redoing finished work. Commits named "
                f"`chore({task.id}): wip, ...` are unverified: check them, finish or fix them, and never leave "
                "the build broken.")
            notes = self.previous_notes(task)
            if notes:
                foot.append("The previous agent's notes (its progress file) follow. They are its own view, not "
                            "verified: use them to see where it was heading, then check against the code.\n\n"
                            "<previous-agent-notes>\n" + notes + "\n</previous-agent-notes>")
        parts.append("---\n\n" + "\n\n".join(foot))
        return "\n\n---\n\n".join(parts) + "\n"
