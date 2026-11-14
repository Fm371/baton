"""Which provider runs next.

The rule:
  * stay on the current provider until it is limited (sticky while a task is
    running; a provider coming back is not a reason to switch it mid-task);
  * a task that has not started an attempt yet begins on the preferred
    provider (the first in the list, `start_provider`) whenever that one is
    available, so a switch to the other provider lasts only while the
    preferred one is limited;
  * when it is limited, move to another available provider;
  * when every provider is limited, sleep until the earliest reset and
    continue on that provider.

Which task runs next is decided by select(): the first ready task, in queue
order, that can start now; a task whose allowed providers are all limited
lets the next one go ahead (skip-ahead).

With [run] lanes > 1 there is no single current provider: choose_provider()
picks per task (the provider it last ran on while available, else the first
available one) and the module-level select() starts several tasks at once,
see baton/lanes.py and docs/design/parallel-lanes.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ProviderStatus:
    limited_until: datetime | None = None
    window: str = ""
    disabled: str | None = None          # reason, e.g. "auth error"
    reset_known: bool = True             # False: limited_until is only a cooldown guess, not a time read from the message
    # a limit of one model family ("You've hit your Opus limit"): only tasks whose model for this
    # provider is of that family wait; the others keep running. family -> reset time
    model_limits: dict[str, datetime] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "limited_until": self.limited_until.isoformat() if self.limited_until else None,
            "window": self.window,
            "disabled": self.disabled,
            "reset_known": self.reset_known,
            "model_limits": {m: u.isoformat() for m, u in self.model_limits.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderStatus":
        lu = d.get("limited_until")
        return cls(limited_until=datetime.fromisoformat(lu) if lu else None,
                   window=d.get("window", ""), disabled=d.get("disabled"),
                   reset_known=d.get("reset_known", True),
                   model_limits={m: datetime.fromisoformat(u) for m, u in (d.get("model_limits") or {}).items()})

    def blocked_until(self, now: datetime, model: str | None = None) -> datetime | None:
        """When a task using `model` (a model id, any case) can run again here; None: it can now.
        The provider-wide limit and the limit of the model's family both count."""
        ends = []
        if self.limited_until is not None and self.limited_until > now:
            ends.append(self.limited_until)
        if model:
            low = model.lower()
            ends += [u for fam, u in self.model_limits.items() if fam in low and u > now]
        return max(ends) if ends else None


@dataclass
class Pick:
    provider: str | None          # None: nothing can run
    wait_until: datetime | None   # set when every provider is limited
    switched_from: str | None = None
    reason: str = ""
    restricted_from: str | None = None   # the task's providers list excludes `current`; current is unchanged
    slot_busy: str | None = None         # lanes: the provider is available but all its slots are taken


def _usable(status: dict[str, ProviderStatus], p: str) -> bool:
    return status[p].disabled is None


def _available(status: dict[str, ProviderStatus], p: str, now: datetime,
               models: dict[str, str] | None = None) -> bool:
    s = status[p]
    return s.disabled is None and s.blocked_until(now, (models or {}).get(p)) is None


def _earliest_reset(providers: list[str], status: dict[str, ProviderStatus], ok: list[str],
                    allowed: list[str] | None, now: datetime, models: dict[str, str] | None) -> Pick:
    """Nothing in `ok` is available now: say so when nothing is usable at all, else wait for the
    earliest reset among the usable ones."""
    candidates = [p for p in ok if _usable(status, p)]
    if not candidates:
        if not ok:
            return Pick(None, None, reason=f"none of {', '.join(allowed or [])} is enabled")
        return Pick(None, None, reason="no usable provider: " + ", ".join(
            f"{p} ({status[p].disabled})" for p in ok))

    def until(x: str) -> datetime:
        return status[x].blocked_until(now, (models or {}).get(x)) or now
    p = min(candidates, key=lambda x: (until(x), providers.index(x)))
    return Pick(p, until(p), reason=f"all limited; earliest reset is {p}")


def choose_provider(providers: list[str], status: dict[str, ProviderStatus], allowed: list[str] | None,
                    last: str | None, now: datetime, busy: dict[str, int], slots: dict[str, int],
                    models: dict[str, str] | None = None) -> Pick:
    """Which provider a task would run on with several lanes. Pure.

    The target is `last` (the provider the task already ran on) while it is
    available, else the first available allowed provider in order. A busy slot
    never moves the task to another provider, only a limit or a disabled
    provider does: with every slot of the target taken the task waits for one
    (Pick.slot_busy). When nothing is available, wait_until is the earliest
    reset among the allowed providers that are still usable. `models` maps a provider to the model
    this task would use there, so a limit of another model family does not hold it back."""
    ok = [p for p in providers if allowed is None or p in allowed]
    avail = [p for p in ok if _available(status, p, now, models)]
    target = last if last in avail else (avail[0] if avail else None)
    if target is not None:
        if busy.get(target, 0) < slots.get(target, 1):
            return Pick(target, None)
        return Pick(None, None, reason=f"waiting for a free {target} slot", slot_busy=target)
    return _earliest_reset(providers, status, ok, allowed, now, models)


@dataclass(frozen=True)
class Candidate:
    """A ready task as select() sees it."""
    id: str
    allowed: tuple[str, ...] | None     # None: any provider; (): needs none (a checkpoint or built-in check)
    lane: str
    repo: str
    last: str | None = None             # the provider the task last ran on
    models: tuple[tuple[str, str], ...] = ()   # (provider, model id) for the task's rung


def select(candidates: list[Candidate], providers: list[str], status: dict[str, ProviderStatus],
           slots: dict[str, int], busy_lanes: set[str], busy_repos: set[str], busy_slots: dict[str, int],
           now: datetime, max_new: int) -> list[tuple[Candidate, str | None]]:
    """The ready tasks to start now, in queue order, each with its provider (None: needs none).
    A task is skipped when its lane or repository is busy, or its provider is limited
    or has no free slot; the next ready task may go ahead. Pure: changes nothing."""
    lanes, repos, busy = set(busy_lanes), set(busy_repos), dict(busy_slots)
    out: list[tuple[Candidate, str | None]] = []
    for c in candidates:
        if len(out) >= max_new:
            break
        if c.lane in lanes or c.repo in repos:
            continue
        if c.allowed is not None and not c.allowed:
            prov = None
        else:
            pick = choose_provider(providers, status, list(c.allowed) if c.allowed is not None else None,
                                   c.last, now, busy, slots, dict(c.models))
            if pick.provider is None or pick.wait_until is not None:
                continue
            prov = pick.provider
            busy[prov] = busy.get(prov, 0) + 1
        lanes.add(c.lane)
        repos.add(c.repo)
        out.append((c, prov))
    return out


class Scheduler:
    def __init__(self, providers: list[str], current: str, status: dict[str, ProviderStatus] | None = None,
                 return_to_preferred: bool = True):
        if not providers:
            raise ValueError("no providers")
        self.providers = list(providers)
        self.return_to_preferred = return_to_preferred
        self.status = {p: (status or {}).get(p, ProviderStatus()) for p in self.providers}
        self.current = current if current in self.providers else self.providers[0]

    def usable(self, p: str) -> bool:
        return self.status[p].disabled is None

    def available(self, p: str, now: datetime, model: str | None = None) -> bool:
        s = self.status[p]
        return s.disabled is None and s.blocked_until(now, model) is None

    def mark_limited(self, p: str, until: datetime, window: str, reset_known: bool = True) -> None:
        s = self.status[p]
        s.limited_until = until
        s.window = window
        s.reset_known = reset_known

    def mark_model_limited(self, p: str, family: str, until: datetime) -> None:
        """One model family is limited on `p` ("You've hit your Opus limit"); the rest of p is not."""
        self.status[p].model_limits[family.lower()] = until

    def disable(self, p: str, reason: str) -> None:
        self.status[p].disabled = reason

    def can_run(self, now: datetime, allowed: list[str] | None = None,
                models: dict[str, str] | None = None) -> bool:
        """True when pick() would start an attempt now instead of sleeping.
        An empty `allowed` list means the task needs no provider at all."""
        if allowed is not None and not allowed:
            return True
        return any(self.available(p, now, (models or {}).get(p))
                   for p in self.providers if allowed is None or p in allowed)

    def select(self, candidates: list[tuple], now: datetime) -> str | None:
        """The first (task id, allowed providers[, models]) in queue order that can start
        now, or None when each one would have to wait. Changes nothing."""
        return next((c[0] for c in candidates if self.can_run(now, c[1], c[2] if len(c) > 2 else None)), None)

    def pick_for(self, allowed: list[str] | None, last: str | None, now: datetime,
                 busy: dict[str, int], slots: dict[str, int], models: dict[str, str] | None = None) -> Pick:
        """Lanes: the provider for one task, see choose_provider(). Does not move `current`."""
        return choose_provider(self.providers, self.status, allowed, last, now, busy, slots, models)

    def select_lanes(self, candidates: list[Candidate], slots: dict[str, int], busy_lanes: set[str],
                     busy_repos: set[str], busy_slots: dict[str, int], now: datetime, max_new: int):
        return select(candidates, self.providers, self.status, slots, busy_lanes, busy_repos, busy_slots,
                      now, max_new)

    def pick(self, now: datetime, allowed: list[str] | None = None, fresh: bool = False,
             models: dict[str, str] | None = None) -> Pick:
        """allowed narrows the choice (a verifier's providers); the rule is unchanged.
        fresh: the task has not started an attempt yet, so it may go back to the
        preferred provider. models: provider -> the model id this task uses there; a limit of
        another model family on a provider does not make it unavailable for this task."""
        models = models or {}
        ok = [p for p in self.providers if allowed is None or p in allowed]
        for step in (self._back_to_preferred, self._restricted, self._stay, self._switch):
            got = step(ok, now, fresh, models)
            if got is not None:
                return got
        return _earliest_reset(self.providers, self.status, ok, allowed, now, models)

    def _back_to_preferred(self, ok: list[str], now: datetime, fresh: bool, models: dict[str, str]) -> Pick | None:
        first = self.providers[0]
        if (fresh and self.return_to_preferred and self.current != first and first in ok
                and self.available(first, now, models.get(first))):
            prev, self.current = self.current, first
            self._clear(first, now)
            return Pick(first, None, switched_from=prev, reason=f"back to the preferred provider {first}")
        return None

    def _restricted(self, ok: list[str], now: datetime, fresh: bool, models: dict[str, str]) -> Pick | None:
        """The task may not use the current provider: run it elsewhere without moving the run, which is
        not limited and returns to its own tasks. (`ok` shorter than the providers means a list was given.)"""
        if len(ok) == len(self.providers) or self.current in ok:
            return None
        for p in ok:
            if self.available(p, now, models.get(p)):
                self._clear(p, now)
                return Pick(p, None, restricted_from=self.current,
                            reason=f"providers list; current stays {self.current}")
        return None

    def _stay(self, ok: list[str], now: datetime, fresh: bool, models: dict[str, str]) -> Pick | None:
        if self.current in ok and self.available(self.current, now, models.get(self.current)):
            self._clear(self.current, now)
            return Pick(self.current, None)
        return None

    def _switch(self, ok: list[str], now: datetime, fresh: bool, models: dict[str, str]) -> Pick | None:
        for p in self._others():
            if p in ok and self.available(p, now, models.get(p)):
                prev, self.current = self.current, p
                self._clear(p, now)
                return Pick(p, None, switched_from=prev, reason=self._why(prev))
        return None

    def _others(self) -> list[str]:
        i = self.providers.index(self.current)
        return self.providers[i + 1:] + self.providers[:i]

    def _clear(self, p: str, now: datetime) -> None:
        s = self.status[p]
        if s.limited_until is not None and s.limited_until <= now:
            s.limited_until = None
            s.window = ""
        for fam in [f for f, u in s.model_limits.items() if u <= now]:
            del s.model_limits[fam]

    def _why(self, p: str) -> str:
        s = self.status[p]
        if s.disabled:
            return f"{p} disabled ({s.disabled})"
        if s.limited_until is None and s.model_limits:
            return f"{p} limited for " + ", ".join(sorted(s.model_limits))
        return f"{p} limited ({s.window or 'limit'})"

    def to_dict(self) -> dict:
        return {"current": self.current, "providers": {p: s.to_dict() for p, s in self.status.items()}}
