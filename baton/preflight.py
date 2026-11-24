"""Checks before anything runs, and the dry-run plan."""

from __future__ import annotations

import os
import shutil
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, branches, gitutil, timefmt, versioninfo
from .clock import local_zone
from .config import PROVIDER_HOME_ENV, Config
from .providers import build_command, provider_env, resolve_bin
from .state import FINISHED, expire_limits, load_state, mid_attempt, starting_provider
from . import codexmodels, notverified, sandbox, services, uipaths
from .autocheck import schedule_preflight
from . import sandboxprobe
from .pathcheck import path_warnings
from .requiredenv import required_env_errors
from .tasks import Task, TaskError, ancestors, load_tasks, order_tasks
from .watch import run_active


def _when(iso_text: str, tz, now: datetime) -> str:
    """A saved ISO time as 'YYYY-MM-DD HH:MM +HH:MM (in 2h10m)', or 'available now' once it has passed."""
    try:
        dt = datetime.fromisoformat(iso_text)
    except (TypeError, ValueError):
        return str(iso_text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return timefmt.until(dt, tz, now)


def timezone_warning(cfg: Config, now: datetime | None = None) -> str | None:
    """Reset times without a zone (Codex) are read in run.timezone. If that is
    not this machine's zone the printed times are misread by hours."""
    if cfg.timezone is None:
        return None
    now = now or datetime.now(timezone.utc)
    configured = now.astimezone(cfg.timezone).utcoffset()
    local = now.astimezone().utcoffset()
    if configured == local:
        return None
    return (f"run.timezone = {cfg.timezone.key} is UTC{_fmt_offset(configured)} now, but this machine is at "
            f"UTC{_fmt_offset(local)}. Reset times that a CLI prints without a zone are read in run.timezone, "
            "so a wrong zone makes baton wait too long or retry too early; remove run.timezone to use this "
            "machine's zone")


def _fmt_offset(delta) -> str:
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _writable(d: Path) -> str | None:
    """Without creating anything: can d be written (or created)? None when yes."""
    probe = d
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    if not probe.is_dir():
        return f"{probe} is not a folder"
    if not os.access(probe, os.W_OK | os.X_OK):
        return f"{probe} is not writable"
    return None


UNSAFE_ARGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
               "--dangerously-bypass-approvals-and-sandbox", "--yolo", "danger-full-access", "--full-auto")
UNSAFE_MODES = {"bypasspermissions", "dontask"}


def unsafe_mode_warnings(cfg: Config, enabled: list[str]) -> list[str]:
    """A permission mode or flag that switches the agent's guardrails off: baton accepts it, but says so."""
    out = []
    for name in enabled:
        pc = cfg.providers[name]
        found = []
        if (pc.permission_mode or "").replace("_", "").replace("-", "").lower() in UNSAFE_MODES:
            found.append(f"permission_mode = {pc.permission_mode!r}")
        for a in pc.extra_args:
            if any(u in a.lower() for u in UNSAFE_ARGS):
                found.append(f"extra_args {a!r}")
        if (pc.sandbox or "").lower() == "danger-full-access":
            found.append('sandbox = "danger-full-access"')
        if found:
            out.append(f"{name}: {', '.join(found)} switches the agent's permission checks or sandbox off; "
                       "the deny rules in the settings file no longer limit what a task can do. "
                       "Run it in a container or as a separate user, never in a repository you cannot lose")
    return out


HEAVY_EFFORTS = {"xhigh", "max"}
HEAVY_MODEL_PARTS = ("astra",)


def heavy_model_warnings(cfg: Config, enabled: list[str]) -> list[str]:
    """Rungs that use the most expensive settings (an `xhigh`/`max` effort, an `astra`-class model) burn the
    quota quickly for routine work; baton accepts them, but says so once per provider."""
    out = []
    for name in enabled:
        heavy = []
        for rung in cfg.routing.rungs:
            spec = cfg.spec(rung, name)
            if spec is None:
                continue
            if spec.effort.lower() in HEAVY_EFFORTS or any(p in spec.model.lower() for p in HEAVY_MODEL_PARTS):
                heavy.append(f"{rung} ({spec.model} {spec.effort or 'default effort'})".strip())
        if heavy:
            out.append(f"{name}: heavy settings in {', '.join(heavy)}; they use the quota fast and rarely pay off "
                       "outside verifiers and the hardest work. Prefer high effort on the workhorse rung")
    return out


def _lanes_can_overlap(cfg: Config, tasks: list[Task], enabled: list[str]) -> bool | None:
    """Can two agent tasks in different lanes run at the same moment? A task goes to the first provider,
    in start order, that it may use, and waits when that provider's slots are taken. None: cannot tell."""
    order = [cfg.start_provider] + [p for p in enabled if p != cfg.start_provider]
    prefer: list[tuple[str, str]] = []
    for t in tasks:
        if not t.runs_agent:
            continue
        allowed = [p for p in order if p in enabled and (not t.providers or p in t.providers)]
        if not allowed:
            return None
        prefer.append((t.lane_key, allowed[0]))
    for i, (lane_a, prov_a) in enumerate(prefer):
        for lane_b, prov_b in prefer[i + 1:]:
            if lane_a == lane_b:
                continue
            if prov_a != prov_b or cfg.providers[prov_a].max_concurrent >= 2:
                return True
    return False


def dirty_message(cfg: Config, repo: Path) -> str:
    """The refusal for a repository with uncommitted changes, naming the first few paths. When they are
    baton's own folders (task-auto inside the repository) it says so, since that is the usual cause."""
    lines = gitutil.short_status(repo)
    msg = f"{repo.name} has uncommitted changes; commit them first"
    if lines:
        msg += ":\n      " + "\n      ".join(lines)
    top = gitutil.toplevel(repo)
    if is_within(cfg.base_dir, top):
        rel = cfg.base_dir.resolve().relative_to(top).as_posix()
        own = "" if rel == "." else rel + "/"
        if any(line[3:].strip('"').startswith(own) for line in lines if len(line) > 3 and not line.startswith("…")):
            msg += (f"\n    these paths are baton's own folders (results, state, logs) inside {top.name}. Keep the "
                    f"task-auto folder out of the repositories it works on (README: Branches), or have git "
                    f"ignore them: add `{own or './'}results/`, `{own or './'}state/` and `{own or './'}logs/` "
                    "to .gitignore or .git/info/exclude")
    return msg


def missing_base_hint(repo: Path, base: str) -> str:
    """What to do about a base branch that does not exist. baton never fetches."""
    if gitutil.has_remote_branch(repo, base):
        return (f"; origin/{base} is known here, so create the local branch yourself: "
                f"`git -C {repo} branch {base} origin/{base}` (baton never fetches or creates the base)")
    return (f"; baton never fetches. Fetch and create it yourself (`git -C {repo} fetch`, then "
            f"`git -C {repo} branch {base} origin/{base}`), or set `base`/[run] default_base to a branch that exists")


PROTECTED_BRANCHES = ("main", "master")


def protected_branch_warnings(cfg: Config, t: Task, resuming: bool) -> list[str]:
    """A task that works directly on main, on the default base, or on the branch the user has checked
    out commits straight onto a line they may be using."""
    if not t.branch or not t.runs_agent or not gitutil.is_repo(t.repo):
        return []
    out = []
    if t.creates_base and (t.branch in PROTECTED_BRANCHES or t.branch == cfg.default_base):
        return []                   # the owner says this task is meant to create or commit to that line
    if t.branch in PROTECTED_BRANCHES or t.branch == cfg.default_base:
        out.append(f"task {t.id}: its branch '{t.branch}' is a main line ({'the default base' if t.branch == cfg.default_base else 'main/master'}): "
                   "the task commits straight onto it. Use a branch of its own (leave `branch` out for baton/<id>)")
    elif not resuming and gitutil.current_branch(t.repo) == t.branch:
        out.append(f"task {t.id}: '{t.branch}' is the branch checked out in {t.repo.name} right now, so the task "
                   "commits onto your own working branch")
    return out

def free_space_warning(cfg: Config) -> str | None:
    """A full disk stops the whole run (state and logs cannot be written); say so before it does."""
    if cfg.min_free_mb <= 0:
        return None
    probe = cfg.results_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free_mb = shutil.disk_usage(probe).free / 1_000_000
    except OSError:
        return None
    if free_mb < cfg.min_free_mb:
        return (f"only {free_mb:.0f} MB free where {cfg.results_dir} lives (run.min_free_mb = "
                f"{cfg.min_free_mb:g}): agent output can fill the disk, and a full disk stops the run")
    return None


@dataclass
class _Ctx:
    """What the checks of `preflight` share: the config, the providers in use and the messages so far."""
    cfg: Config
    provider: str | None
    create_dirs: bool
    enabled: list[str]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)

    def add_new_warnings(self, items) -> None:
        self.warnings.extend(w for w in items if w not in self.warnings)

    def add_new_errors(self, items) -> None:
        self.errors.extend(e for e in items if e not in self.errors)


# --- checks that need no task files ------------------------------------------------------------

def _check_environment(ctx: _Ctx) -> None:
    cfg = ctx.cfg
    tz_warning = timezone_warning(cfg)
    if tz_warning:
        ctx.warnings.append(tz_warning)
    space_warning = free_space_warning(cfg)
    if space_warning:
        ctx.warnings.append(space_warning)
    if cfg.min_version and versioninfo.older_than(__version__, cfg.min_version):
        ctx.warnings.append(f"run.min_version = {cfg.min_version}, but this baton is {versioninfo.describe()}: "
                            "pull and reinstall before the run (an old baton can mis-record results that a "
                            "newer one handles)")


def _check_provider_flags(ctx: _Ctx) -> None:
    cfg, enabled = ctx.cfg, ctx.enabled
    if not enabled:
        ctx.errors.append("every provider is disabled")
    if cfg.start_provider not in enabled:
        ctx.errors.append(f"start_provider '{cfg.start_provider}' is disabled")
    if ctx.provider and ctx.provider not in enabled:
        ctx.errors.append(f"--provider {ctx.provider}: that provider is not enabled")


def _check_saved_state(ctx: _Ctx) -> None:
    cfg = ctx.cfg
    state0 = load_state(cfg.state_dir / "run.json")
    start = ctx.provider or cfg.start_provider
    if state0.current and state0.current != start and start in ctx.enabled:
        began = starting_provider(state0, cfg.start_provider, ctx.provider, enabled=ctx.enabled)
        if began != start:      # a task really is mid-attempt on another provider: say what happens
            ctx.warnings.append(f"the saved state says current = {state0.current} with task(s) "
                                f"{', '.join(mid_attempt(state0))} mid-attempt: they continue on {state0.current}, "
                                f"and new tasks begin on {start}; pass --provider {start} to move them too")


def _check_add_dirs(ctx: _Ctx, name: str, pc) -> None:
    own = ctx.cfg.results_dir.resolve()
    for d in pc.add_dirs:
        if not os.path.isdir(d) and Path(d).resolve() == own:
            if ctx.create_dirs:         # baton's own output folder: a workspace not made by `baton init` lacks it
                own.mkdir(parents=True, exist_ok=True)
        elif not os.path.isdir(d):
            ctx.errors.append(f"{name}: add_dirs entry is not a folder: {d}")
        elif is_within(Path(d), ctx.cfg.verifiers_dir):
            ctx.errors.append(f"{name}: add_dirs entry {d} exposes the verifiers folder")


def _check_claude_flags(ctx: _Ctx, pc, path: str) -> None:
    try:
        help_text = subprocess.run([path, "--help"], capture_output=True, text=True, timeout=30,
                                   env={**os.environ, **provider_env(pc)}).stdout
    except (OSError, subprocess.TimeoutExpired):
        help_text = ""
    if not help_text:
        return
    if "--output-format" not in help_text:
        ctx.errors.append(f"{pc.name}: this version has no --output-format flag; update Claude Code")
    uses_effort = any(s.effort for r in ctx.cfg.routing.rungs if (s := ctx.cfg.spec(r, pc.name)))
    if uses_effort and pc.effort_flag and "--effort" not in help_text:
        ctx.warnings.append(f"{pc.name}: no --effort flag in this version; set providers.{pc.name}."
                            "effort_flag = false (CLAUDE_CODE_EFFORT_LEVEL is still set)")


def _check_provider(ctx: _Ctx, name: str) -> None:
    pc = ctx.cfg.providers[name]
    path = resolve_bin(pc)
    if not path:
        ctx.errors.append(f"{name}: '{pc.bin}' not found or not executable "
                          f"(install it, fix providers.{name}.bin, or set enabled = false)")
        return
    if pc.settings and not os.path.isfile(pc.settings):
        ctx.errors.append(f"{name}: settings file not found: {pc.settings}")
    _check_add_dirs(ctx, name, pc)
    if pc.kind == "claude":
        _check_claude_flags(ctx, pc, path)


def _check_providers(ctx: _Ctx) -> None:
    for name in ctx.enabled:
        _check_provider(ctx, name)


# --- checks that need the task files -----------------------------------------------------------

def _check_codex_sandbox(ctx: _Ctx) -> None:
    for name in ctx.enabled:
        pc = ctx.cfg.providers[name]
        if pc.kind == "codex":
            _check_account_sandbox(ctx, pc)


def _check_account_sandbox(ctx: _Ctx, pc) -> None:
    if not resolve_bin(pc):
        return
    ok, detail = sandbox.probe(pc)
    if ok is False:
        label = "Codex" if pc.name == "codex" else pc.name
        ctx.warnings.append(f"{pc.name}: its sandbox cannot start ({detail}). {label} will be left out of this "
                            f"run. {sandbox.HINT}")
    elif ok is None and "turned off" not in detail:
        ctx.warnings.append(f"{pc.name}: sandbox not checked: {detail}")
    elif ok:
        ctx.warnings.extend(sandboxprobe.check(pc))


def _check_services(ctx: _Ctx) -> None:
    svc_errors, svc_warnings = services.preflight_findings(ctx.cfg, ctx.tasks, ctx.enabled)
    ctx.errors.extend(svc_errors)
    ctx.warnings.extend(svc_warnings)


def _check_codex_models(ctx: _Ctx) -> None:
    for name in ctx.enabled:
        if ctx.cfg.provider_kind(name) != "codex":
            continue
        used = {t.resolved_rung for t in ctx.tasks
                if t.runs_agent and (not t.providers or name in t.providers)}
        model_errors, model_warnings = codexmodels.check(ctx.cfg, used, name)
        ctx.errors.extend(model_errors)
        ctx.warnings.extend(model_warnings)


def _check_repo_suites(ctx: _Ctx) -> None:
    cfg = ctx.cfg
    for t in ctx.tasks:
        if t.template != "repo-suite":
            continue
        rc = cfg.repos.get(t.repo)
        baseline = (cfg.base_dir / t.params["baseline"]) if t.params.get("baseline") else rc and rc.suite_baseline
        if not (t.params.get("cmd") or (rc and rc.suite_cmd)):
            ctx.errors.append(f"{t.id}: no suite_cmd for {t.repo.name}; set [repos.\"...\"] suite_cmd or params.cmd")
        if baseline and not baseline.is_file():
            ctx.errors.append(f"{t.id}: baseline file {baseline} not found (an empty file means no known failures)")


def _check_money_gates(ctx: _Ctx) -> None:
    gates: dict[str, list[str]] = {}
    for t in ctx.tasks:
        if t.money_gate:
            gates.setdefault(t.feature or "(no feature)", []).append(t.id)
    for feature, ids in gates.items():
        if len(ids) > 1:
            ctx.warnings.append(f"feature {feature} has {len(ids)} money gates ({', '.join(ids)}); each one holds "
                                "back its dependents. Keep one and give it `when = \"feature-end\"` so it runs "
                                "once after the feature's tasks")


def _check_ui_paths(ctx: _Ctx) -> None:
    ctx.warnings.extend(uipaths.warnings_for(ctx.tasks, ctx.cfg.ui_paths, notverified.deferred_reason(ctx.cfg)))


def _check_has_tasks(ctx: _Ctx) -> None:
    if not ctx.tasks:
        ctx.warnings.append(f"no task files in {ctx.cfg.tasks_dir}")


def _check_task_providers(ctx: _Ctx) -> None:
    cfg = ctx.cfg
    for t in ctx.tasks:
        if not t.runs_agent:
            continue
        if t.providers and not set(t.providers) & set(ctx.enabled):
            where = "run.verifier_providers" if t.is_verifier and t.providers == cfg.match_providers(cfg.verifier_providers) \
                else "its providers"
            ctx.errors.append(f"task {t.id}: none of {where} ({', '.join(t.providers)}) is enabled")
        if t.is_verifier and any(cfg.provider_kind(p) == "codex" for p in (t.providers or [])):
            _check_codex_verifier(ctx, t)


def _check_codex_verifier(ctx: _Ctx, t: Task) -> None:
    """Codex cannot hide files from the agent, so a verifier on it may see implementer output. That is an
    error unless the owner opted in; a verifier that has already finished is not checked again."""
    cfg = ctx.cfg
    if load_state(cfg.state_dir / "run.json").task(t.id).status in FINISHED:
        return
    where = "run.verifier_providers" if t.providers == cfg.match_providers(cfg.verifier_providers) else "its providers"
    text = (f"verifier {t.id}: {where} includes codex, which cannot hide files from the agent "
            "(`codex exec` has no read restriction), so the verifier would see implementer output")
    if cfg.allow_codex_verifiers:
        ctx.warnings.append(text + "; allowed by run.allow_codex_verifiers. The test-path check still applies")
    else:
        ctx.errors.append(text + "; use claude, or set run.allow_codex_verifiers = true to accept that")


# --- the per-task checks ---------------------------------------------------------------------

@dataclass
class _Seen:
    """Repositories already looked at, so each is reported once however many tasks use it."""
    repos: set = field(default_factory=set)
    identity: set = field(default_factory=set)
    layout: set = field(default_factory=set)


def _check_rungs(ctx: _Ctx, t: Task) -> None:
    for p in ctx.enabled:
        if t.providers and p not in t.providers:
            continue
        if ctx.cfg.spec(t.resolved_rung, p) is None:
            ctx.errors.append(f"task {t.id}: rung '{t.resolved_rung}' has no model for {p}")


def _check_repo_is_git(ctx: _Ctx, t: Task) -> bool:
    """False (with the error recorded) when the task's repository is not a working copy."""
    if gitutil.is_repo(t.repo):
        return True
    if t.repo.is_dir() and gitutil.is_bare(t.repo):
        ctx.errors.append(f"task {t.id}: {t.repo} is a bare repository; baton needs a working copy "
                          "(a clone with files checked out), because the agent edits files there")
    else:
        ctx.errors.append(f"task {t.id}: {t.repo} is not a git repository "
                          "(the repo path in the task file is relative to baton.toml's folder)")
    return False


def _check_dirty(ctx: _Ctx, t: Task, running: set[str], seen: _Seen) -> None:
    if t.repo in seen.repos:
        return
    seen.repos.add(t.repo)
    # a task interrupted mid-run may leave changes; the runner commits them as WIP
    running_here = next((x for x in ctx.tasks if x.id in running and x.repo == t.repo), None)
    if running_here and running_here.branch and gitutil.current_branch(t.repo) != running_here.branch:
        problem = gitutil.resume_checkout_problem(t.repo, running_here.branch)
        if problem:
            ctx.errors.append(problem)
        return
    if gitutil.is_dirty(t.repo) and not running_here:
        ctx.errors.append(dirty_message(ctx.cfg, t.repo))


def _check_identity(ctx: _Ctx, t: Task, seen: _Seen) -> None:
    if t.repo in seen.identity:
        return
    seen.identity.add(t.repo)
    missing = gitutil.identity_missing(t.repo)
    if missing:
        ctx.errors.append(f"{t.repo.name}: git has no {' or '.join(missing)} here, so baton's WIP commits and "
                          "the agent's commits would fail; set it with `git config user.name ...` / "
                          "`git config user.email ...`")


def _note_repo_layout(ctx: _Ctx, t: Task, seen: _Seen) -> None:
    if t.repo in seen.layout:
        return
    seen.layout.add(t.repo)
    top = gitutil.subfolder_of(t.repo)
    if top is not None:
        ctx.warnings.append(f"{t.repo} is a folder inside the repository {top}: baton works on all of {top.name} "
                            "(its dirty check, WIP commits and branches cover the whole repository)")
    if gitutil.submodule_paths(t.repo):
        ctx.warnings.append(f"{t.repo.name} has submodules: changes an agent makes inside a submodule's own "
                            "work tree cannot be committed by baton, and the task ends partial if it leaves any")


def _check_branch_name(ctx: _Ctx, t: Task) -> bool:
    """False (with the error recorded) when the task's branch name is not usable."""
    bad_branch = gitutil.valid_branch_name(t.branch, t.repo) if t.branch else None
    if not bad_branch:
        return True
    hint = " (the task id becomes part of the branch name baton/<id>; rename the task file)" \
        if t.branch_auto else ""
    ctx.errors.append(f"task {t.id}: '{t.branch}' is not a valid branch name{hint}: {bad_branch}")
    return False


def _check_missing_branch(ctx: _Ctx, t: Task, by_id: dict[str, Task], made_by: dict) -> None:
    if t.branch_auto and not t.base:
        return                                    # baton/<id> starts from the current checkout
    if not t.base:
        ctx.errors.append(f"task {t.id}: branch {t.branch} does not exist and no base is set")
    elif (not gitutil.ref_exists(t.repo, t.base)
          and not made_by.get((t.repo, t.base), set()) & {a.id for a in ancestors(t, by_id)}):
        ctx.errors.append(f"task {t.id}: base {t.base} does not exist in {t.repo.name}"
                          + missing_base_hint(t.repo, t.base))


def _check_existing_branch(ctx: _Ctx, t: Task, by_id: dict[str, Task]) -> None:
    # the branch is there already (an earlier night, an earlier run of this task, or by hand)
    rep = branches.assess(t.repo, t.branch, t.base or ("HEAD" if t.branch_auto else None),
                          branches.dependency_branches(t, by_id))
    if rep and not rep.ok:
        ctx.warnings.append(f"task {t.id}: " + branches.plan_note(rep, ctx.cfg.existing_branch))


def _check_branch(ctx: _Ctx, t: Task, by_id: dict[str, Task], made_by: dict) -> None:
    if not t.branch:
        return
    if not gitutil.branch_exists(t.repo, t.branch):
        _check_missing_branch(ctx, t, by_id, made_by)
    else:
        _check_existing_branch(ctx, t, by_id)


def _branches_made_by_tasks(tasks: list[Task]) -> dict[tuple, set[str]]:
    """Branches the run itself creates: a task may start from one made by a task it depends on."""
    made_by: dict[tuple, set[str]] = {}
    for t in tasks:
        if t.branch and not t.is_verifier:
            made_by.setdefault((t.repo, t.branch), set()).add(t.id)
    return made_by


def _check_tasks(ctx: _Ctx) -> None:
    cfg, tasks = ctx.cfg, ctx.tasks
    state = load_state(cfg.state_dir / "run.json")
    running = {tid for tid, s in state.tasks.items() if s.status == "running"}
    by_id = {t.id: t for t in tasks}
    made_by = _branches_made_by_tasks(tasks)
    seen = _Seen()
    for t in tasks:
        if not t.runs_agent:
            continue
        _check_rungs(ctx, t)
        if not _check_repo_is_git(ctx, t):
            continue
        ts = state.tasks.get(t.id)
        if ts and ts.status in FINISHED:
            continue
        _check_dirty(ctx, t, running, seen)
        _check_identity(ctx, t, seen)
        _note_repo_layout(ctx, t, seen)
        ctx.add_new_warnings(t.warnings)
        if not _check_branch_name(ctx, t):
            continue
        _check_branch(ctx, t, by_id, made_by)
        ctx.add_new_warnings(protected_branch_warnings(cfg, t, t.id in running))


# --- checks on the whole run -----------------------------------------------------------------

def _check_unsafe_and_heavy(ctx: _Ctx) -> None:
    ctx.warnings.extend(unsafe_mode_warnings(ctx.cfg, ctx.enabled))
    ctx.warnings.extend(heavy_model_warnings(ctx.cfg, ctx.enabled))


def _check_lanes(ctx: _Ctx) -> None:
    cfg, tasks, enabled = ctx.cfg, ctx.tasks, ctx.enabled
    if cfg.lanes <= 1:
        return
    agent_lanes = {t.lane_key for t in tasks if t.runs_agent}
    overlap = _lanes_can_overlap(cfg, tasks, enabled)
    if len(agent_lanes) >= 2 and overlap is False:
        ctx.warnings.append(f"lanes = {cfg.lanes}, but nothing can run in parallel: every task goes to one "
                            "provider and that provider has max_concurrent = 1 (a busy slot never sends a task "
                            "to the other provider, only a limit does). Give some tasks `providers = [\"codex\"]`, "
                            "or raise providers.<name>.max_concurrent if your plan's limits allow two sessions")
    if len(agent_lanes) < 2:
        ctx.warnings.append(f"lanes = {cfg.lanes}, but the agent tasks share {len(agent_lanes)} lane: "
                            "tasks in one repository never run at the same time, so nothing runs in parallel")
    verifier_tasks = [t for t in tasks if t.is_verifier and t.runs_agent]
    if verifier_tasks and any(cfg.provider_kind(p) == "codex" for p in enabled):
        ctx.warnings.append("lanes: a Codex task in another lane can read a verifier's uncommitted test files "
                            "in its repository while the verifier runs; keep verifier_providers = [\"claude\"] "
                            "and set providers = [\"claude\"] on tasks you do not want to overlap with verifiers")


def _check_watch_repos(ctx: _Ctx) -> None:
    for r in ctx.cfg.watch_repos:
        if not gitutil.is_repo(r):
            ctx.warnings.append(f"watch_repos: {r} is not a git repository")


def _dir_problem(d: Path, create: bool) -> str | None:
    """Why baton could not write into d (None: it can). With create, d is made and a file is written."""
    if not create:
        return _writable(d)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".baton-write-test"
        probe.write_text("x")
        probe.unlink()
    except OSError as e:
        return str(e)
    return None


def _check_output_dirs(ctx: _Ctx) -> None:
    cfg = ctx.cfg
    for d in (cfg.results_dir, cfg.state_dir, cfg.logs_dir):
        problem = _dir_problem(d, ctx.create_dirs)
        if problem:
            ctx.errors.append(f"cannot write to {d}: {problem}")


def _check_baton_dirs_in_repos(ctx: _Ctx) -> None:
    """baton's own folders inside a task repository would show up as uncommitted changes."""
    cfg = ctx.cfg
    for repo in sorted({t.repo for t in ctx.tasks if t.runs_agent and gitutil.is_repo(t.repo)}):
        top = gitutil.toplevel(repo)
        for d in (cfg.results_dir, cfg.state_dir, cfg.logs_dir):
            if d.is_dir() and is_within(d, top) and not gitutil.is_ignored(top, d):
                ctx.errors.append(f"{d} is inside {top.name} and git does not ignore it: results, state and logs "
                                  f"would count as uncommitted changes and block the next run. Add "
                                  f"`{d.resolve().relative_to(top)}/` to .gitignore "
                                  f"or .git/info/exclude, or move the task-auto folder out of the repository")


def _check_paths_and_env(ctx: _Ctx) -> None:
    ctx.add_new_warnings(path_warnings(ctx.cfg, ctx.tasks))
    ctx.add_new_errors(required_env_errors(ctx.cfg, ctx.tasks))


# The checks in the order their messages appear. Each takes the shared context and appends to it.
_BEFORE_TASKS = (
    ("environment", _check_environment),
    ("provider flags", _check_provider_flags),
    ("saved state", _check_saved_state),
    ("providers", _check_providers),
)
_AFTER_TASKS = (
    ("codex sandbox", _check_codex_sandbox),
    ("services", _check_services),
    ("codex models", _check_codex_models),
    ("repo suites", _check_repo_suites),
    ("money gates", _check_money_gates),
    ("ui paths", _check_ui_paths),
    ("task files present", _check_has_tasks),
    ("task providers", _check_task_providers),
    ("tasks", _check_tasks),
    ("unsafe and heavy settings", _check_unsafe_and_heavy),
    ("lanes", _check_lanes),
    ("watched repositories", _check_watch_repos),
    ("output folders", _check_output_dirs),
    ("baton folders in repositories", _check_baton_dirs_in_repos),
    ("paths and environment", _check_paths_and_env),
)


def preflight(cfg: Config, provider: str | None = None, *,
              create_dirs: bool = True) -> tuple[list[str], list[str], list[Task]]:
    """provider: the `baton run --provider` override of start_provider.
    create_dirs=False (baton plan) only checks that results/state/logs could be
    written, and creates nothing."""
    ctx = _Ctx(cfg, provider, create_dirs, cfg.enabled_providers())
    for _name, check in _BEFORE_TASKS:
        check(ctx)
    try:
        ctx.tasks = order_tasks(schedule_preflight(cfg, load_tasks(cfg)))
    except TaskError as e:
        ctx.errors.extend(str(e).splitlines())
        return ctx.errors, ctx.warnings, []
    for _name, check in _AFTER_TASKS:
        check(ctx)
    return ctx.errors, ctx.warnings, ctx.tasks


# --- the plan --------------------------------------------------------------------------------

def _limit_lines(state, tz, now: datetime) -> list[str]:
    out = []
    for p, d in state.providers.items():
        if d.get("limited_until"):
            w = _when(d["limited_until"], tz, now)
            out.append(f"  {p} available now (its limit has passed)" if w == "available now"
                       else f"  {p} limited until {w}")
        for m, u in (d.get("model_limits") or {}).items():
            w = _when(u, tz, now)
            if w != "available now":
                out.append(f"  {p}: {m} limited until {w} (other models of {p} are available)")
    return out


def _plan_header(cfg: Config, state, enabled: list[str], start: str, provider: str | None,
                 tz, now: datetime) -> list[str]:
    lines = [f"providers: {' > '.join([start] + [p for p in enabled if p != start])}"
             f" (begins on: {starting_provider(state, cfg.start_provider, provider, enabled=enabled)})"]
    lines += _limit_lines(state, tz, now)
    if cfg.verifiers_last:
        lines.append("verifiers run last (verifiers_last): a verifier nobody depends on runs after every other task; "
                     "a task that lists a verifier in depends_on still waits for it")
    if cfg.lanes > 1:
        lines.append(f"lanes: up to {cfg.lanes} tasks at once, one per repository (or `lane`); slots "
                     + ", ".join(f"{p} x{cfg.providers[p].max_concurrent}" for p in enabled) + " (experimental)")
    lines.append("")
    return lines


def _task_label(t: Task) -> str:
    # a verifier's goal and acceptance text are never printed
    return ("  [approve]" if t.is_approval else
            f"  [verify: {t.template}{', auto' if t.auto else ''}]" if t.template else
            f"  [verify, at the end of {t.feature}]" if t.when else
            "  [verify]" if t.is_verifier else "")


def _task_models(cfg: Config, t: Task, enabled: list[str]) -> str:
    provs = [p for p in enabled if not t.providers or p in t.providers]
    return ", ".join(
        f"{p}: {s.model}{' ' + s.effort if s.effort else ''}"
        for p in provs if (s := cfg.spec(t.resolved_rung, p)))


def _branch_exists_note(t: Task, by_id: dict[str, Task]) -> str:
    if not (t.branch and gitutil.is_repo(t.repo)):
        return ""
    rep = branches.assess(t.repo, t.branch, t.base or ("HEAD" if t.branch_auto else None),
                          branches.dependency_branches(t, by_id))
    if not rep:
        return ""
    return (f"  [branch exists: {rep.describe()}"
            f"{'; MISSING ' + ', '.join(d for d, _ in rep.missing) if rep.missing else ''}]")


def _task_where(cfg: Config, t: Task, enabled: list[str], by_id: dict[str, Task]) -> str:
    if not t.runs_agent:
        return "  human checkpoint" if t.is_approval else "  runs in baton, no agent"
    origin = t.base or (f"the current checkout ({gitutil.current_branch(t.repo) or 'detached HEAD'})"
                        if t.branch_auto and gitutil.is_repo(t.repo) else "?")
    where = (f"  repo {t.repo.name}" + (f" ({t.branch}{' [automatic]' if t.branch_auto else ''} from {origin})"
                                        if t.branch else "")
             + f"  rung {t.resolved_rung}: {_task_models(cfg, t, enabled)}")
    if cfg.lanes > 1:
        where += f"  lane {t.lane_name}"
    where += _branch_exists_note(t, by_id)
    if t.return_mid_task is False:
        where += "  return: off"
    return where


def _task_line(cfg: Config, t: Task, i: int, state, enabled: list[str], active: bool,
               by_id: dict[str, Task]) -> str:
    ts = state.tasks.get(t.id)
    st = ts.status if ts else "pending"
    if st == "running" and not active:
        st = "interrupted"          # the run that was on it is gone; the next run continues it
    return (f"{i:>2}. {t.id}{_task_label(t)}  [{st}]{_task_where(cfg, t, enabled, by_id)}"
            + (f"  after {', '.join(t.depends_on)}" if t.depends_on else "")
            + (f"  priority {t.priority}" if t.priority else ""))


def _restore_lines(tasks: list[Task]) -> list[str]:
    repos = {t.repo: gitutil.current_branch(t.repo) for t in tasks if t.runs_agent and t.branch
             and gitutil.is_repo(t.repo)}
    if not repos:
        return []
    return ["", "when the run ends each repository goes back to the branch it is on now: "
            + ", ".join(f"{r.name} -> {b or 'the detached HEAD at ' + (gitutil.head(r) or '?')[:12] + ' (checked out again by commit)'}"
                        for r, b in repos.items())]


def _first_command_lines(cfg: Config, tasks: list[Task], enabled: list[str], start: str) -> list[str]:
    t = next((x for x in tasks if x.runs_agent), None)
    if t is None:
        return []
    # the provider that task would really begin on: the first of the run's order it may use
    allowed = t.providers or enabled
    first = next((p for p in [start, *enabled] if p in enabled and p in allowed), start)
    if first not in cfg.providers:
        return []
    spec = cfg.spec(t.resolved_rung, first)
    if not spec:
        return []
    pc = cfg.providers[first]
    argv, env = build_command(pc, spec, t.max_turns or cfg.max_turns or None)
    if pc.name != pc.kind:
        key = PROVIDER_HOME_ENV[pc.kind]
        argv = ["env", f"{key}={env[key]}", *argv]
    return ["", f"first command (cwd {t.repo}, prompt on stdin):", "  " + shlex.join(argv)]


def plan_text(cfg: Config, tasks: list[Task], provider: str | None = None,
              now: datetime | None = None) -> str:
    state = load_state(cfg.state_dir / "run.json")
    enabled = cfg.enabled_providers()
    start = provider or cfg.start_provider
    tz = cfg.timezone or local_zone()
    now = now or datetime.now(timezone.utc)
    expire_limits(state, now)       # a limit that has passed is not shown
    active = run_active(cfg)
    by_id = {x.id: x for x in tasks}
    lines = _plan_header(cfg, state, enabled, start, provider, tz, now)
    for i, t in enumerate(tasks, 1):
        lines.append(_task_line(cfg, t, i, state, enabled, active, by_id))
    lines += _restore_lines(tasks)
    lines += _first_command_lines(cfg, tasks, enabled, start)
    return "\n".join(lines)


def is_within(path: Path, folder: Path) -> bool:
    """True when path is folder itself or lies inside it."""
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False
