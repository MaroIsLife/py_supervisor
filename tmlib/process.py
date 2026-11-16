"""One supervised process: how it is started (fork + exec) and its states.

States (the same names supervisor uses):

  STOPPED  : not running (never started, or stopped by the user)
  STARTING : just forked, not yet alive for `starttime` seconds
  RUNNING  : alive for at least `starttime` seconds => "successfully started"
  BACKOFF  : died while STARTING, will be retried after a short delay
  FATAL    : died while STARTING more than `startretries` times => given up
  STOPPING : we sent `stopsignal`, waiting for it to die (SIGKILL after `stoptime`)
  EXITED   : died by itself while RUNNING
"""

import os
import pwd
import signal

STOPPED, STARTING, RUNNING, BACKOFF = "STOPPED", "STARTING", "RUNNING", "BACKOFF"
FATAL, STOPPING, EXITED = "FATAL", "STOPPING", "EXITED"
ALIVE = (STARTING, RUNNING, STOPPING)   # states where a real pid exists

TAIL_SIZE = 8192   # bytes of recent output kept in memory (for "tail" and "attach")


class Process:
    def __init__(self, program, index, log):
        self.program = program      # its ProgramConfig (see config.py)
        self.index = index          # 0..numprocs-1
        self.log = log
        self.state = STOPPED
        self.info = "Not started"   # text shown by "status"
        self.pid = 0
        self.retries = 0            # failed starts in a row
        self.start_time = 0.0       # when it was forked
        self.kill_time = 0.0        # when to send SIGKILL (while STOPPING)
        self.retry_time = 0.0       # when to retry (while BACKOFF)
        self.stdin_fd = None        # pipe to the child's stdin (for attach)
        self.streams = {}           # pipe fd -> ("stdout"/"stderr", log file or None)
        self.tails = {"stdout": b"", "stderr": b""}

    @property
    def name(self):
        if self.program.numprocs == 1:
            return self.program.name
        return f"{self.program.name}:{self.index}"

    # ------------------------------------------------------------ fork/exec
    def spawn(self):
        """Create the child process. Returns False if it could not be created."""
        prog = self.program
        if prog.user and os.geteuid() != 0 and pwd.getpwnam(prog.user).pw_uid != os.geteuid():
            self.info = f"taskmaster must be root to run as user '{prog.user}'"
            self.log.error("spawnerr: %s: %s", self.name, self.info)
            return False
        try:
            files = {s: open(getattr(prog, s), "ab") if getattr(prog, s) else None
                     for s in ("stdout", "stderr")}
        except OSError as e:
            self.info = f"cannot open log file: {e}"
            self.log.error("spawnerr: %s: %s", self.name, self.info)
            return False
        env = dict(os.environ)
        env.update(prog.env)
        if prog.user:
            pw = pwd.getpwnam(prog.user)
            env.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)

        # 3 pipes: we write in stdin_w, the child writes in out_w / err_w
        stdin_r, stdin_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()

        pid = os.fork()   # <-- from here on there are 2 processes running this code
        if pid == 0:
            # ======== CHILD ========
            self._exec_child(env, stdin_r, out_w, err_w)   # never returns

        # ======== PARENT (taskmaster) ========
        os.close(stdin_r)     # these ends belong to the child
        os.close(out_w)
        os.close(err_w)
        for fd in (stdin_w, out_r, err_r):
            os.set_blocking(fd, False)    # the main loop must never block on a pipe
        self.pid = pid
        self.stdin_fd = stdin_w
        self.streams[out_r] = ("stdout", files["stdout"])
        self.streams[err_r] = ("stderr", files["stderr"])
        return True

    def _exec_child(self, env, stdin_r, out_w, err_w):
        """Runs in the child, between fork and exec: prepare, then become the program."""
        prog = self.program
        try:
            os.setsid()               # new session + process group (Ctrl-C of the shell won't reach us)
            os.dup2(stdin_r, 0)       # pipes become fd 0, 1, 2
            os.dup2(out_w, 1)
            os.dup2(err_w, 2)
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # Python ignores SIGPIPE, programs expect default
            if prog.umask is not None:
                os.umask(prog.umask)
            if prog.workingdir:
                os.chdir(prog.workingdir)
            if prog.user:             # bonus: drop root privileges
                pw = pwd.getpwnam(prog.user)
                if os.getuid() == 0:
                    os.setgroups([])
                os.setgid(pw.pw_gid)
                os.setuid(pw.pw_uid)
            argv = prog.argv
            os.execvpe(argv[0], argv, env)    # replaces this process with the program
        except BaseException as e:            # exec failed (bad path, bad dir...)
            os.write(2, f"taskmaster: cannot start '{prog.cmd}': {e}\n".encode())
        os._exit(127)                         # never go back into taskmaster's code

    # --------------------------------------------------------------- output
    def read(self, fd):
        """Called by the main loop when a pipe of this process is readable.
        Returns (stream, data); data is b"" at end of file."""
        stream, logfile = self.streams[fd]
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return stream, None
        except OSError:
            data = b""
        if not data:                              # child closed it (it died)
            os.close(fd)
            if logfile:
                logfile.close()
            del self.streams[fd]
            return stream, b""
        if logfile:
            logfile.write(data)
            logfile.flush()
        self.tails[stream] = (self.tails[stream] + data)[-TAIL_SIZE:]
        return stream, data

    def write_stdin(self, text):
        if self.stdin_fd is None:
            return False
        try:
            os.write(self.stdin_fd, text.encode())
            return True
        except OSError:
            return False

    # -------------------------------------------------------- state machine
    def start(self, now, manual=False):
        if manual:
            self.retries = 0
        if not self.spawn():
            self._failed_start(now, self.info)
            return
        self.state, self.info = STARTING, ""
        self.start_time = now
        self.log.info("spawned: '%s' with pid %d", self.name, self.pid)
        if self.program.starttime == 0:
            self._now_running()

    def _now_running(self):
        self.state, self.retries = RUNNING, 0
        self.log.info("success: %s entered RUNNING state, process has stayed up for > than %g seconds (starttime)",
                      self.name, self.program.starttime)

    def _failed_start(self, now, reason="Exited too quickly (process log may have details)"):
        self.retries += 1
        if self.retries > self.program.startretries:
            self.state, self.info = FATAL, reason
            self.log.error("gave up: %s entered FATAL state, too many start retries too quickly (%d)",
                           self.name, self.program.startretries)
            self.log.alert("FATAL", self.name, self.info)
        else:
            self.state = BACKOFF
            self.info = f"{reason} - retry {self.retries}/{self.program.startretries}"
            self.retry_time = now + self.retries      # wait 1s, then 2s, then 3s...
            self.log.info("backoff: %s will retry in %ds (%d/%d)",
                          self.name, self.retries, self.retries, self.program.startretries)

    def stop(self, now):
        """Ask the process to stop. Returns False if it was not running."""
        if self.state == BACKOFF:
            self.state, self.info = STOPPED, "Stopped"
            return True
        if self.state not in (STARTING, RUNNING):
            return False
        self.send_signal(self.program.signum)
        self.state = STOPPING
        self.kill_time = now + self.program.stoptime
        self.log.info("stopping: %s (pid %d) with SIG%s, SIGKILL in %gs if still alive",
                      self.name, self.pid, self.program.stopsignal, self.program.stoptime)
        return True

    def send_signal(self, sig):
        try:
            os.killpg(self.pid, sig)       # the whole process group (program + its children)
        except OSError:
            try:
                os.kill(self.pid, sig)
            except OSError:
                pass

    def tick(self, now):
        """Called by the main loop ~10 times per second: handles the timers."""
        if self.state == STARTING and now - self.start_time >= self.program.starttime:
            self._now_running()
        elif self.state == STOPPING and now >= self.kill_time:
            self.log.warning("killing: %s (pid %d) still alive after %gs, sending SIGKILL",
                             self.name, self.pid, self.program.stoptime)
            self.send_signal(signal.SIGKILL)
            self.kill_time = now + 3600
        elif self.state == BACKOFF and now >= self.retry_time:
            self.start(now)

    def exited(self, status, now):
        """Called by the main loop when waitpid() says our pid died."""
        if os.WIFSIGNALED(status):
            code = None
            how = f"terminated by SIG{signal.Signals(os.WTERMSIG(status)).name[3:]}"
        else:
            code = os.WEXITSTATUS(status)
            how = f"exit status {code}"
        self.pid = 0
        if self.stdin_fd is not None:
            os.close(self.stdin_fd)
            self.stdin_fd = None

        if self.state == STOPPING:                    # we asked for it
            self.state, self.info = STOPPED, f"Stopped ({how})"
            self.log.info("stopped: %s (%s)", self.name, how)
        elif self.state == STARTING:                  # died before starttime
            self.log.warning("exited: %s (%s) before starttime, not successfully started", self.name, how)
            self._failed_start(now)
        else:                                         # RUNNING -> died by itself
            expected = code is not None and code in self.program.exitcodes
            self.state = EXITED
            self.info = f"{how} ({'expected' if expected else 'unexpected'})"
            if expected:
                self.log.info("exited: %s (%s; expected)", self.name, how)
            else:
                self.log.warning("exited: %s (%s; not expected)", self.name, how)
                self.log.alert("UNEXPECTED_EXIT", self.name, how)
            mode = self.program.autorestart
            if mode == "always" or (mode == "unexpected" and not expected):
                self.log.info("restarting: %s (autorestart=%s)", self.name, mode)
                self.start(now, manual=True)

    def status_line(self, now):
        if self.state == RUNNING:
            up = int(now - self.start_time)
            detail = f"pid {self.pid}, uptime {up // 3600}:{up // 60 % 60:02d}:{up % 60:02d}"
        elif self.state in ALIVE:
            detail = f"pid {self.pid}"
        else:
            detail = self.info
        return f"{self.name:<20} {self.state:<9} {detail}"
