"""real-data-smoke: run the feature's main path against a copy of the dev
database instead of fixtures, and report each failure with the exact response.

Fixtures create the keys the code expects; a real database names them
differently. This starts the backend (`server`) with its database URL variable
(`db_env`) pointing at `smoke_db_url`, which must be a copy or snapshot, never
the dev database itself, and walks `steps` over HTTP.

    [real_data_smoke]               # in baton.toml; the verifier's params override
    smoke_db_url = "mongodb://localhost:27017/app_smoke"   # or env BATON_SMOKE_DB_URL
    db_env = "MONGO_URL"            # the variable the backend reads its database URL from
    # A backend that reads the database in pieces takes a table instead: each value may use
    # {url} {host} {port} {db} {user} {password} of smoke_db_url, or be a literal.
    # db_env = { MONGO_HOST = "{host}", MONGO_PORT = "{port}", MONGO_DB = "{db}" }
    # db_env = { MONGO_HOST = "127.0.0.1", MONGO_DB = "app_smoke" }   # no smoke_db_url needed
    prepare_cmd = "scripts/refresh-smoke-db.sh"  # optional: makes the copy; gets SMOKE_DB_URL
    allow_reuse = false             # true: use a server that already answers (its database is unchecked)
    base_url = "http://127.0.0.1:8000"
    server = { name = "api", cwd = "../api", cmd = "uvicorn app.main:app --port 8000", url = "http://127.0.0.1:8000/docs" }

    [[params.steps]]
    name = "log in"
    method = "POST"
    path = "/auth/login"
    json = { username = "qa", password = "${QA_PASSWORD}" }   # ${VAR}: from the environment, never printed
    capture = { token = "access_token" }                      # dotted path in the JSON response
    [[params.steps]]
    name = "enroll through the invite link"
    method = "POST"
    path = "/plans/{plan_id}/enroll"      # {name}: a captured value
    headers = { Authorization = "Bearer {token}" }
    expect_status = [200, 201]
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import CheckContext, CheckResult
from .servers import CheckError, dev_servers, reachable

BODY_LIMIT = 2000


def setting(ctx: CheckContext, key: str, default=None):
    return ctx.params.get(key, ctx.cfg.real_data_smoke.get(key, default))


def mask_url(url: str) -> str:
    """The URL without credentials."""
    p = urlsplit(url)
    host = p.hostname or "?"
    return f"{p.scheme}://{host}{':' + str(p.port) if p.port else ''}{p.path}"


SECRET_NAME = re.compile(r"pass|secret|token|key|pwd", re.IGNORECASE)


def _url_parts(db_url: str | None) -> dict[str, str]:
    p = urlsplit(db_url) if db_url else None
    return {"url": db_url or "", "host": (p.hostname or "") if p else "", "port": str(p.port or "") if p else "",
            "db": p.path.lstrip("/") if p else "", "user": (p.username or "") if p else "",
            "password": (p.password or "") if p else ""}


def _expand(var: str, template: str, parts: dict[str, str], db_url: str | None) -> str:
    for name in re.findall(r"\{(\w+)\}", template):
        if name not in parts:
            raise CheckError(f"db_env.{var}: unknown placeholder {{{name}}} (use {', '.join(parts)})")
        if not db_url:
            raise CheckError(f"db_env.{var} uses {{{name}}} but smoke_db_url is not set "
                             "([real_data_smoke] or BATON_SMOKE_DB_URL): this check runs against a copy "
                             "of the dev database and will not fall back to fixtures")
    return re.sub(r"\{(\w+)\}", lambda m: parts[m.group(1)], template)


def db_variables(db_env, db_url: str | None) -> dict[str, str]:
    """The environment variables that point the backend at the smoke database.
    A string names the one variable that gets the URL; a table maps each variable
    to a template over the URL's parts."""
    if isinstance(db_env, str):
        return {db_env: db_url or ""}
    if not isinstance(db_env, dict):
        raise CheckError("db_env must be a variable name or a table of variable = value")
    parts = _url_parts(db_url)
    return {var: _expand(var, str(template), parts, db_url) for var, template in db_env.items()}


def describe_database(db_env, db_vars: dict[str, str], db_url: str | None) -> str:
    if isinstance(db_env, dict):
        shown = {k: "***" if SECRET_NAME.search(k) else mask_url(v) if "://" in v else v for k, v in db_vars.items()}
        return "database: " + ", ".join(f"{k}={v}" for k, v in shown.items())
    return f"database: {mask_url(db_url)}"


def substitute(value, captured: dict[str, str], env: dict[str, str], expand_env: bool):
    """Replace {name} with captured values and, when expand_env, ${VAR} with the environment."""
    if isinstance(value, str):
        if expand_env:
            value = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), value)
        return re.sub(r"\{(\w+)\}", lambda m: captured.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: substitute(v, captured, env, expand_env) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, captured, env, expand_env) for v in value]
    return value


def dig(data, dotted: str):
    for part in dotted.split("."):
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data


def call(method: str, url: str, headers: dict, body, timeout: float) -> tuple[int, str]:
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _smoke_settings(ctx: CheckContext):
    """(db_url, db_env, db_vars, steps, server, base_url), after checking they fit together."""
    db_url = setting(ctx, "smoke_db_url") or os.environ.get("BATON_SMOKE_DB_URL")
    db_env = setting(ctx, "db_env")
    steps = ctx.params.get("steps", [])
    if not db_url and not isinstance(db_env, dict):
        raise CheckError("smoke_db_url is not set ([real_data_smoke] or BATON_SMOKE_DB_URL): this check runs "
                         "against a copy of the dev database and will not fall back to fixtures")
    db_vars = db_variables(db_env, db_url) if db_env else {}
    if not steps:
        raise CheckError("the verifier lists no steps ([[params.steps]])")
    server = setting(ctx, "server")
    base_url = (setting(ctx, "base_url") or (server or {}).get("url", "")).rstrip("/")
    if not base_url:
        raise CheckError("no base_url and no server configured")
    if server and not db_env:
        raise CheckError("db_env is not set: baton cannot point the backend it starts at smoke_db_url")
    return db_url, db_env, db_vars, steps, server, base_url


def _prepare(ctx: CheckContext, env: dict[str, str], res: CheckResult) -> None:
    """Refresh the smoke database with `prepare_cmd`, when one is set."""
    prepare = setting(ctx, "prepare_cmd")
    if not prepare:
        return
    argv = shlex.split(prepare) if isinstance(prepare, str) else list(prepare)
    p = subprocess.run(argv, cwd=ctx.cfg.base_dir, env=env, capture_output=True, text=True,
                       timeout=float(setting(ctx, "prepare_timeout_s", 1800)))
    if p.returncode != 0:
        raise CheckError(f"prepare_cmd failed (exit {p.returncode}):\n"
                         + "\n".join((p.stderr or p.stdout).strip().splitlines()[-6:]))
    res.checked.append("prepare_cmd refreshed the smoke database")


def run(ctx: CheckContext) -> CheckResult:
    res = CheckResult()
    db_url, db_env, db_vars, steps, server, base_url = _smoke_settings(ctx)
    env = {**os.environ, "SMOKE_DB_URL": db_url or ""}
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    _prepare(ctx, env, res)
    reuse = bool(setting(ctx, "allow_reuse", False))
    if server and not reuse and reachable(server["url"]):
        raise CheckError(f"something already answers at {server['url']} and may use another database "
                         "(the dev one, perhaps). Stop it, or set allow_reuse = true to use it unchecked")
    if reuse:
        res.skipped.append("which database the reused server uses (allow_reuse = true)")
    specs = [server] if server else []
    with dev_servers(specs, ctx.cfg.base_dir, ctx.out_dir, float(setting(ctx, "startup_timeout_s", 90)),
                     ctx.should_stop, reuse=reuse, env=db_vars or None) as notes:
        res.checked += notes
        res.checked.append(describe_database(db_env, db_vars, db_url))
        walk(ctx, steps, base_url, env, res)
    return res


def walk(ctx: CheckContext, steps: list[dict], base_url: str, env: dict[str, str], res: CheckResult) -> None:
    captured: dict[str, str] = {}
    timeout = float(setting(ctx, "request_timeout_s", 30))
    for i, step in enumerate(steps, 1):
        name = step.get("name") or f"step {i}"
        method, path = step.get("method", "GET").upper(), step["path"]
        shown = f"{method} {substitute(path, captured, env, False)}"
        try:
            status, body = call(method, base_url + substitute(path, captured, env, True),
                                substitute(step.get("headers", {}), captured, env, True),
                                substitute(step.get("json"), captured, env, True), timeout)
        except (urllib.error.URLError, OSError) as e:
            res.gaps.append(f"{name}: {shown}: no response ({e})")
            return
        expected = step.get("expect_status", [200, 201, 204])
        expected = [expected] if isinstance(expected, int) else expected
        if status not in expected:
            res.gaps.append(f"{name}: {shown} -> HTTP {status}, expected {'/'.join(map(str, expected))}. "
                            f"Response: {body[:BODY_LIMIT]}{' (cut)' if len(body) > BODY_LIMIT else ''}")
            return      # later steps depend on this one
        try:
            payload = json.loads(body) if body.strip() else None
        except json.JSONDecodeError:
            payload = None
        failed = False
        for var, dotted in step.get("capture", {}).items():
            value = dig(payload, dotted)
            if value is None:
                res.gaps.append(f"{name}: {shown} -> HTTP {status}, but the response has no `{dotted}` "
                                f"to capture as {var}. Response: {body[:BODY_LIMIT]}")
                failed = True
            else:
                captured[var] = str(value)
        for dotted, want in step.get("expect_json", {}).items():
            got = dig(payload, dotted)
            if got != want:
                res.gaps.append(f"{name}: {shown} -> `{dotted}` is {json.dumps(got)}, expected {json.dumps(want)}. "
                                f"Response: {body[:BODY_LIMIT]}")
                failed = True
        if failed:
            return
        res.checked.append(f"{name}: {shown} -> HTTP {status}")
