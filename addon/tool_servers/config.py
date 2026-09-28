"""Tool server specs and the rules for turning preferences into a launch.

No bpy: the bridge copies the preference fields into ServerSpec on the
main thread, and everything after that works from the copy.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

NAME_MAX = 24
_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_TIMEOUT_S = 60.0
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    """A server's settings can't be turned into a launch."""


@dataclass(frozen=True)
class ServerSpec:
    name: str
    start: str
    env_text: str = ""
    token: str = ""
    trusted: bool = False
    enabled: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S

    @property
    def kind(self) -> str:
        return kind_of(self.start)

    def launch_key(self) -> tuple:
        """Fields that need a restart when they change (trust and timeout don't)."""
        return (self.start.strip(), self.env_text, self.token)


def kind_of(start: str) -> str:
    """"http" for an http(s):// address, otherwise "stdio" (a command)."""
    s = (start or "").strip().lower()
    return "http" if s.startswith(("https://", "http://")) else "stdio"


def sanitize_name(name: str) -> str:
    """Lowercase, invalid characters to "-", at most NAME_MAX characters."""
    cleaned = re.sub(r"[^a-z0-9-]", "-", (name or "").strip().lower())
    return cleaned[:NAME_MAX]


def name_problem(name: str) -> str | None:
    if not name:
        return "needs a name"
    if len(name) > NAME_MAX:
        return f"name is longer than {NAME_MAX} characters"
    if not _NAME_RE.match(name):
        return "name may only use a-z, 0-9 and -"
    return None


def expand_vars(text: str, environ: Mapping[str, str] | None = None) -> str:
    """Replace ${VAR} with its value; a variable that isn't set is an error."""
    env = os.environ if environ is None else environ

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in env:
            raise ConfigError(f"environment variable {key} is not set")
        return env[key]

    return _VAR_RE.sub(_sub, text or "")


def parse_env(text: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """KEY=value pairs, one per line or separated by ";". Values expand ${VAR}.

    Blender's text fields are single-line, so ";" is the practical separator.
    """
    out: dict[str, str] = {}
    for raw in re.split(r"[\n;]", text or ""):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            raise ConfigError(f"environment entry {line[:40]!r} isn't KEY=value")
        out[key] = expand_vars(value.strip(), environ)
    return out


def split_command(start: str) -> tuple[str, list[str]]:
    try:
        parts = shlex.split(start or "", posix=os.name != "nt")
    except ValueError as e:
        raise ConfigError(f"can't read the command: {e}") from None
    if not parts:
        raise ConfigError("needs a command or an https:// address")
    return parts[0], parts[1:]


@dataclass
class Launch:
    kind: str
    command: str = ""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    url: str = ""
    headers: dict = field(default_factory=dict)


def build_launch(spec: ServerSpec, environ: Mapping[str, str] | None = None) -> Launch:
    """How to start or reach a server. Raises ConfigError with a readable reason."""
    env_base = dict(os.environ if environ is None else environ)
    problem = name_problem(spec.name)
    if problem:
        raise ConfigError(problem)
    if spec.kind == "http":
        url = spec.start.strip()
        parsed = urlparse(url)
        if not parsed.hostname:
            raise ConfigError("the address has no host")
        headers = {}
        token = expand_vars(spec.token, env_base).strip()
        if token:
            if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
                raise ConfigError("tokens are only sent over https:// (or to localhost)")
            headers["Authorization"] = f"Bearer {token}"
        return Launch(kind="http", url=url, headers=headers)
    command, args = split_command(spec.start)
    env = dict(env_base)
    env.update(parse_env(spec.env_text, env_base))
    return Launch(kind="stdio", command=command, args=args, env=env)


def snapshot_problems(specs: list[ServerSpec]) -> dict[str, str]:
    """Per-name problems that keep a server from starting (bad or duplicate name)."""
    problems: dict[str, str] = {}
    seen: set[str] = set()
    for spec in specs:
        p = name_problem(spec.name)
        if p is None and spec.name in seen:
            p = "another server has this name"
        if p is not None and spec.name:
            problems[spec.name] = p
        seen.add(spec.name)
    return problems
