"""The heart of taskmaster: ONE loop that does everything.

    while not finished:
        1. handle signal flags     (SIGHUP -> reload, SIGTERM -> shutdown)
        2. select()                (new client? client command? program output?)
        3. waitpid(-1, WNOHANG)    (which children died? -> Process.exited)
        4. tick every process      (starttime / stoptime / backoff timers)
        5. run "when" callbacks    (send delayed answers: "stopped", "started"...)

Only this loop touches the processes, so there are no locks.
Control shells talk to it through a UNIX socket, one JSON object per line:
    shell -> taskmaster : {"cmd": "stop", "args": ["ticker"]}
    taskmaster -> shell : {"output": "ticker:0: stopped\\nticker:1: stopped"}
"""

import json
import os
import select
import socket
import time

from .config import ConfigError, load_config
from .process import ALIVE, RUNNING, STARTING, STOPPING, Process


class Client:
    """A connected control shell."""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""
        self.attached = None     # Process this shell is attached to (bonus)

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())


class Engine:
    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.programs = {}        # program name -> [Process, ...]
        self.retiring = []        # processes removed by a reload, still dying
        self.clients = {}         # socket -> Client
        self.waiters = []         # (condition, action, deadline): see when()
        self.reload_requested = False     # set by the SIGHUP handler
        self.shutdown_requested = False   # set by the SIGTERM handler
        self.shutting_down = False
        self.finished = False
        self.server = self.listen(config.settings.socket)

    # ================================================================ setup
    def listen(self, path):
        if os.path.exists(path):
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(path)
                raise RuntimeError(f"another taskmaster is already running ({path})")
            except OSError:
                os.unlink(path)          # leftover from a crash
            finally:
                probe.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)            # only our user may connect
        try:
            sock.bind(path)
        except OSError as e:
            raise RuntimeError(f"cannot create socket {path}: {e}")
        finally:
            os.umask(old)
        sock.listen(16)
        return sock

    def all_processes(self):
        return [p for group in self.programs.values() for p in group] + self.retiring

    # ============================================================ main loop
    def run(self):
        self.log.info("taskmaster started (pid %d) with %s", os.getpid(), self.config.path)
        now = time.monotonic()
        for name, prog in self.config.programs.items():
            self.programs[name] = [Process(prog, i, self.log) for i in range(prog.numprocs)]
            if prog.autostart:
                for p in self.programs[name]:
                    p.start(now, manual=True)

        while not self.finished:
            if self.reload_requested:
                self.reload_requested = False
                self.reload("SIGHUP")
            if self.shutdown_requested and not self.shutting_down:
                self.shutdown(None)
            self.poll_io(0.1)
            self.reap_children()
            now = time.monotonic()
            for p in self.all_processes():
                p.tick(now)
            self.retiring = [p for p in self.retiring if p.state in ALIVE]
            self.run_waiters(now)
        self.cleanup()

    def poll_io(self, timeout):
        """select(): sleep until something is readable (max `timeout`)."""
        pipes = {fd: p for p in self.all_processes() for fd in p.streams}
        watched = [self.server] + list(self.clients) + list(pipes)
        readable, _, _ = select.select(watched, [], [], timeout)
        for r in readable:
            if r is self.server:
                sock, _ = self.server.accept()
                sock.settimeout(2)     # a stuck shell can't freeze taskmaster
                self.clients[sock] = Client(sock)
            elif r in self.clients:
                self.read_client(self.clients[r])
            else:
                proc = pipes[r]
                stream, data = proc.read(r)
                if data:
                    for c in list(self.clients.values()):
                        if c.attached is proc:
                            self.send(c, {"stream": stream, "out": data.decode(errors="replace")})

    def reap_children(self):
        """waitpid(-1, WNOHANG): collect every dead child without blocking.
        This is how we ALWAYS know exactly which process is alive or dead
        (and it removes the zombies)."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:      # we have no children at all
                return
            if pid == 0:                   # children exist, none has died
                return
            for p in self.all_processes():
                if p.pid == pid:
                    p.exited(status, time.monotonic())
                    break

    def when(self, condition, action, timeout):
        """Run action() as soon as condition() is true (or after timeout seconds).
        Used to answer "stop" only once processes are really dead, etc."""
        self.waiters.append((condition, action, time.monotonic() + timeout))

    def run_waiters(self, now):
        waiting, self.waiters = self.waiters, []
        for condition, action, deadline in waiting:
            if condition() or now >= deadline:
                action()
            else:
                self.waiters.append((condition, action, deadline))

    def cleanup(self):
        for c in list(self.clients.values()):
            c.sock.close()
        self.server.close()
        try:
            os.unlink(self.config.settings.socket)
        except OSError:
            pass
        self.log.info("taskmaster stopped")

    # ============================================================= clients
    def send(self, client, obj):
        try:
            client.send(obj)
        except OSError:
            self.drop(client)

    def reply(self, client, text):
        if client is not None:
            self.send(client, {"output": text})

    def drop(self, client):
        self.clients.pop(client.sock, None)
        client.sock.close()

    def read_client(self, client):
        try:
            data = client.sock.recv(65536)
        except OSError:
            data = b""
        if not data:
            self.drop(client)
            return
        client.buffer += data
        while b"\n" in client.buffer:
            line, client.buffer = client.buffer.split(b"\n", 1)
            try:
                req = json.loads(line)
                cmd, args = str(req["cmd"]), [str(a) for a in req.get("args", [])]
            except (ValueError, KeyError, TypeError):
                self.reply(client, "ERROR: malformed request")
                continue
            self.handle(client, cmd, args)

    def handle(self, client, cmd, args):
        commands = {
            "status": self.cmd_status, "start": self.cmd_start, "stop": self.cmd_stop,
            "restart": self.cmd_restart, "reload": self.cmd_reload, "shutdown": self.cmd_shutdown,
            "config": self.cmd_config, "tail": self.cmd_tail,
            "pid": self.cmd_pid, "names": self.cmd_names,
            "attach": self.cmd_attach, "input": self.cmd_input, "detach": self.cmd_detach,
        }
        if cmd not in commands:
            self.reply(client, f"*** Unknown command: {cmd}")
            return
        try:
            answer = commands[cmd](client, args)
        except Exception as e:          # a bad command must never kill taskmaster
            self.log.error("command '%s %s' failed: %r", cmd, " ".join(args), e)
            answer = f"ERROR: {e}"
        if answer is not None:          # None = the answer will be sent later
            self.reply(client, answer)

    # ======================================================== name lookup
    def resolve(self, names):
        """'all' | 'prog' | 'prog:*' | 'prog:2'  ->  (processes, error lines)"""
        if not names:
            return [], ["ERROR: give a process name or 'all'"]
        found, errors = [], []
        for n in names:
            group, _, index = n.partition(":")
            if n == "all":
                found += [p for g in self.programs.values() for p in g]
            elif group not in self.programs:
                errors.append(f"{n}: ERROR (no such process)")
            elif index in ("", "*"):
                found += self.programs[group]
            else:
                match = [p for p in self.programs[group] if p.name == n]
                found += match
                if not match:
                    errors.append(f"{n}: ERROR (no such process)")
        return list(dict.fromkeys(found)), errors   # removes duplicates, keeps the order

    # ============================================================ commands
    def cmd_status(self, client, args):
        procs, errors = self.resolve(args) if args else (self.all_processes(), [])
        now = time.monotonic()
        return "\n".join(errors + [p.status_line(now) for p in procs]) or "no programs"

    def cmd_start(self, client, args):
        procs, out = self.resolve(args)
        self.start_and_reply(client, procs, out)

    def start_and_reply(self, client, procs, out):
        now = time.monotonic()
        started = []
        for p in procs:
            if p.state in (STARTING, RUNNING, STOPPING):
                out.append(f"{p.name}: ERROR (already {p.state.lower()})")
            else:
                p.start(now, manual=True)
                started.append(p)

        def answer():
            for p in started:
                ok = p.state == RUNNING
                out.append(f"{p.name}: started" if ok else f"{p.name}: ERROR ({p.state}: {p.info})")
            self.reply(client, "\n".join(out))
        wait = max([p.program.starttime for p in started], default=0) + 1
        self.when(lambda: all(p.state != STARTING for p in started), answer, wait)

    def stop_all(self, procs, out):
        """Send stopsignal to procs, returns the ones we must wait for."""
        now = time.monotonic()
        stopping = []
        for p in procs:
            if p.stop(now):
                stopping.append(p)
            else:
                out.append(f"{p.name}: ERROR (not running)")
        return stopping

    def cmd_stop(self, client, args):
        procs, out = self.resolve(args)
        stopping = self.stop_all(procs, out)

        def answer():
            out.extend(f"{p.name}: stopped" for p in stopping)
            self.reply(client, "\n".join(out))
        wait = max([p.program.stoptime for p in stopping], default=0) + 2
        self.when(lambda: all(p.state not in ALIVE for p in stopping), answer, wait)

    def cmd_restart(self, client, args):
        procs, out = self.resolve(args)
        stopping = self.stop_all(procs, [])
        wait = max([p.program.stoptime for p in stopping], default=0) + 2
        self.when(lambda: all(p.state not in ALIVE for p in stopping),
                  lambda: self.start_and_reply(client, procs, out), wait)

    def cmd_reload(self, client, args):
        return self.reload("shell command")

    def reload(self, source):
        """Re-read the config file and change ONLY what changed."""
        self.log.info("reload requested (%s)", source)
        try:
            new = load_config(self.config.path)
        except ConfigError as e:
            self.log.error("reload failed, keeping the current configuration: %s", e)
            return f"ERROR: {e}\nconfiguration NOT changed"
        old_progs, new_progs = self.config.programs, new.programs
        now = time.monotonic()
        report = []
        for name in list(old_progs):
            old, prog = old_progs[name], new_progs.get(name)
            if prog == old:
                continue                                    # unchanged: don't touch
            if prog is None:                                # removed
                self.retire(self.programs.pop(name))
                report.append(f"{name}: removed")
            elif old.same_except_numprocs(prog):            # only numprocs changed
                group = self.programs[name]
                self.retire(group[prog.numprocs:])
                keep = group[:prog.numprocs]
                for p in keep:
                    p.program = prog
                extra = [Process(prog, i, self.log) for i in range(len(keep), prog.numprocs)]
                for p in extra:
                    if prog.autostart:
                        p.start(now, manual=True)
                self.programs[name] = keep + extra
                report.append(f"{name}: numprocs {old.numprocs} -> {prog.numprocs}")
            else:                                           # other option changed
                old_group = self.programs[name]
                self.retire(old_group)
                self.add_program(prog, after=old_group)
                report.append(f"{name}: changed, restarted")
        for name, prog in new_progs.items():
            if name not in old_progs:                       # added
                self.add_program(prog, after=[])
                report.append(f"{name}: added")
        unchanged = [n for n in new_progs if n in old_progs and new_progs[n] == old_progs[n]]
        if unchanged:
            report.append(f"unchanged, not restarted: {', '.join(unchanged)}")
        if new.settings.logfile != self.config.settings.logfile or new.settings.syslog != self.config.settings.syslog:
            self.log.configure(new.settings)
        self.log.settings = new.settings
        self.config = new
        self.log.info("configuration reloaded: %s", "; ".join(report) or "no changes")
        return "\n".join(report) or "no changes"

    def retire(self, procs):
        now = time.monotonic()
        for p in procs:
            p.stop(now)
            if p.state in ALIVE:
                self.retiring.append(p)   # keep watching it until it is dead

    def add_program(self, prog, after):
        """Create the processes of a program; start them once `after` are dead."""
        group = [Process(prog, i, self.log) for i in range(prog.numprocs)]
        self.programs[prog.name] = group
        if prog.autostart:
            def start():
                now = time.monotonic()
                for p in group:
                    if p.state not in ALIVE:
                        p.start(now, manual=True)
            self.when(lambda: all(p.state not in ALIVE for p in after), start,
                      max([p.program.stoptime for p in after], default=0) + 2)

    def cmd_shutdown(self, client, args):
        if self.shutting_down:
            return "already shutting down"
        self.shutdown(client)

    def shutdown(self, client):
        self.log.info("shutdown: stopping every program")
        self.shutting_down = True
        everything = self.all_processes()
        self.stop_all(everything, [])

        def finish():
            self.reply(client, "taskmaster: all programs stopped, bye")
            self.finished = True
        self.when(lambda: all(p.state not in ALIVE for p in everything), finish,
                  max([p.program.stoptime for p in everything], default=0) + 5)

    def cmd_config(self, client, args):
        if not args:
            return f"config file: {self.config.path}\n" + "\n".join(
                f"  {k}: {v}" for k, v in vars(self.config.settings).items())
        out = []
        for n in args:
            prog = self.config.programs.get(n.partition(":")[0])
            if prog is None:
                out.append(f"{n}: ERROR (no such program)")
            else:
                out.append(f"[{prog.name}]")
                for key, value in vars(prog).items():
                    if key == "umask" and value is not None:
                        value = f"{value:03o}"
                    if key != "name":
                        out.append(f"  {key:<13}{list(value) if key == 'exitcodes' else value}")
        return "\n".join(out)

    def find_one(self, args):
        procs, errors = self.resolve(args[:1])
        if errors:
            return None, errors[0]
        if len(procs) != 1:
            return None, f"choose one of: {', '.join(p.name for p in procs)}"
        return procs[0], None

    def cmd_tail(self, client, args):
        proc, err = self.find_one(args)
        if err:
            return err
        stream = args[1] if len(args) > 1 else "stdout"
        if stream not in proc.tails:
            return "stream must be stdout or stderr"
        return proc.tails[stream].decode(errors="replace") or f"(no {stream} output yet)"

    def cmd_pid(self, client, args):
        if not args:
            return str(os.getpid())
        procs, errors = self.resolve(args)
        return "\n".join(errors + [f"{p.name}: {p.pid}" for p in procs])

    def cmd_names(self, client, args):
        names = ["all"] + list(self.programs)
        names += [p.name for g in self.programs.values() for p in g if ":" in p.name]
        return " ".join(names)

    # --------------------------------------------------- attach (bonus)
    def cmd_attach(self, client, args):
        proc, err = self.find_one(args)
        if err:
            self.send(client, {"error": err})
            return None
        client.attached = proc
        self.log.info("console attached to %s", proc.name)
        self.send(client, {"attached": proc.name, "backlog": proc.tails["stdout"][-2048:].decode(errors="replace")})

    def cmd_input(self, client, args):
        if client.attached and args and not client.attached.write_stdin(args[0]):
            self.send(client, {"stream": "stderr", "out": "[taskmaster] process not running, input lost\n"})

    def cmd_detach(self, client, args):
        if client.attached:
            self.log.info("console detached from %s", client.attached.name)
        client.attached = None
        self.send(client, {"detached": True})
