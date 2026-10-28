"""Frontend-version staleness check.

The API declares — via a JSON file at project root (`frontend_versions.json`) —
which client versions are considered fresh (code 0), out-of-date but usable
(code 5), or too old to serve (code 67). The frontend sends its own version
as `?v=vMAJOR.MINOR.PATCH-CHANNEL` on `/v1/config`; this module parses that
string, matches it against the JSON's patterns, and returns the code.

Patterns look like `v3.(0-4).(0-9)-*`. Each of the four fields is either:
- an exact number 0-99 (or literal channel `Release`/`Beta`/`Alpha`),
- an inclusive range `a-b` (numeric fields only),
- a `*` wildcard.

If several patterns match one version, we pick by:
1. more exact-value fields wins (exact = 1, range = 0, wildcard = 0);
2. else, smaller total range width wins (width = b-a+1 for ranges, 1 for
   exacts, 0 for wildcards — wildcards deliberately don't participate here);
3. else, fewer wildcards wins;
4. else, random pick — all remaining candidates are equally specific.

The JSON is cached in memory for 20 minutes. After TTL, the *stale* cache is
still served immediately while a background task refreshes it (single-flight,
so a burst of requests doesn't spawn N reloaders).
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


Channel = Literal['Release', 'Beta', 'Alpha']


class PatternParseError(ValueError):
    """The JSON contained a pattern the loader could not understand — treated
    like a syntax error at load time; the entry is dropped and logged (via
    print for now — swap for structured logging when the app grows one)."""


@dataclass(frozen=True)
class ClientVersion:
    major: int
    minor: int
    patch: int
    channel: Channel


@dataclass(frozen=True)
class NumericField:
    """One of the three numeric fields of a version pattern."""

    kind: Literal['exact', 'range', 'wildcard']
    lo: int
    hi: int

    def matches(self, n: int) -> bool:
        if self.kind == 'wildcard':
            return True
        return self.lo <= n <= self.hi


@dataclass(frozen=True)
class ChannelField:
    kind: Literal['exact', 'wildcard']
    value: Channel | None

    def matches(self, c: str) -> bool:
        if self.kind == 'wildcard':
            return True
        return self.value == c


@dataclass(frozen=True)
class VersionPattern:
    major: NumericField
    minor: NumericField
    patch: NumericField
    channel: ChannelField
    code: int
    raw: str

    def matches(self, v: ClientVersion) -> bool:
        return (
            self.major.matches(v.major)
            and self.minor.matches(v.minor)
            and self.patch.matches(v.patch)
            and self.channel.matches(v.channel)
        )

    def exact_count(self) -> int:
        n = 0
        for f in (self.major, self.minor, self.patch):
            if f.kind == 'exact':
                n += 1
        if self.channel.kind == 'exact':
            n += 1
        return n

    def total_range_width(self) -> int:
        """Sum of (hi-lo+1) across numeric fields — exacts contribute 1, wildcards 0.
        Deliberately excludes wildcards so they're only counted in wildcard_count."""
        total = 0
        for f in (self.major, self.minor, self.patch):
            if f.kind == 'wildcard':
                continue
            total += (f.hi - f.lo + 1)
        return total

    def wildcard_count(self) -> int:
        n = 0
        for f in (self.major, self.minor, self.patch):
            if f.kind == 'wildcard':
                n += 1
        if self.channel.kind == 'wildcard':
            n += 1
        return n


@dataclass
class _CacheEntry:
    patterns: list[VersionPattern]
    loaded_at: float


_CACHE_TTL_SECONDS = 20 * 60  # 20 minutes per module docstring

# Client version string sent as ?v=vMAJOR.MINOR.PATCH-CHANNEL on /v1/config.
_CLIENT_VERSION_RE = re.compile(
    r'^v(\d{1,2})\.(\d{1,2})\.(\d{1,2})-(Release|Beta|Alpha)$'
)

# Pattern string as stored in the JSON: `v3.(0-4).(0-9)-*` etc. Each numeric
# field is either `N`, `(A-B)`, or `*`; the channel is `Release`/`Beta`/`Alpha`
# or `*`. Actual per-field validation lives in _parse_numeric_field.
_PATTERN_RE = re.compile(
    r'^v(?P<major>\*|\(\d{1,2}-\d{1,2}\)|\d{1,2})'
    r'\.(?P<minor>\*|\(\d{1,2}-\d{1,2}\)|\d{1,2})'
    r'\.(?P<patch>\*|\(\d{1,2}-\d{1,2}\)|\d{1,2})'
    r'-(?P<channel>\*|Release|Beta|Alpha)$'
)

# frontend_versions.json sits at the project root — this file is under
# app/services/, so two parents up gets us to app/, three gets the project.
_JSON_PATH = Path(__file__).resolve().parent.parent.parent / 'frontend_versions.json'


# Module-level cache. _refresh_task doubles as the single-flight guard: while
# it is non-None a background reload is in progress and _get_patterns will not
# start another one.
_cache: _CacheEntry | None = None
_refresh_task: asyncio.Task | None = None


def parse_client_version(raw: str) -> ClientVersion | None:
    """Returns None if the string doesn't match the strict v0-99.0-99.0-99-Channel
    shape — callers should turn that into a 400 BAD_REQUEST."""
    m = _CLIENT_VERSION_RE.match(raw)
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (0 <= major <= 99):
        return None
    if not (0 <= minor <= 99):
        return None
    if not (0 <= patch <= 99):
        return None
    return ClientVersion(
        major=major,
        minor=minor,
        patch=patch,
        channel=m.group(4),
    )


def _parse_numeric_field(raw: str) -> NumericField:
    if raw == '*':
        return NumericField(kind='wildcard', lo=0, hi=0)
    if raw.startswith('('):
        # `(A-B)` — strip the parens, split on '-'.
        inner = raw[1:-1]
        a_str, b_str = inner.split('-', 1)
        a, b = int(a_str), int(b_str)
        if not (0 <= a <= 99) or not (0 <= b <= 99):
            raise PatternParseError(f'numeric bound out of 0-99: {raw!r}')
        if a > b:
            raise PatternParseError(f'range low > high: {raw!r}')
        if a == b:
            return NumericField(kind='exact', lo=a, hi=a)
        return NumericField(kind='range', lo=a, hi=b)
    n = int(raw)
    if not (0 <= n <= 99):
        raise PatternParseError(f'exact value out of 0-99: {raw!r}')
    return NumericField(kind='exact', lo=n, hi=n)


def _parse_pattern(pattern_str: str, code: int) -> VersionPattern:
    m = _PATTERN_RE.match(pattern_str)
    if not m:
        raise PatternParseError(f'malformed pattern: {pattern_str!r}')
    major = _parse_numeric_field(m.group('major'))
    minor = _parse_numeric_field(m.group('minor'))
    patch = _parse_numeric_field(m.group('patch'))
    ch_raw = m.group('channel')
    if ch_raw == '*':
        channel = ChannelField(kind='wildcard', value=None)
    else:
        channel = ChannelField(kind='exact', value=ch_raw)
    return VersionPattern(
        major=major,
        minor=minor,
        patch=patch,
        channel=channel,
        code=code,
        raw=pattern_str,
    )


def _load_patterns_sync() -> list[VersionPattern]:
    """Reads and parses the JSON. Called both on the sync first-load path and
    inside asyncio.to_thread on the background refresh path."""
    if not _JSON_PATH.exists():
        return []
    try:
        with _JSON_PATH.open('r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f'[frontend_version] failed to read {_JSON_PATH}: {exc}')
        return []

    versions = raw.get('versions') if isinstance(raw, dict) else None
    if not isinstance(versions, dict):
        print(f"[frontend_version] {_JSON_PATH} missing dict 'versions' key")
        return []

    out: list[VersionPattern] = []
    for pattern_str, code in versions.items():
        if not (isinstance(pattern_str, str) and isinstance(code, int)):
            print(
                f'[frontend_version] skipping bad entry: {pattern_str!r} -> {code!r}'
            )
            continue
        try:
            out.append(_parse_pattern(pattern_str, code))
        except PatternParseError as exc:
            print(f'[frontend_version] skipping pattern: {exc}')
    return out


async def _refresh_in_background() -> None:
    """Reloads the JSON off the event loop and swaps it into the cache. Only one
    of these runs at a time — see _get_patterns."""
    global _cache, _refresh_task
    try:
        patterns = await asyncio.to_thread(_load_patterns_sync)
        _cache = _CacheEntry(patterns=patterns, loaded_at=time.monotonic())
    finally:
        _refresh_task = None


def _get_patterns() -> list[VersionPattern]:
    """Returns the current pattern list, kicking off a background refresh if the
    cache is stale but has data. On the very first call — no cache yet — loads
    synchronously so we have something to return."""
    global _cache, _refresh_task
    now = time.monotonic()
    if _cache is None:
        _cache = _CacheEntry(patterns=_load_patterns_sync(), loaded_at=now)
        return _cache.patterns
    age = now - _cache.loaded_at
    if age >= _CACHE_TTL_SECONDS and _refresh_task is None:
        # Kick off a single-flight refresh; return stale data immediately. If
        # we're outside a running loop (e.g. tests, sync callers), we simply
        # skip the refresh — the next call from an async context will do it.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            _refresh_task = loop.create_task(_refresh_in_background())
    return _cache.patterns


def resolve_version_code(client: ClientVersion) -> int | None:
    """Returns the code from the best-matching pattern in the JSON, or None if
    no pattern matches (caller then returns 67 FRONTEND_UPDATE_NEEDED).

    Best-match ordering (all applied in sequence, each break-ties into the next):
    1. more exact-value fields wins
    2. smaller total range width wins
    3. fewer wildcards wins
    4. random pick among remaining equals"""
    patterns = _get_patterns()
    matches = [p for p in patterns if p.matches(client)]
    if not matches:
        return None

    def rank(p: VersionPattern) -> tuple[int, int, int]:
        # Negate exact_count so `min` picks the pattern with the MOST exacts;
        # width and wildcard_count are already "smaller is better".
        return (-p.exact_count(), p.total_range_width(), p.wildcard_count())

    best_rank = min(rank(p) for p in matches)
    top = [p for p in matches if rank(p) == best_rank]
    if len(top) == 1:
        chosen = top[0]
        return chosen.code
    chosen = random.choice(top)
    return chosen.code


def reset_cache_for_tests() -> None:
    """Clears in-memory cache — for tests that mutate the JSON file mid-run."""
    global _cache, _refresh_task
    _cache = None
    _refresh_task = None
