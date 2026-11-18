"""Read the YAML config file and check every option.

    taskmaster:      # optional: settings of taskmaster itself
      logfile: ...
    programs:        # one block per program to supervise
      <name>:
        cmd: ...
"""

from __future__ import annotations

import os
import pwd
import shlex
import signal
from dataclasses import dataclass, field, fields

import yaml


class ConfigError(Exception):
    pass


SIGNALS = {s.name[3:]: s for s in signal.Signals if not s.name.startswith("SIG_")}  # "TERM" -> SIGTERM


@dataclass
class ProgramConfig:
    name: str
    cmd: str                        # command line to launch
    numprocs: int = 1               # how many copies to keep running
    umask: int | None = None        # umask before exec (None = inherit)
    workingdir: str | None = None   # cwd before exec (None = inherit)
    autostart: bool = True          # start when taskmaster starts
    autorestart: str = "unexpected" # always | never | unexpected
    exitcodes: tuple = (0,)         # "expected" exit codes
    startretries: int = 3           # failed starts allowed before FATAL
    starttime: float = 1            # seconds alive = "successfully started"
    stopsignal: str = "TERM"        # signal for a graceful stop
    stoptime: float = 10            # seconds before SIGKILL
    stdout: str | None = None       # file, None = discard
    stderr: str | None = None       # file, None = discard
    env: dict = field(default_factory=dict)  # extra environment variables
    user: str | None = None         # bonus: run as this user (taskmaster must be root)

    @property
    def argv(self):
        return shlex.split(self.cmd)

    @property
    def signum(self):
        return SIGNALS[self.stopsignal]

    def same_except_numprocs(self, other):
        return all(getattr(self, f.name) == getattr(other, f.name)
                   for f in fields(self) if f.name != "numprocs")


@dataclass
class Settings:
    logfile: str = "/tmp/taskmaster.log"
    socket: str = "/tmp/taskmaster.sock"
    http_alert: str | None = None     # bonus: POST alerts to this URL


@dataclass
class Config:
    path: str
    settings: Settings
    programs: dict   # name -> ProgramConfig


def _path(base, value):
    """Relative paths are relative to the config file's folder."""
    value = os.path.expanduser(str(value))
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(base, value))


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_program(name, raw, base):
    def check(key, ok, msg):
        if not ok:
            raise ConfigError(f"program '{name}': option '{key}': {msg}")

    if not isinstance(raw, dict):
        raise ConfigError(f"program '{name}': expected a list of options")
    allowed = {f.name for f in fields(ProgramConfig)} - {"name"}
    for key in raw:
        check(key, key in allowed, "unknown option")
    check("cmd", isinstance(raw.get("cmd"), str) and raw["cmd"].strip(), "required, must be a command line")
    p = ProgramConfig(name=name, **raw)   # options not in the file keep their default value

    try:
        argv = p.argv
    except ValueError as e:              # e.g. a missing closing quote
        check("cmd", False, str(e))
    if "/" in argv[0] and not os.path.isabs(argv[0]):     # "programs/x.py" -> absolute path
        p.cmd = shlex.join([_path(base, argv[0])] + argv[1:])
    check("numprocs", _is_int(p.numprocs) and p.numprocs >= 1, "must be an integer >= 1")
    check("startretries", _is_int(p.startretries) and p.startretries >= 0, "must be an integer >= 0")
    for key in ("starttime", "stoptime"):
        value = getattr(p, key)
        check(key, isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0,
              "must be a number of seconds")
    check("autostart", isinstance(p.autostart, bool), "must be true or false")
    check("autorestart", p.autorestart in ("always", "never", "unexpected"),
          "must be always, never or unexpected")
    if _is_int(p.exitcodes):
        p.exitcodes = [p.exitcodes]
    check("exitcodes", isinstance(p.exitcodes, (list, tuple)) and p.exitcodes
          and all(_is_int(c) and 0 <= c <= 255 for c in p.exitcodes), "must be a code or a list of codes (0-255)")
    p.exitcodes = tuple(p.exitcodes)
    p.stopsignal = str(p.stopsignal).upper().removeprefix("SIG")
    check("stopsignal", p.stopsignal in SIGNALS, "unknown signal")
    check("umask", p.umask is None or (_is_int(p.umask) and 0 <= p.umask <= 0o777),
          "must be an octal value written with a leading 0, like 022")
    check("env", isinstance(p.env or {}, dict), "must be a list of NAME: value")
    p.env = {str(k): str(v) for k, v in (p.env or {}).items()}
    if p.user is not None:
        try:
            pwd.getpwnam(str(p.user))
        except KeyError:
            check("user", False, "unknown user")
    if p.workingdir is not None:
        p.workingdir = _path(base, p.workingdir)
    for key in ("stdout", "stderr"):
        value = getattr(p, key)
        setattr(p, key, None if value in (None, "discard") else _path(base, value))
    return p


def _parse_settings(raw, base):
    raw = raw or {}
    allowed = {f.name for f in fields(Settings)}
    for key in raw:
        if key not in allowed:
            raise ConfigError(f"section 'taskmaster': unknown option '{key}'")
    s = Settings(**raw)
    s.logfile, s.socket = _path(base, s.logfile), _path(base, s.socket)
    return s


def load_config(path):
    path = os.path.abspath(path)
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}")
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("programs") or {}, dict):
        raise ConfigError("the file must contain a 'programs:' section")
    for key in data:
        if key not in ("programs", "taskmaster"):
            raise ConfigError(f"unknown section '{key}'")
    base = os.path.dirname(path)
    programs = {str(n): _parse_program(str(n), p, base) for n, p in (data.get("programs") or {}).items()}
    return Config(path, _parse_settings(data.get("taskmaster"), base), programs)
