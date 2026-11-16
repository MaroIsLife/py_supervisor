"""Where everything starts.

./taskmaster   -> main_foreground(): engine in a thread + shell in the terminal
./taskmasterd  -> main_daemon():     engine only, in the background (bonus)
./taskmasterctl-> main_ctl():        shell only, connects to the daemon (bonus)
"""

import argparse
import os
import signal
import sys
import threading

from .config import ConfigError, load_config
from .logger import EventLogger
from .engine import Engine
from .shell import Connection, Shell, run_ctl


def load(path):
    """Read + check the config, open the log file, create the engine."""
    try:
        config = load_config(path)
    except ConfigError as e:
        sys.exit(f"taskmaster: configuration error: {e}")
    log = EventLogger()
    try:
        log.configure(config.settings)
    except OSError as e:
        sys.exit(f"taskmaster: cannot open log file: {e}")
    return config, log


def make_engine(config, log):
    try:
        return Engine(config, log)
    except RuntimeError as e:
        sys.exit(f"taskmaster: {e}")


# ------------------------------------------------------------- ./taskmaster
def main_foreground():
    ap = argparse.ArgumentParser(prog="taskmaster")
    ap.add_argument("-c", "--config", default="taskmaster.yaml")
    args = ap.parse_args()
    config, log = load(args.config)
    engine = make_engine(config, log)
    exiting = False

    # Signal handlers only set flags; the engine loop reads them (max 0.1s later).
    def on_sighup(signum, frame):
        engine.reload_requested = True

    def on_sigterm(signum, frame):
        engine.shutdown_requested = True
        if not exiting:
            raise SystemExit      # leaves the shell's input() below

    signal.signal(signal.SIGHUP, on_sighup)
    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGQUIT, on_sigterm)

    def engine_thread():
        engine.run()
        if not exiting:          # stopped from elsewhere (taskmasterctl shutdown)
            os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=engine_thread)
    thread.start()
    shell = Shell(Connection(config.settings.socket), embedded=True)
    try:
        shell.run()
    except SystemExit:
        print("\ntaskmaster: shutting down...")
    finally:
        exiting = True
        engine.shutdown_requested = True
        thread.join()            # wait until every program is stopped
    return 0


# ------------------------------------------------------------ ./taskmasterd
def daemonize():
    """Double fork: the daemon is not a child of the shell and has no terminal."""
    if os.fork() > 0:
        os._exit(0)        # parent: give the prompt back to the user
    os.setsid()            # new session, no controlling terminal
    if os.fork() > 0:
        os._exit(0)        # can never get a terminal again
    os.chdir("/")
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)


def main_daemon():
    ap = argparse.ArgumentParser(prog="taskmasterd")
    ap.add_argument("-c", "--config", default="taskmaster.yaml")
    ap.add_argument("-n", "--nodaemon", action="store_true", help="stay in the foreground")
    args = ap.parse_args()
    config, log = load(args.config)
    engine = make_engine(config, log)       # fails here if already running
    if not args.nodaemon:
        print(f"taskmasterd: running in background, log: {config.settings.logfile}")
        sys.stdout.flush()
        daemonize()

    def on_sighup(signum, frame):
        engine.reload_requested = True

    def on_stop(signum, frame):
        engine.shutdown_requested = True

    signal.signal(signal.SIGHUP, on_sighup)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
        signal.signal(s, on_stop)
    engine.run()
    return 0


# ---------------------------------------------------------- ./taskmasterctl
def main_ctl():
    ap = argparse.ArgumentParser(prog="taskmasterctl")
    ap.add_argument("-c", "--config", help="take the socket path from this config file")
    ap.add_argument("-s", "--socket", help="socket path (default /tmp/taskmaster.sock)")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="run one command and exit")
    args = ap.parse_args()
    path = args.socket or "/tmp/taskmaster.sock"
    if args.config:
        try:
            path = load_config(args.config).settings.socket
        except ConfigError as e:
            sys.exit(f"taskmasterctl: {e}")
    return run_ctl(path, args.command)
