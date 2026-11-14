"""browser-smoke: open each new or changed route in the real app, logged out
and logged in, and look at what a person would see.

baton starts or reuses the dev servers, writes a job list, and runs a Playwright
driver (baton/checks/browser_driver.js, Node) that only observes: final URL,
console errors, requests, whether a spinner is still there, a screenshot, and
the message after an invalid submit. The verdicts are made here, in `evaluate`,
so they are testable without a browser.

Settings: `[browser_smoke]` in baton.toml, overridden by the verifier's `params`.

    node = "node"                   # runs the driver; `playwright` must resolve from node_dir
    node_dir = "../user-panel"   # cwd of the driver; its node_modules hold playwright
    driver_cmd = [...]              # replaces node + built-in driver (a custom or fake driver)
    storage_state = "qa/state.json" # Playwright storage state of a TEST account, never a personal one
    api_origins = ["http://127.0.0.1:8000"]   # where API (fetch/xhr) calls must go
    spinner_selector = "[aria-busy=true], .spinner"
    spinner_timeout_s = 10
    startup_timeout_s = 90
    ignore_console = ["^Failed to load resource"]   # regexes; the request itself is judged below
    servers = [{ name = "panel", cwd = "../panel", cmd = "npm run dev", url = "http://localhost:3000" }]
    base_url = "http://localhost:3000"

    [[params.routes]]
    path = "/join?token=abc"
    modes = ["logged_out", "logged_in"]      # default both
    logged_out_redirect = "/login"           # the redirect that is expected, if any
    reuse = "/invitations"                   # the Reuse reference page: also screenshotted
    expect_endpoints = ["/api/plans"]        # must be called when logged in
    invalid_submit = { steps = [{ fill = "#email", value = "x" }, { click = "button[type=submit]" }],
                       message_selector = "[role=alert]", expect_field = "email" }
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import CheckContext, CheckResult
from .errormsg import check_error_message
from .servers import CheckError, dev_servers

DRIVER = Path(__file__).with_name("browser_driver.js")
MODES = ("logged_out", "logged_in")


def setting(ctx: CheckContext, key: str, default=None):
    return ctx.params.get(key, ctx.cfg.browser_smoke.get(key, default))


def path_of(url: str) -> str:
    return (urlsplit(url).path or "/").rstrip("/") or "/"


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "root"


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()


def _one_job(ctx: CheckContext, i: int, route: dict, mode: str, base: str, storage_state: str | None,
             ref_mode: str, submit_mode: str) -> dict:
    stem = f"{i:02d}-{slug(route['path'])}-{mode}"
    job = {
        "id": f"{route['path']} [{mode}]", "route": route["path"], "mode": mode,
        "url": urljoin(base + "/", route["path"].lstrip("/")),
        "storage_state": storage_state if mode == "logged_in" else None,
        "screenshot": str(ctx.out_dir / f"{stem}.png"),
    }
    if route.get("reuse") and mode == ref_mode:
        job["reference"] = {"url": urljoin(base + "/", route["reuse"].lstrip("/")),
                            "screenshot": str(ctx.out_dir / f"{i:02d}-reuse-{slug(route['reuse'])}.png")}
    if route.get("invalid_submit") and mode == submit_mode:
        job["invalid_submit"] = route["invalid_submit"]
    return job


def _route_jobs(ctx: CheckContext, i: int, route: dict, base_url: str,
                storage_state: str | None) -> tuple[list[dict], list[str]]:
    """The jobs (one per mode) for one route, and what is wrong with its settings."""
    if "path" not in route:
        return [], [f"routes[{i}] has no path"]
    modes = route.get("modes", list(MODES))
    bad = [m for m in modes if m not in MODES]
    if bad:
        return [], [f"route {route['path']}: unknown mode(s) {', '.join(bad)}"]
    ref_mode = "logged_in" if "logged_in" in modes else modes[0]
    submit_mode = (route.get("invalid_submit") or {}).get("mode", ref_mode)
    jobs: list[dict] = []
    gaps: list[str] = []
    for mode in modes:
        if mode == "logged_in" and not storage_state:
            gaps.append(f"route {route['path']}: logged_in needs a test session; set "
                        "[browser_smoke] storage_state to a Playwright storage state of a test account")
            continue
        jobs.append(_one_job(ctx, i, route, mode, route.get("base_url", base_url), storage_state,
                             ref_mode, submit_mode))
    return jobs, gaps


def build_jobs(ctx: CheckContext, base_url: str, storage_state: str | None) -> tuple[list[dict], list[str]]:
    jobs: list[dict] = []
    gaps: list[str] = []
    for i, route in enumerate(ctx.params.get("routes", [])):
        route_jobs, route_gaps = _route_jobs(ctx, i, route, base_url, storage_state)
        jobs += route_jobs
        gaps += route_gaps
    return jobs, gaps


def run_driver(ctx: CheckContext, spec: dict) -> list[dict]:
    spec_path, obs_path = ctx.out_dir / "browser-spec.json", ctx.out_dir / "browser-observations.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    obs_path.unlink(missing_ok=True)
    custom = setting(ctx, "driver_cmd")
    node_dir = Path(os.path.expanduser(setting(ctx, "node_dir", ".")))
    node_dir = node_dir if node_dir.is_absolute() else ctx.cfg.base_dir / node_dir
    argv = (list(custom) if custom else [setting(ctx, "node", "node"), str(DRIVER)]) + [str(spec_path), str(obs_path)]
    try:
        p = subprocess.run(argv, cwd=node_dir if node_dir.is_dir() else ctx.cfg.base_dir, capture_output=True,
                           text=True, timeout=float(setting(ctx, "driver_timeout_s", 600)))
    except OSError as e:
        raise CheckError(f"could not run the browser driver ({argv[0]}): {e}") from e
    except subprocess.TimeoutExpired as e:
        raise CheckError("the browser driver timed out") from e
    if p.returncode != 0 or not obs_path.is_file():
        tail = "\n".join((p.stderr or p.stdout).strip().splitlines()[-6:])
        raise CheckError(f"the browser driver failed (exit {p.returncode}); is Playwright installed where "
                         f"node_dir points?\n{tail}")
    return json.loads(obs_path.read_text(encoding="utf-8")).get("observations", [])


def run(ctx: CheckContext) -> CheckResult:
    res = CheckResult()
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    servers = list(setting(ctx, "servers", []))
    base_url = setting(ctx, "base_url") or (servers[0]["url"] if servers else None)
    if not base_url:
        raise CheckError("no base_url and no servers configured: nothing to open")
    if not ctx.params.get("routes"):
        raise CheckError("the verifier lists no routes ([[params.routes]])")
    storage = setting(ctx, "storage_state")
    if storage:
        storage = os.path.expanduser(storage)
        storage = storage if os.path.isabs(storage) else str(ctx.cfg.base_dir / storage)
        if not Path(storage).is_file():
            res.gaps.append(f"storage_state {storage} does not exist: record a test session first")
            storage = None
    jobs, gaps = build_jobs(ctx, base_url.rstrip("/"), storage)
    res.gaps += gaps
    if not jobs:
        return res
    spec = {"jobs": jobs, "spinner_selector": setting(ctx, "spinner_selector", ""),
            "spinner_timeout_ms": int(float(setting(ctx, "spinner_timeout_s", 10)) * 1000),
            "nav_timeout_ms": int(float(setting(ctx, "nav_timeout_s", 30)) * 1000)}
    with dev_servers(servers, ctx.cfg.base_dir, ctx.out_dir, float(setting(ctx, "startup_timeout_s", 90)),
                     ctx.should_stop) as notes:
        res.checked += notes
        observations = run_driver(ctx, spec)
    evaluate(ctx, jobs, observations, res)
    res.files += [Path(j["screenshot"]) for j in jobs if Path(j["screenshot"]).is_file()]
    res.files += [Path(j["reference"]["screenshot"]) for j in jobs
                  if "reference" in j and Path(j["reference"]["screenshot"]).is_file()]
    return res


def _console_problems(obs: dict, ignore: list) -> list[str]:
    errors = [e for e in obs.get("console_errors", []) if not any(rx.search(e) for rx in ignore)]
    if not errors:
        return []
    return [f"{len(errors)} console error(s): " + " | ".join(e[:160] for e in errors[:3])]


def _api_problems(calls: list[dict], api_origins: list[str]) -> list[str]:
    problems: list[str] = []
    if api_origins:
        for r in calls:
            if origin_of(r["url"]) not in api_origins:
                problems.append(f"API call {r.get('method', 'GET')} {r['url']} goes to {origin_of(r['url'])}, "
                                f"expected {', '.join(api_origins)}")
                break
    for r in calls:
        if r.get("status", 0) >= 500 or r.get("failure"):
            problems.append(f"API call {r.get('method', 'GET')} {r['url']} -> "
                            f"{r.get('status') or r.get('failure')}")
    return problems


def _job_problems(ctx: CheckContext, job: dict, route: dict, obs: dict, api_origins: list[str],
                  ignore: list) -> tuple[list[str], list[dict]]:
    """What is wrong with one route in one mode, and the API calls it made."""
    problems: list[str] = []
    if obs.get("error"):
        problems.append(f"could not load the page: {obs['error']}")
    if obs.get("spinner_stuck"):
        problems.append(f"endless spinner: still loading after {setting(ctx, 'spinner_timeout_s', 10):g}s")
    problems += _console_problems(obs, ignore)
    problems += redirect_problems(job, route, obs)
    calls = [r for r in obs.get("requests", []) if r.get("resource_type") in ("fetch", "xhr")]
    problems += _api_problems(calls, api_origins)
    if job["mode"] == route.get("expect_endpoints_in", "logged_in"):
        problems += [f"expected a call to {ep}; none was made" for ep in route.get("expect_endpoints", [])
                     if not any(ep in r["url"] for r in calls)]
    if not obs.get("screenshot") or not Path(obs["screenshot"]).is_file():
        problems.append("no screenshot was saved")
    if "invalid_submit" in job:
        problems += submit_problems(job["invalid_submit"], obs.get("invalid_submit"))
    return problems, calls


def evaluate(ctx: CheckContext, jobs: list[dict], observations: list[dict], res: CheckResult) -> None:
    """Judge the driver's observations; appends to res.gaps / res.checked / res.skipped."""
    by_id = {o.get("id"): o for o in observations}
    routes = {r["path"]: r for r in ctx.params.get("routes", [])}
    api_origins = [o.rstrip("/").lower() for o in setting(ctx, "api_origins", [])]
    ignore = [re.compile(p) for p in setting(ctx, "ignore_console", ["^Failed to load resource"])]
    if not api_origins:
        res.skipped.append("API host (browser_smoke.api_origins is not set)")
    for job in jobs:
        label = job["id"]
        obs = by_id.get(label)
        if obs is None:
            res.gaps.append(f"{label}: the driver reported nothing for this route")
            continue
        problems, calls = _job_problems(ctx, job, routes[job["route"]], obs, api_origins, ignore)
        if problems:
            res.gaps += [f"{label}: {p}" for p in problems]
        else:
            res.checked.append(f"{label}: loaded at {path_of(obs.get('final_url', ''))}, "
                               f"{len(calls)} API call(s), no console errors")


def redirect_problems(job: dict, route: dict, obs: dict) -> list[str]:
    final = obs.get("final_url") or ""
    if not final:
        return []
    wanted = route.get(f"{job['mode']}_redirect")
    if wanted:
        ok = final.startswith(wanted) if "://" in wanted else path_of(final) == path_of(wanted)
        return [] if ok else [f"expected a redirect to {wanted}, ended at {final}"]
    same = origin_of(final) == origin_of(job["url"]) and path_of(final) == path_of(job["url"])
    return [] if same else [f"unexpected redirect to {final}"]


def submit_problems(spec: dict, seen: dict | None) -> list[str]:
    if not seen:
        return ["the invalid submit did not run"]
    if seen.get("error"):
        return [f"the invalid submit failed: {seen['error']}"]
    return [f"invalid submit: {p}" for p in
            check_error_message(seen.get("message"), seen.get("status"), spec.get("expect_field"))]
