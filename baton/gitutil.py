"""The few git operations the runner needs. The runner never pushes, resets,
cleans or stashes, and rebases only a task branch that already exists when
`[run] existing_branch = "rebase"` asks for it (see baton/branches.py)."""

from __future__ import annotations

import fnmatch
import os
import signal
import subprocess
from pathlib import Path

GIT_TIMEOUT_S = 300.0           # one git command; a hook or credential prompt that waits forever is cut off
COMMIT_TIMEOUT_S = 1800.0       # `git commit` with the repository's own hooks may run a linter or tests


class GitError(Exception):
    pass


class CommitRejected(GitError):
    """`git commit` refused the commit, normally because a hook failed."""


class UnsafeToCommit(GitError):
    """The work tree is in the middle of a merge, rebase, cherry-pick or revert, or has unmerged paths:
    a commit now would close the operation with conflict markers in it."""


def printable(text: str) -> str:
    """`text` with bytes that are not UTF-8 (git hands those names back as surrogates) made visible,
    so it can go into a log line, a note or the state file."""
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _one_line(text: str, limit: int = 600) -> str:
    text = " ".join(printable(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"        # never wait for a user name or password
    return env


def _label(args: list[str]) -> str:
    rest = list(args)
    if rest[:1] == ["-C"]:
        rest = rest[2:]
    return " ".join(rest[:3])


def _run(args: list[str], cwd: Path | str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run `git <args>`. No stdin, no terminal prompt, a time limit (the whole process group is killed when
    it expires), and output decoded with surrogateescape, so a file name that is not UTF-8 never raises.
    Anything that stops git from running at all is a GitError."""
    cmd = ["git", *args]
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_env(), text=True, encoding="utf-8",
                                errors="surrogateescape", start_new_session=True)
    except FileNotFoundError as e:
        raise GitError("git is not installed or not on PATH") from e
    except (OSError, ValueError, UnicodeError) as e:
        raise GitError(_one_line(f"cannot run git {_label(args)}: {e}")) from e
    limit = timeout if timeout is not None else GIT_TIMEOUT_S
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise GitError(f"git {_label(args)} did not finish in {limit:g} s (a hook or a prompt waiting for "
                       "input?)") from None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def git(repo: Path, *args: str, check: bool = True, timeout: float | None = None) -> str:
    """Stdout of `git -C repo <args>` without its final newline. Leading blanks are kept: the first
    column of `status --porcelain` is a space for a file changed only in the work tree."""
    p = _run(["-C", str(repo), *args], timeout=timeout)
    if check and p.returncode != 0:
        raise GitError(_one_line(f"git {' '.join(args)} in {repo}: {p.stderr.strip() or p.stdout.strip()}"))
    return p.stdout.rstrip("\n")


def is_repo(repo: Path) -> bool:
    if not repo.is_dir():
        return False
    try:
        p = _run(["-C", str(repo), "rev-parse", "--is-inside-work-tree"])
    except GitError:
        return False
    return p.returncode == 0 and p.stdout.strip() == "true"


def toplevel(repo: Path) -> Path:
    """The work tree's top-level folder, so `../api` and `/abs/api/src` name one repository.
    Falls back to the resolved path when it is not a repository."""
    if repo.is_dir():
        try:
            p = _run(["-C", str(repo), "rev-parse", "--show-toplevel"])
        except GitError:
            return repo.resolve()
        if p.returncode == 0 and p.stdout.strip():
            return Path(p.stdout.strip()).resolve()
    return repo.resolve()


def subfolder_of(repo: Path) -> Path | None:
    """The repository's top-level folder when `repo` is only a folder inside it, else None."""
    top = toplevel(repo)
    return top if top != repo.resolve() else None


def is_ignored(repo: Path, path: Path) -> bool:
    """True when the repository's ignore rules (.gitignore, .git/info/exclude, global) cover `path`."""
    target = str(path) + ("/" if path.is_dir() else "")
    try:
        return _run(["-C", str(repo), "check-ignore", "-q", target]).returncode == 0
    except GitError:
        return False


def head(repo: Path) -> str | None:
    out = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return out or None


def is_dirty(repo: Path) -> bool:
    """Changes in tracked or untracked files. Changes inside a submodule's own work tree do not count:
    the superproject cannot commit them (see `leftovers`)."""
    return bool(git(repo, "status", "--porcelain", "--ignore-submodules=dirty"))


def current_branch(repo: Path) -> str:
    return git(repo, "branch", "--show-current", check=False)


def valid_branch_name(name: str, repo: Path | None = None) -> str | None:
    """None if `name` can be a branch name, else the complaint of `check-ref-format --branch`."""
    if not name or name.startswith("-"):
        return "a branch name cannot be empty or start with '-'"
    cwd = str(repo) if repo is not None and repo.is_dir() else None
    try:
        r = _run(["check-ref-format", "--branch", name], cwd=cwd)
    except GitError as e:
        return str(e)
    if r.returncode == 0:
        return None
    return (r.stderr or r.stdout).strip() or f"'{name}' is not a valid branch name"


def ref_exists(repo: Path, ref: str) -> bool:
    """Any commit-ish: a branch, a remote branch, a tag, a hash, HEAD."""
    return bool(git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False))


def branch_exists(repo: Path, branch: str) -> bool:
    """A local branch of this name. A tag or a hash that happens to be spelled the same does not count:
    checking that out would leave a detached HEAD."""
    return bool(git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False))


def checkout(repo: Path, branch: str, base: str | None) -> str:
    """Put the repo on `branch`, creating it from `base` if needed. Returns a log line."""
    if current_branch(repo) == branch:
        return ""
    if is_dirty(repo):
        raise GitError(f"{repo.name} has uncommitted changes; cannot switch to {branch}")
    if branch_exists(repo, branch):
        git(repo, "checkout", "-q", branch)
        here = current_branch(repo)
        if here != branch:
            raise GitError(f"{repo.name}: checking out {branch} left {here or 'a detached HEAD'}")
        return f"checked out {repo.name}:{branch}"
    if not base:
        raise GitError(f"{repo.name}: branch {branch} does not exist and no base is set")
    if not ref_exists(repo, base):
        raise GitError(f"{repo.name}: base {base} does not exist")
    git(repo, "checkout", "-q", "-b", branch, base)
    return f"created {repo.name}:{branch} from {base}"


# Two tiers of "looks like a secret" file names (globs; one without a `/` is matched against the file name,
# one with a `/` against the whole path relative to the repository's top-level folder):
#   refuse: secrets beyond reasonable doubt. A WIP commit never takes them; untracked ones are added to
#           .git/info/exclude so the repository stays clean. A file of any name is also refused when it
#           starts like a private key (see private_key_files). Set with `[run] wip_secret_patterns`.
#   warn:   names that are often ordinary code or data (secrets.py, credentials.json, foo.key, .npmrc).
#           They are committed, so a queue never stops on a false alarm, but every one is reported loudly
#           (log, task note, SUMMARY, on_event). Set with `[run] wip_warn_patterns`.
DEFAULT_SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.p12", "*.pfx", "*.keystore", "id_rsa", "id_rsa.*",
                           "id_ed25519", "id_ed25519.*", "id_ecdsa", "id_ecdsa.*", "id_dsa", "id_dsa.*",
                           ".pypirc")
DEFAULT_WARN_PATTERNS = ("*.key", "credentials*", "secrets.*", ".npmrc", "*.token", "*.secret",
                         "service-account*.json", ".netrc", ".htpasswd")
SECRET_GLOBS = DEFAULT_SECRET_PATTERNS          # the old name
BIG_FILE_BYTES = 20 * 1024 * 1024
# examples such as `.env.example` are meant to be committed
SECRET_OK = (".env.example", ".env.sample", ".env.template", ".env.dist")
PRIVATE_KEY_SNIFF_BYTES = 100_000       # larger files are not read
UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _matches(path: str, patterns) -> bool:
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(low if "/" in g else name, g.lower()) for g in patterns)


def looks_secret(path: str, patterns=None) -> bool:
    """True for a name the refuse tier names (`.env.example` and its kin are always fine)."""
    if path.rsplit("/", 1)[-1].lower() in SECRET_OK:
        return False
    return _matches(path, DEFAULT_SECRET_PATTERNS if patterns is None else patterns)


def looks_suspect(path: str, patterns=None) -> bool:
    """True for a name the warn tier names: committed, but reported."""
    return _matches(path, DEFAULT_WARN_PATTERNS if patterns is None else patterns)


def _status_entries(repo: Path, submodules: bool = False) -> list[tuple[str, str]]:
    """(XY status, path) of every changed path, untracked ones included. Paths are relative to the
    repository's top-level folder, whichever folder `repo` names. A rename or copy is listed under its
    new name only: `status --porcelain -z` prints the old name as a separate entry that has no status of
    its own. Changes inside a submodule's own work tree are left out unless `submodules` is set."""
    args = ["status", "--porcelain", "-z", "--untracked-files=all"]
    if not submodules:
        args.append("--ignore-submodules=dirty")
    out = git(toplevel(repo), *args)
    entries = out.split("\0")
    found, i = [], 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) <= 3:
            continue
        if entry[0] in "RC" or entry[1] in "RC":
            i += 1                  # skip the origin path
        found.append((entry[:2], entry[3:]))
    return found


def changed_files(repo: Path) -> list[str]:
    """Paths (relative to the repository's top-level folder) with uncommitted changes, untracked ones
    included (ignored files are not listed)."""
    return [p for _, p in _status_entries(repo)]


def untracked_files(repo: Path) -> list[str]:
    return [p for xy, p in _status_entries(repo) if xy == "??"]


def unmerged_files(repo: Path) -> list[str]:
    return [p for xy, p in _status_entries(repo) if xy in UNMERGED]


def leftovers(repo: Path) -> list[str]:
    """Everything still uncommitted, submodule contents included: what a WIP commit could not take."""
    return [p for _, p in _status_entries(repo, submodules=True)]


def submodule_paths(repo: Path) -> list[str]:
    """Paths of the repository's submodules (entries of mode 160000 in the index)."""
    out = git(toplevel(repo), "ls-files", "--stage", "-z", check=False)
    return [e.split("\t", 1)[1] for e in out.split("\0") if e.startswith("160000 ") and "\t" in e]


def operation_in_progress(repo: Path) -> str | None:
    """What git is in the middle of in this work tree (a merge, a rebase, ...), or None."""
    gdir = git(toplevel(repo), "rev-parse", "--path-format=absolute", "--git-dir", check=False)
    if not gdir:
        return None
    base = Path(gdir)
    for name, what in (("MERGE_HEAD", "a merge"), ("CHERRY_PICK_HEAD", "a cherry-pick"),
                       ("REVERT_HEAD", "a revert"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase")):
        if (base / name).exists():
            return what
    return None


def private_key_files(repo: Path, paths: list[str]) -> list[str]:
    """Files whose first lines say they hold a private key, whatever they are called.
    `paths` are relative to the repository's top-level folder."""
    top = toplevel(repo)
    found = []
    for p in paths:
        f = top / p
        try:
            if not f.is_file() or f.stat().st_size > PRIVATE_KEY_SNIFF_BYTES:
                continue
            text = f.read_bytes()[:400].decode("utf-8", errors="ignore")
        except OSError:
            continue
        if "-----BEGIN" in text and "PRIVATE KEY" in text:
            found.append(p)
    return found


def secret_looking(repo: Path, patterns=None) -> list[str]:
    """Changed files a WIP commit must not take (refuse tier), paths relative to the top-level folder."""
    files = changed_files(repo)
    named = [p for p in files if looks_secret(p, patterns)]
    return named + private_key_files(repo, [p for p in files if p not in named])


def suspect_looking(repo: Path, secret_patterns=None, warn_patterns=None) -> list[str]:
    """Changed files that a WIP commit will take but that look like they may hold a secret (warn tier).
    A file the refuse tier keeps out is not listed here."""
    refused = set(secret_looking(repo, secret_patterns))
    return [p for p in changed_files(repo) if p not in refused and looks_suspect(p, warn_patterns)]


def _exclude_pattern(path: str) -> str:
    """The line of an exclude file that names exactly this path, relative to the repository root."""
    out = ""
    for ch in path:
        out += "\\" + ch if ch in "*?[]\\" else ch
    if out[:1] in ("#", "!") or out[:1] == " ":
        out = "\\" + out
    return "/" + out


def exclude_locally(repo: Path, paths: list[str]) -> list[str]:
    """Add untracked `paths` to this repository's own exclude file (never the shared .gitignore), so a
    file baton refuses to commit does not keep the work tree dirty and block the next task. Tracked files
    cannot be excluded this way. Returns the paths that were added."""
    if not paths:
        return []
    top = toplevel(repo)
    tracked = set(git(top, "ls-files", "-z", "--", *[f":(literal){p}" for p in paths], check=False).split("\0"))
    todo = [p for p in paths if p not in tracked]
    if not todo:
        return []
    exclude = Path(git(top, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        have = exclude.read_text(encoding="utf-8", errors="surrogateescape").splitlines() if exclude.exists() else []
        add = [_exclude_pattern(p) for p in todo if _exclude_pattern(p) not in have]
        if add:
            with exclude.open("a", encoding="utf-8", errors="surrogateescape") as fh:
                if have and have[-1] != "":
                    fh.write("\n")
                fh.write("# added by baton: looks like a secret, never committed\n" + "\n".join(add) + "\n")
    except OSError:
        return []
    return todo


def big_files(repo: Path, limit: int = BIG_FILE_BYTES) -> list[str]:
    top = toplevel(repo)
    big = []
    for p in changed_files(repo):
        try:
            if (top / p).is_file() and (top / p).stat().st_size > limit:
                big.append(p)
        except OSError:
            continue
    return big


def identity_missing(repo: Path) -> list[str]:
    """Which of user.name / user.email the repository cannot find (commits fail without them)."""
    return [k for k in ("user.name", "user.email") if not git(repo, "config", "--get", k, check=False)]


def _refuse_unsafe_state(top: Path, entries: list) -> None:
    """Raise UnsafeToCommit in the middle of a merge, rebase and the like, or with unmerged paths."""
    busy = operation_in_progress(top)
    unmerged = [p for xy, p in entries if xy in UNMERGED]
    if busy or unmerged:
        what = f"the repository is in the middle of {busy}" if busy else "there are unmerged paths"
        names = f": {', '.join(printable(p) for p in unmerged[:5])}" if unmerged else ""
        raise UnsafeToCommit(f"{what}{names}; baton does not commit it, a commit would close it with "
                             "conflict markers inside")


def _create_commit(top: Path, message: str, verify: bool) -> None:
    if not verify:
        git(top, "commit", "-q", "--no-verify", "-m", message, timeout=COMMIT_TIMEOUT_S)
        return
    p = _run(["-C", str(top), "commit", "-q", "-m", message], timeout=COMMIT_TIMEOUT_S)
    if p.returncode != 0:
        raise CommitRejected((p.stderr.strip() or p.stdout.strip() or f"exit {p.returncode}")[-1500:])


def commit_all(repo: Path, message: str, verify: bool = False, secret_patterns=None) -> str | None:
    """Commit everything in the work tree except files the refuse tier names (.env, private keys; see
    `secret_patterns`, default DEFAULT_SECRET_PATTERNS).
    It works on the whole repository, also when `repo` is only a folder inside it.
    Hooks are skipped by default: that is an explicitly unverified WIP commit. With
    verify=True the repository's hooks run, and a refusal raises CommitRejected with the hook
    output (the changes stay staged). Returns the new hash, or None if nothing to commit.
    Raises UnsafeToCommit in the middle of a merge or rebase, or with unmerged paths."""
    top = toplevel(repo)
    entries = _status_entries(top)
    if not entries:
        return None
    _refuse_unsafe_state(top, entries)
    skip = secret_looking(top, secret_patterns)
    git(top, "add", "-A", "--", ".", *[f":(exclude,literal){p}" for p in skip])
    if _run(["-C", str(top), "diff", "--cached", "--quiet"]).returncode == 0:
        if skip:
            exclude_locally(top, skip)
        return None         # nothing was staged: only secret-looking files, or changes git cannot stage
    _create_commit(top, message, verify)
    if skip:
        exclude_locally(top, skip)
    return head(top)


def count_commits(repo: Path, since: str | None) -> int:
    if since is None:
        out = git(repo, "rev-list", "--count", "HEAD", check=False)
    else:
        out = git(repo, "rev-list", "--count", f"{since}..HEAD", check=False)
    try:
        return int(out)
    except ValueError:
        return 0


def changed_paths(repo: Path, since: str) -> list[str]:
    """Paths changed between `since` and HEAD, both sides of a rename included (for display and
    matching: names that are not UTF-8 come back made printable)."""
    out = git(repo, "diff", "--name-only", "-z", "--no-renames", f"{since}..HEAD")
    return [printable(p) for p in out.split("\0") if p]


# --- branch comparison helpers (used by baton/branches.py) ---

def has_branch(repo: Path, name: str) -> bool:
    """True when `name` is a local branch (a tag or a hash of the same name does not count)."""
    return branch_exists(repo, name)


def has_remote_branch(repo: Path, name: str, remote: str = "origin") -> bool:
    """True when `<remote>/<name>` is already known locally. Never fetches."""
    return bool(git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{name}", check=False))


def is_bare(repo: Path) -> bool:
    return git(repo, "rev-parse", "--is-bare-repository", check=False) == "true"


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """True when every commit of `ancestor` is already in `descendant`."""
    return _run(["-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0


def share_history(repo: Path, a: str, b: str) -> bool:
    """False for two lines of history that have no commit in common (an orphan branch)."""
    return _run(["-C", str(repo), "merge-base", a, b]).returncode == 0


def ahead_behind(repo: Path, branch: str, base: str) -> tuple[int, int] | None:
    """(commits on `branch` that `base` lacks, commits on `base` that `branch` lacks); None when unknown."""
    out = git(repo, "rev-list", "--left-right", "--count", f"{branch}...{base}", check=False).split()
    try:
        return int(out[0]), int(out[1])
    except (IndexError, ValueError):
        return None


def short_status(repo: Path, limit: int = 5) -> list[str]:
    """The first `limit` lines of `git status --short`, untracked files included, with a '…' line if cut."""
    out = _run(["-C", str(repo), "status", "--short", "--untracked-files=all"]).stdout
    lines = [line for line in out.splitlines() if line.strip()]
    return lines[:limit] + ([f"… and {len(lines) - limit} more"] if len(lines) > limit else [])


def rebase_onto(repo: Path, branch: str, onto: str) -> None:
    """Rebase `branch` (checked out here) onto `onto`. A conflict aborts the rebase and raises GitError,
    leaving the branch as it was. Only used when `[run] existing_branch = "rebase"`."""
    p = _run(["-C", str(repo), "rebase", onto])
    if p.returncode != 0:
        _run(["-C", str(repo), "rebase", "--abort"])
        raise GitError(f"rebase of {branch} onto {onto} in {repo.name} failed and was aborted: "
                       f"{(p.stderr.strip() or p.stdout.strip())[-400:]}")
