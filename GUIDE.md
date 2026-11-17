# Taskmaster: the complete guide

The guide is in this order:

0. The big picture
1. The Unix concepts you must know, and where each one appears in the code
2. The files
3. What happens when you type `./taskmaster`
4. The main loop
5. The life of a process (state machine)
6. Scenarios, step by step
7. Signals: who sends what, and where
8. The configuration file
9. Logging and alerts
10. Defense checklist
11. Questions the grader will probably ask
12. Shell commands, one by one

---

## 0. The big picture

Taskmaster is a babysitter for programs.
You give it a list of programs (the config file). It starts them, watches them every
0.1 second, and restarts them when they die (if the config says so). It writes
everything into a log file and gives you a shell to control it.

```
                    ┌───────────── taskmaster ───────────────┐
 you ── shell ──────►  socket  ──►  ENGINE (one loop)        │
                    │                │  fork/exec            │
                    │                ▼                       │
                    │     child programs (ticker, echo ...)  │
                    │        │ stdout/stderr through pipes   │
                    │        ▼                               │
                    │     log files / tail / attach          │
                    └────────────────────────────────────────┘
```

There are two parts, and only two:

* **The engine** (`tmlib/engine.py`) is one `while` loop. It owns every process.
  It is the only code that starts, stops or watches programs.
* **The shell** (`tmlib/shell.py`) reads your commands. It sends each one to the engine
  through a UNIX socket and prints the answer. It never touches a process itself.

The three executables just put these two parts together differently:

| executable | what it runs |
|---|---|
| `./taskmaster` (mandatory) | engine in a background thread + shell in your terminal, **one process** |
| `./taskmasterd` (bonus) | engine only, as a background daemon |
| `./taskmasterctl` (bonus) | shell only. It connects to the daemon's socket |

---

## 1. Unix concepts you must know

### 1.1 Process, PID, parent/child
A process is a running program with a number, its **PID**. Every process has a parent.
The programs that taskmaster starts are its **children**, and only a parent can learn how its
children died.

### 1.2 `fork()`
`fork()` **duplicates** the current process. After the call, two processes run the same code:

```python
pid = os.fork()
if pid == 0:
    # I am the CHILD (a copy of taskmaster)
else:
    # I am the PARENT (taskmaster); pid = the child's PID
```

**Where:** `Process.spawn()` in `tmlib/process.py`. This is the only place where a program is started.
`daemonize()` in `tmlib/main.py` also uses fork, but only to put taskmasterd in the background.

### 1.3 `exec()`
`execvpe(path, argv, env)` **replaces** the current process's code with another program.
The PID stays the same. If exec succeeds it never returns, because the old code is gone.

So "start a program" = **fork** (make a copy) + in the copy, **prepare** + **exec** (become the program).

**Where:** `Process._exec_child()`. Between fork and exec, the child prepares itself:

| step in `_exec_child` | why |
|---|---|
| `os.setsid()` | own session/process group, so Ctrl-C in your terminal doesn't kill it, and we can signal its whole group |
| `os.dup2(pipe, 0/1/2)` | its stdin/stdout/stderr become our pipes, so we can capture its output and write to its stdin |
| `signal.signal(SIGPIPE, SIG_DFL)` | Python ignores SIGPIPE, and an ignored signal stays ignored after exec, so we restore the default |
| `os.umask(...)` | config option `umask` |
| `os.chdir(...)` | config option `workingdir` |
| `setgid/setuid` | bonus option `user` (drop root privileges) |
| `os.execvpe(argv[0], argv, env)` | become the program, with config option `env` merged into the environment |
| `os._exit(127)` | only reached if exec failed. Exit **immediately** without going back into taskmaster's code |

The settings survive exec because umask, cwd, uid, open fds and the session are properties of the
**process**, not of the program. exec keeps them.

### 1.4 `waitpid()` and zombies
When a child dies, the kernel keeps a small record: its PID and exit status. The parent must
collect that record with `waitpid()`. Until the parent does, the dead child is a
**zombie** (`<defunct>` in `ps`).

```python
pid, status = os.waitpid(-1, os.WNOHANG)
#   -1        = any of my children
#   WNOHANG   = don't block: if nobody died, return (0, 0) right away
```

Decoding `status`:

```python
os.WIFEXITED(status)   -> it called exit()       -> os.WEXITSTATUS(status) = exit code (0..255)
os.WIFSIGNALED(status) -> it was killed by a signal -> os.WTERMSIG(status) = signal number
```

**Where:** `Engine.reap_children()` calls `waitpid(-1, WNOHANG)` in a loop, about 10 times per second.
For every dead PID it finds the `Process` and calls `Process.exited(status)`, which decodes the
status and decides what to do (restart, FATAL, ...).

This is the answer to the subject's "know at all times if processes are alive or dead (accurate)":
we don't guess with `ps` or `kill -0`. The kernel tells us through `waitpid`. And no zombies are left.

> Why not SIGCHLD? The kernel sends SIGCHLD to the parent when a child dies. We don't need it,
> because the loop already calls `waitpid(WNOHANG)` every 0.1 s, so a death is noticed within
> 0.1 s. That is simpler and impossible to miss.

### 1.5 Signals
A signal is a small message sent to a process (`kill -SIGNAL pid`). The ones that matter here:

| signal | number | meaning | can be caught? |
|---|---|---|---|
| SIGTERM | 15 | "please stop" (polite) | yes |
| SIGKILL | 9 | "die now" | **no**, that's why it is the last resort |
| SIGINT | 2 | Ctrl-C | yes |
| SIGHUP | 1 | historically "terminal hung up"; daemons use it as "reload your config" | yes |
| SIGUSR1 | 10/30 | free for programs to use | yes |
| SIGQUIT | 3 | Ctrl-\ | yes |
| SIGCHLD | 17/20 | "one of your children died" (we don't use it) | yes |
| SIGPIPE | 13 | "you wrote into a pipe nobody reads" | yes |

Section 7 lists every place taskmaster sends or receives one.

### 1.6 Process groups and sessions
`setsid()` puts the child in a new session and a new process group whose ID equals its PID. Two effects:
* Ctrl-C in your terminal sends SIGINT to the terminal's **foreground group**, which is taskmaster.
  Our children are in their own group, so they don't receive it.
* `os.killpg(pid, sig)` sends the signal to the **whole group**. If a supervised program
  started children of its own (for example `sh -c "..."`), they receive the stop signal too.

### 1.7 File descriptors and pipes
Every process has numbered "files": 0 = stdin, 1 = stdout, 2 = stderr.
`os.pipe()` returns `(read_end, write_end)`. Bytes written into one end come out of the other.
We create 3 pipes per process. The child keeps one end of each (as its fd 0/1/2) and the parent
keeps the other end:

```
 taskmaster                         child
 stdin_w  ───── pipe ─────►  fd 0 (stdin)
 out_r    ◄──── pipe ──────  fd 1 (stdout)
 err_r    ◄──── pipe ──────  fd 2 (stderr)
```

The engine reads `out_r`/`err_r` and writes the bytes into the files from the config (`stdout:` /
`stderr:`), or throws them away (`discard`). It also keeps the last 8 KB for `tail` and `attach`.
When the child dies, reading its pipe returns `b""` (end of file) and we close it.

**Why the pipes are non-blocking:** the engine is a single loop. If it got stuck reading one pipe,
it would stop watching every other process. `os.set_blocking(fd, False)` guarantees that never happens.

### 1.8 `select()`
`select(list_of_fds, [], [], 0.1)` sleeps until **one of the fds has data to read**, or 0.1 s has passed.
It lets one loop wait on everything at the same time: new shell connections, commands from
shells, and output from every program. **Where:** `Engine.poll_io()`.

### 1.9 umask
The umask removes permission bits from newly created files. A file is normally created `666` (rw-rw-rw-):
* `umask 022` gives `644` (rw-r--r--)
* `umask 077` gives `600` (rw-------)

It is inherited by the child and survives exec. That's why we set it between fork and exec.

### 1.10 UNIX socket
A socket is a file path (`/tmp/taskmaster.sock`) that two processes can use to talk to each other,
like a local network connection. The engine `listen()`s on it and each shell `connect()`s to it.
We make it `0700` with a umask, so only your user can control taskmaster.

### 1.11 Daemon (bonus)
A daemon is a program with no terminal that runs in the background. `daemonize()` does the classic
**double fork**:
1. `fork()`, and the parent exits. Your shell prompt comes back.
2. `setsid()`. The process is now in a new session with no terminal.
3. `fork()` again, and the parent exits. The remaining process is not a session leader, so it can never get a terminal again.
4. `chdir("/")` and point fd 0/1/2 to `/dev/null`.

---

## 2. The files

| file | lines | what it contains |
|---|---|---|
| `taskmaster`, `taskmasterd`, `taskmasterctl` | 7 each | launchers: they call `tmlib/main.py` |
| `Makefile` | | `make` creates a virtual environment `.venv` and installs PyYAML; `make run` / `daemon` / `ctl` start the programs with it |
| `bonus.yaml` | | config for `make bonus`: alerts (HTTP, e-mail, syslog) already enabled, plus `ticker`, `crasher` and `echo` to trigger them; the `user` bonus block is ready to uncomment |
| `tmlib/main.py` | ~135 | **where everything starts**: parse arguments, install signal handlers, start the engine and/or shell, daemonize |
| `tmlib/config.py` | ~170 | reads the YAML file, **checks** every option, fills defaults, returns `ProgramConfig` objects |
| `tmlib/process.py` | ~250 | **one process**: fork/exec, reading its output, its state machine |
| `tmlib/engine.py` | ~440 | **the loop**: select, waitpid, timers, every command, reload, attach |
| `tmlib/shell.py` | ~220 | the interactive shell (client of the socket) |
| `tmlib/logger.py` | ~65 | log file + bonus syslog/HTTP/e-mail alerts |
| `taskmaster.yaml` | | demo config: one program per grading item |
| `programs/*.py` | | small test programs (see section 10) |

---

## 3. What happens when you type `./taskmaster -c taskmaster.yaml`

```
./taskmaster                         (script) → tmlib.main.main_foreground()
│
├─ load("taskmaster.yaml")           config.load_config(): YAML → checked ProgramConfig objects
│                                    a bad config → error message, exit (nothing started)
├─ EventLogger.configure()           opens logs/taskmaster.log
├─ Engine(config, log)               creates /tmp/taskmaster.sock
│                                    (refuses if another taskmaster already uses it)
├─ signal.signal(SIGHUP,  on_sighup)   → sets engine.reload_requested = True
├─ signal.signal(SIGTERM, on_sigterm)  → sets engine.shutdown_requested = True
├─ Thread(engine.run).start()        ┐
│                                    │  engine.run():
│                                    │   - for each program with autostart: Process.start()
│                                    │       → fork + exec (section 1.3)
│                                    │   - then loops forever (section 4)
│                                    ┘
└─ Shell(Connection(socket)).run()   the prompt "taskmaster> " (main thread)
      you type "status" → sent to the engine over the socket → answer printed
```

When you type `quit` (or press Ctrl-D):
the shell sends `shutdown`. The engine stops every program and replies "all programs stopped",
then its loop ends. The main thread `join()`s the engine thread, and the process exits.

`./taskmasterd` is the same without the shell: `main_daemon()` → `daemonize()` → `engine.run()` in
the main thread.
`./taskmasterctl` is the shell only: `main_ctl()` → `run_ctl()` → `Shell(...).run()`.

---

## 4. The main loop (`Engine.run`)

```python
while not self.finished:
    if self.reload_requested:  reload("SIGHUP")         # 1. a signal arrived?
    if self.shutdown_requested: shutdown()
    self.poll_io(0.1)       # 2. select(): new shells, commands, program output (≤0.1s)
    self.reap_children()    # 3. waitpid(-1, WNOHANG): who died?  → Process.exited()
    for p in processes:     # 4. timers: starttime reached? stoptime over? backoff over?
        p.tick(now)
    self.run_waiters(now)   # 5. send the delayed answers ("ticker: stopped")
```

**Why one loop and no locks?** Only this loop changes processes, so two pieces of code can never
modify the same process at the same time. A signal handler only sets a flag
(`reload_requested = True`), which the loop reads at most 0.1 s later. Doing real work inside a
signal handler is dangerous, and this design avoids it.

**Delayed answers (`when`)**: `stop ticker` must reply only once ticker is really dead, but the
loop can't sit and wait. So the command registers "**when** no process is alive anymore, **then**
reply 'stopped'". Step 5 checks these conditions on every turn. The same mechanism is used for
`start` (reply after `starttime`), `restart` (start once stopped), reload (start the new version
once the old one is dead) and shutdown.

---

## 5. The life of a process (state machine)

```
            start                    alive ≥ starttime
 STOPPED ──────────► STARTING ───────────────────────► RUNNING
    ▲                  │  ▲                               │
    │       died early │  │ after 1s, 2s, 3s...           │ died by itself
    │                  ▼  │                               ▼
    │               BACKOFF                            EXITED ── autorestart says yes ──► STARTING
    │                  │ died early more than startretries times
    │                  ▼
    │                FATAL  (given up, alert sent)
    │
    └──────── STOPPING ◄──── stop (from STARTING or RUNNING): send stopsignal
     died          │
                   └── still alive after stoptime → SIGKILL (cannot be ignored)
```

| state | meaning | who moves it |
|---|---|---|
| STOPPED | not running | `Process.exited` (after STOPPING) or initial |
| STARTING | forked, waiting `starttime` | `Process.start` |
| RUNNING | survived `starttime` → **successfully started** | `Process.tick` |
| BACKOFF | died too early, will retry | `Process._failed_start` |
| FATAL | too many early deaths, gave up | `Process._failed_start` |
| STOPPING | stop signal sent, waiting | `Process.stop` |
| EXITED | died by itself while RUNNING | `Process.exited` |

"Expected" exit = an exit code listed in `exitcodes`. A death by a signal (kill -9...) is
**never** expected.

---

## 6. Scenarios, step by step (with file:line)

Every step names the function that runs, where it is, and what it does.
"Next loop turn" means the next pass of the main loop (`engine.py:87-99`), which repeats these steps:

1. check the signal flags (`engine.py:88-92`)
2. `poll_io` (`engine.py:93`)
3. `reap_children` (`engine.py:94`)
4. `tick` on every process (`engine.py:96-97`)
5. `run_waiters` (`engine.py:99`)

### A. Normal start (`ticker`, starttime 2, numprocs 2)

1. **`main_foreground`**, `main.py:46`: calls `load()`, which calls **`load_config`** (`config.py:153`).
   `load_config` turns the `ticker:` YAML block into a **`ProgramConfig`** (`config.py:28`)
   with `numprocs=2`, `starttime=2.0`, `umask=0o22`, `workingdir=/tmp`, `env={...}`.
2. **`Engine.run`**, `engine.py:81-82`: creates `Process(prog, 0)` and `Process(prog, 1)`.
   The **`name`** property (`process.py:42`) names them `ticker:0` and `ticker:1`.
3. **`Engine.run`**, `engine.py:83-85`: `autostart` is true, so it calls `p.start(now, manual=True)` on each.
4. **`Process.start`**, `process.py:148`: `manual=True` resets `retries` to 0 (`process.py:149-150`), then it calls `spawn()`.
5. **`Process.spawn`**, `process.py:48`:
   - `process.py:55-57`: opens `logs/ticker.stdout` and `logs/ticker.stderr` in append mode.
   - `process.py:62-63`: builds the environment: a copy of `os.environ` plus `STARTED_BY` and `ANSWER`.
   - `process.py:69-71`: creates 3 pipes with `os.pipe()`.
   - `process.py:73`: **`os.fork()`**. From here, two processes run this code.
6. **`Process._exec_child`**, `process.py:90`. This runs in the child (`pid == 0`, `process.py:74`):
   - `process.py:94`: `os.setsid()` gives the child its own session and process group.
   - `process.py:95-97`: `os.dup2` makes the pipes fd 0, 1 and 2.
   - `process.py:98`: restores SIGPIPE to its default action.
   - `process.py:100`: `os.umask(0o22)`.
   - `process.py:102`: `os.chdir("/tmp")`.
   - `process.py:110`: **`os.execvpe(...)`**. The child stops being taskmaster and becomes `ticker.py`.
7. **`Process.spawn`**, parent side:
   - `process.py:79-81`: closes the child's ends of the pipes.
   - `process.py:83`: makes our ends non-blocking.
   - `process.py:84-87`: stores the pid, the stdin pipe, and the stdout/stderr pipes (in `self.streams`).
8. **`Process.start`**, `process.py:154-156`: sets the state to **STARTING**, saves `start_time`,
   and logs `spawned: 'ticker:0' with pid ...`.
9. ticker prints "tick 0". On the next loop turn:
   - **`Engine.poll_io`**, `engine.py:104-106`: `select()` reports the stdout pipe as readable.
   - `engine.py:115-116` calls `proc.read(fd)`.
   - **`Process.read`**, `process.py:121`: `os.read` gets the bytes.
   - `process.py:132-134`: appends them to `logs/ticker.stdout`.
   - `process.py:135`: keeps the last 8 KB in memory for `tail`.
10. **`Process.tick`**, `process.py:204-205`: two seconds later, `now - start_time >= 2` is true,
    so it calls **`_now_running`** (`process.py:160`). That sets the state to **RUNNING** and logs `success: ...`.

### B. You kill it (`kill -9 <pid of ticker:0>`)

1. The kernel kills the process. Its end of the pipes is now closed.
2. **`Engine.poll_io`** sees the pipes as readable. **`Process.read`**, `process.py:121`, gets `b""`
   (end of file), so `process.py:126-131` close the fd and the log file and remove it from `streams`.
3. **`Engine.reap_children`**, `engine.py:128`: `os.waitpid(-1, os.WNOHANG)` returns `(pid, status)`.
   `engine.py:133-135` find the `Process` with that pid and call `p.exited(status, now)`.
4. **`Process.exited`**, `process.py:214`:
   - `process.py:216-218`: `os.WIFSIGNALED(status)` is true and `os.WTERMSIG(status)` is 9,
     so `how = "terminated by SIGKILL"` and `code = None`.
   - `process.py:222-225`: sets `pid = 0` and closes the stdin pipe.
   - The state was RUNNING, so the `else` branch at `process.py:233` runs.
5. `process.py:234`: `expected = code is not None and code in exitcodes` gives **False**.
   A death by signal is never expected.
6. `process.py:235-241`: sets the state to **EXITED** and logs `exited: ... not expected`.
   It also calls **`EventLogger.alert`** (`logger.py:39`), which sends the bonus HTTP and e-mail alerts.
7. `process.py:242-245`: `autorestart == "unexpected"` and the exit was not expected,
   so it logs `restarting:` and calls `start(now, manual=True)`. This is scenario A from step 4 again, with a new pid.

### C. A program that can only fail (`crasher`, startretries 3)

1. Started like scenario A. `crasher.py` writes to stderr and exits with code 1 after a few milliseconds.
   Its state is still **STARTING**.
2. **`Engine.reap_children`**, `engine.py:128`, collects the death. **`Process.exited`**,
   `process.py:219-221`, reads `code = os.WEXITSTATUS(status)`, which is 1.
3. `process.py:230-232`: the state is STARTING, so the process died before `starttime`.
   It logs `exited: ... not successfully started` and calls `_failed_start(now)`.
4. **`Process._failed_start`**, `process.py:165`:
   - `process.py:166`: `retries` becomes 1.
   - `process.py:167`: `1 > 3` is false.
   - `process.py:173-177`: sets the state to **BACKOFF** and `retry_time = now + 1`.
5. **`Process.tick`**, `process.py:211-212`: once `now >= retry_time`, it calls `start(now)`.
   This call is **without `manual=True`**, so `retries` is not reset (`process.py:149-150`).
6. It dies again: `retries` = 2, BACKOFF 2 s. Again: `retries` = 3, BACKOFF 3 s. Again: `retries` = 4.
7. `process.py:167-171`: `4 > 3`, so the state becomes **FATAL**. It logs `gave up: ...` and calls `alert("FATAL", ...)`.
   Nothing starts it again by itself; only a manual `start crasher` will.
8. **`Process.read`**, `process.py:132-133`, has written the error text from the stderr pipe into `logs/crasher.stderr`.

### D. Stopping a program that ignores SIGTERM (`stubborn`, stopsignal TERM, stoptime 3)

1. **`Shell.do_stop`**, `shell.py:129`, runs when you type `stop stubborn`. It calls
   **`send_command`** (`shell.py:110`), which calls **`Connection.request`** (`shell.py:56`).
   `request` sends `{"cmd":"stop","args":["stubborn"]}` and waits in **`recv`** (`shell.py:50`).
2. **`Engine.poll_io`**, `engine.py:112-113`: the client socket is readable.
   - **`read_client`**, `engine.py:176`: reads the line and parses the JSON (`engine.py:188`).
   - **`handle`**, `engine.py:195`: calls `cmd_stop` (`engine.py:207`).
3. **`Engine.cmd_stop`**, `engine.py:274`:
   - **`resolve`** (`engine.py:215`) turns `"stubborn"` into `[stubborn]`.
   - **`stop_all`** (`engine.py:263`) calls `p.stop(now)`.
4. **`Process.stop`**, `process.py:179`:
   - `process.py:186` calls **`send_signal`**, which runs `os.killpg(pid, SIGTERM)` at `process.py:195`.
   - `process.py:187-188`: sets the state to **STOPPING** and `kill_time = now + 3`.
5. `engine.py:282`: **`when`** stores the rule "once everything is dead, run `answer`".
   `cmd_stop` returns `None`, so **`handle`** sends nothing yet (`engine.py:211`). The shell keeps waiting.
6. `stubborn.py` catches SIGTERM and ignores it. **`run_waiters`**, `engine.py:143-149`,
   checks the condition on every loop turn and leaves the waiter in place while it is false.
7. **`Process.tick`**, `process.py:206-210`: after 3 s, `now >= kill_time` is true,
   so it logs `killing:` and calls `send_signal(SIGKILL)`. SIGKILL cannot be caught.
8. **`Engine.reap_children`** collects the death. **`Process.exited`**, `process.py:227-229`:
   the state was STOPPING, so it becomes **STOPPED** "(terminated by SIGKILL)".
9. **`run_waiters`** now finds the condition true and calls `answer()` (`engine.py:278-280`).
   **`reply`** (`engine.py:168`) sends `{"output":"stubborn: stopped"}`.
   On the shell side, `recv` returns and `send_command` prints the line (`shell.py:111`).

With `stopsignal: USR1`, step 4 sends SIGUSR1 instead. `stubborn.py` exits on SIGUSR1,
so step 8 happens about 0.1 s later and no SIGKILL is sent.

### E. A program that exits by itself (`exiter`: exits with code 2 after 4 s)

Everything happens in **`Process.exited`**, in the `else` branch (`process.py:233-245`), because the process was RUNNING:

- `process.py:220`: `code = os.WEXITSTATUS(status)`, which is 2.
- `process.py:234`: `expected = code in self.program.exitcodes`.
- `process.py:243`: restarts if `mode == "always" or (mode == "unexpected" and not expected)`.

| exitcodes | autorestart | `expected` (`process.py:234`) | restart? (`process.py:243`) | result |
|---|---|---|---|---|
| `[0]` | unexpected | False | True | `process.py:245` calls `start()`: restarted |
| `[0, 2]` | unexpected | True | False | stays **EXITED** |
| `[0, 2]` | always | True | True | restarted |
| `[0]` | never | False | False | stays **EXITED**; `process.py:240` logs "not expected" |

If it had died before `starttime`, the code would take `process.py:230` instead (scenario C)
and `exitcodes` would not matter.

### F. Hot reload (`kill -HUP <pid>` or `reload`)

1. **`on_sighup`**, `main.py:51-52` (`main.py:108-109` for the daemon): only sets
   `engine.reload_requested = True`. The handler is installed by `signal.signal(SIGHUP, ...)` at
   `main.py:59` (`main.py:114` for the daemon).
   The shell command `reload` goes through **`cmd_reload`** (`engine.py:291`) to the same `reload` function.
2. **`Engine.run`**, `engine.py:88-90`: on the next loop turn it resets the flag and calls `reload("SIGHUP")`.
3. **`Engine.reload`**, `engine.py:294`: calls `load_config` again (`engine.py:298`).
   If the file is invalid, `engine.py:299-301` log the error and return, and **nothing changes**.
4. `engine.py:307`: `prog == old` compares **every option**. The `==` comes from `@dataclass` (`config.py:28`).
   If the two are equal it moves on to the next program, and that program's processes keep their pid.
5. **Removed program**, `engine.py:309-311`: calls **`retire`** (`engine.py:343`), which stops each
   process and puts it in `self.retiring`. That list stays in **`all_processes`** (`engine.py:74-76`),
   so `waitpid` and `tick` keep watching it. `engine.py:98` forgets it once it is dead.
6. **Only `numprocs` changed**, `engine.py:312`, detected by **`same_except_numprocs`** (`config.py:55`):
   - `engine.py:314`: retires the extra instances.
   - `engine.py:315-317`: keeps the others running and gives them the new config.
   - `engine.py:318-321`: creates and starts the missing ones.
7. **Another option changed**, `engine.py:324-327`: retires the old group and calls
   **`add_program(prog, after=old_group)`** (`engine.py:350`). `add_program` creates the new processes,
   and `engine.py:360` uses `when` to start them only once the old ones are all dead.
8. **Added program**, `engine.py:329-332`: calls `add_program(prog, after=[])`, so it starts at once if `autostart` is set.
9. `engine.py:336-340`: reopens the log if `logfile` changed, stores the new config, and logs `configuration reloaded: ...`.

### G. The journey of a command: `stop ticker`

| step | function, file:line | what it does |
|---|---|---|
| 1 | **`Shell.run`** `shell.py:96` (`cmdloop`) | readline reads the line; `cmd.Cmd` calls `do_stop("ticker")` |
| 2 | **`Connection.request`** `shell.py:56-58` | writes `{"cmd":"stop","args":["ticker"]}\n` into the socket and waits in `recv` |
| 3 | **`Engine.poll_io`** `engine.py:106` | `select` reports the client socket as readable |
| 4 | **`Engine.read_client`** `engine.py:176-193` | `recv`, splits on `\n`, `json.loads`, then calls `handle` |
| 5 | **`Engine.handle`** `engine.py:195-212` | looks up `"stop"` in the `commands` table and calls `cmd_stop` |
| 6 | **`Engine.cmd_stop`** `engine.py:274-282` | `resolve` gives `[ticker:0, ticker:1]`; `stop_all` calls `Process.stop` on each (SIGTERM, STOPPING); `when(...)` |
| 7 | **`reap_children`** `engine.py:128` and **`Process.exited`** `process.py:227` | on later loop turns, both deaths are collected and the processes become STOPPED |
| 8 | **`Engine.run_waiters`** `engine.py:143-147` | the condition is true, so `answer()` runs and `reply` sends `{"output":"ticker:0: stopped\nticker:1: stopped"}` |
| 9 | **`Connection.recv`** `shell.py:50-54` and **`send_command`** `shell.py:111` | prints the answer; the prompt comes back |

### H. Attach (bonus): `attach echo`

1. **`Shell.do_attach`**, `shell.py:175`: sends `{"cmd":"attach","args":["echo"]}`.
2. **`Engine.cmd_attach`**, `engine.py:427-434`:
   - finds `echo` with **`find_one`**;
   - sets `client.attached = proc` (`engine.py:432`);
   - sends the last 2 KB of its output (`engine.py:434`).
3. **`Shell.do_attach`**, `shell.py:180-182`: prints that backlog.
   Then `shell.py:190` loops on `select([stdin, socket])`, waiting for either one.
4. **When echo prints:** **`Engine.poll_io`**, `engine.py:117-120`, sends `{"stream","out"}`
   to every client whose `attached` is this process. `shell.py:185-189` prints it.
5. **When you type a line:** `shell.py:194-197` sends `{"cmd":"input","args":[line]}`.
   **`Engine.cmd_input`** (`engine.py:436`) calls **`Process.write_stdin`** (`process.py:138-142`),
   which does `os.write(stdin_fd, ...)` into echo's stdin pipe.
6. **When you press Ctrl-D:** `readline()` returns `""` (`shell.py:195`) and the loop stops.
   - `shell.py:200` sends `detach`.
   - **`Engine.cmd_detach`** (`engine.py:440-444`) sets `attached = None` and replies `{"detached": true}`.
   - `shell.py:201-202` skip any output still in flight until that reply arrives.
   - echo keeps running.

### I. Shutdown (three ways)

**1. `quit`, `shutdown` or Ctrl-D in `./taskmaster`**

1. **`Shell.do_EOF`** (`shell.py:168`) and **`Shell.do_quit`** (`shell.py:159`, `embedded=True`)
   both call **`Shell.do_shutdown`** (`shell.py:153-157`), which sends `shutdown`.
2. **`Engine.cmd_shutdown`** (`engine.py:363`) calls **`Engine.shutdown`** (`engine.py:368-378`), which:
   - sets `shutting_down = True`;
   - calls `stop_all()` on every process;
   - registers `when(all dead, finish)`.
3. **`finish`**, `engine.py:374-376`: once everything is dead (scenario D, steps 7-8),
   replies "bye" and sets `finished = True`.
4. **`Engine.run`**: the loop condition at `engine.py:87` is now false, so `engine.py:100` calls **`cleanup`**
   (`engine.py:151-158`), which closes the sockets and deletes the socket file (`engine.py:156`).
5. **`main_foreground`**, `main.py:75-78`: the shell has returned. The `finally` block sets `exiting = True`
   and calls `thread.join()`, then the process exits.

**2. `kill <taskmaster pid>` (SIGTERM)**

1. **`on_sigterm`**, `main.py:54-57`: sets `engine.shutdown_requested = True` and raises `SystemExit`.
   That pulls the main thread out of the shell's `input()`.
2. **`main_foreground`**, `main.py:73-78`: prints "shutting down..." and calls `thread.join()`.
3. **`Engine.run`**, `engine.py:91-92`: sees the flag and calls `shutdown(None)`.
   The rest is the same as path 1, steps 2-4, without a client to reply to.

**3. `./taskmasterctl shutdown` while `./taskmaster` is open**

1. The engine side is the same as path 1.
2. **`engine_thread`**, `main.py:64-66`: when `engine.run()` returns, `exiting` is still False,
   so it sends SIGTERM to its own process with `os.kill(os.getpid(), SIGTERM)`.
3. That signal triggers path 2, step 1, and the shell leaves.

**For `taskmasterd`:** **`on_stop`**, `main.py:111-116`, handles SIGTERM, SIGINT and SIGQUIT and only sets the flag; then `engine.run()` returns and the program exits.

### J. A command that doesn't exist (`cmd: /nope/x`)

1. **`Process.spawn`**, `process.py:73`: `os.fork()` **succeeds**, because fork doesn't care what will be run.
2. **`Process._exec_child`**, `process.py:110`: `os.execvpe("/nope/x", ...)` raises `FileNotFoundError`.
3. `process.py:111-112`: catches the error and writes `taskmaster: cannot start '/nope/x': ...` to **fd 2**.
   Because of `dup2` at `process.py:97`, fd 2 is the stderr pipe.
4. `process.py:113`: `os._exit(127)` ends the child immediately, so it never runs taskmaster code.
5. **`Process.start`**, `process.py:154-156`: on the parent side, fork returned a pid, so it logs `spawned`
   and sets the state to STARTING. From the parent's point of view the start worked.
6. On the next loop turn, **`reap_children`** collects exit code 127 while the state is STARTING.
   From here it is exactly scenario C from step 3: BACKOFF, then **FATAL**.
7. **`Process.read`**, `process.py:132-133`: writes the error text into the `stderr:` file.
   `tail <name> stderr` also shows it, from memory.

### K. Ctrl-C in the shell

1. The terminal sends SIGINT to its foreground process group, which is taskmaster.
   Python raises `KeyboardInterrupt` in the main thread, where the shell runs.
2. **`Shell.run`**, `shell.py:97-99`: prints `^C` and calls **`Connection.reconnect`** (`shell.py:60`),
   which opens a new socket. If a command was still waiting for its answer, that answer goes to the old
   socket, so it can't be mixed up with the next command.
3. The engine thread and the supervised programs are not affected. Each program called `os.setsid()`
   (`process.py:94`), so it is in its own process group and never receives the terminal's SIGINT.

---

## 7. Signals: who sends what, and where

| signal | sent by | received by | code | effect |
|---|---|---|---|---|
| `stopsignal` (TERM by default) | engine | supervised program (whole group) | `Process.stop` → `send_signal` → `os.killpg` | graceful stop |
| SIGKILL | engine | supervised program | `Process.tick` (STOPPING and `now >= kill_time`) | forced stop after `stoptime` |
| SIGHUP | you (`kill -HUP`) | taskmaster | handler in `main.py` → `reload_requested` → `Engine.reload` | reload config |
| SIGTERM / SIGQUIT | you, or the system | taskmaster | handler in `main.py` → `shutdown_requested` | clean shutdown |
| SIGINT (Ctrl-C) | terminal | `./taskmaster`: the shell; `taskmasterd -n`: the daemon | `Shell.run` (KeyboardInterrupt) / `main_daemon` handler | new prompt / shutdown |
| SIGTERM | engine thread → own process | `./taskmaster` main thread | `engine_thread()` in `main.py` | leave the shell after a remote shutdown |
| SIGPIPE | — | — | `_exec_child` resets it to default | programs behave normally when a pipe closes |
| SIGCHLD | kernel | taskmaster | not used: `waitpid(WNOHANG)` polling | — |

Find the PID to send signals to:

```bash
./taskmasterctl pid
```

Or use `pid` in the shell, or `pgrep -f "taskmaster -c"`.

---

## 8. The configuration file

### 8.1 Format
YAML is plain-text nested dictionaries and lists. Indentation matters (use spaces, never tabs).
`key: value`, lists as `[a, b]` or `- a` lines. Strings may be quoted.

```yaml
taskmaster:              # 1. GLOBAL settings for taskmaster itself (optional)
  logfile: ./logs/taskmaster.log
  socket: /tmp/taskmaster.sock

programs:                # 2. one block per program to supervise (required)
  ticker:                #    ← the program's name (used in the shell: "stop ticker")
    cmd: "programs/ticker.py tick"
    numprocs: 2
    ...
```

### 8.2 How it is read (`tmlib/config.py`)
1. `yaml.safe_load()` turns the file into Python dicts and lists.
2. `load_config()` checks the top level: only `programs` and `taskmaster` are allowed.
3. `_parse_program()` runs for each program. It rejects unknown options, then builds
   `ProgramConfig(name=name, **options)`. Options missing from the file keep the default written in
   the dataclass. Then `check()` verifies each option (type and range) and turns relative paths into
   absolute ones.
4. `_parse_settings()` does the same for the `taskmaster:` section → `Settings`.
5. Any problem raises `ConfigError("program 'x': option 'numprocs': must be >= 1")`. At startup
   taskmaster refuses to run; during a reload the old config is kept.

Because `ProgramConfig` is a dataclass, `old == new` compares **every option**. That is how
reload knows what changed.

### 8.3 Every program option

| option | default | type | what it does | where it is used |
|---|---|---|---|---|
| `cmd` | **required** | string | command line, split like a shell would (`shlex`). **No shell is used**, so for pipes or `&&` write `sh -c '...'`. A relative path such as `programs/x.py` is relative to the config file | `execvpe` in `_exec_child` |
| `numprocs` | 1 | int ≥ 1 | how many copies. Names: `prog` if 1, else `prog:0`, `prog:1`... | `Engine.run`, `reload` |
| `autostart` | true | bool | start when taskmaster starts (and when added by reload) | `Engine.run`, `add_program` |
| `autorestart` | unexpected | always / never / unexpected | what to do when it dies **while RUNNING** | `Process.exited` |
| `exitcodes` | [0] | int or list | exit codes that count as "expected" | `Process.exited` |
| `starttime` | 1 | seconds | how long it must stay alive to count as successfully started (STARTING → RUNNING) | `Process.tick` |
| `startretries` | 3 | int | how many retries after dying during STARTING, before FATAL | `Process._failed_start` |
| `stopsignal` | TERM | name (TERM, INT, USR1, HUP, QUIT, `SIG` prefix allowed) | signal sent by `stop` | `Process.stop` |
| `stoptime` | 10 | seconds | wait after stopsignal before SIGKILL | `Process.tick` |
| `stdout` / `stderr` | discard | path or `discard` | file to append the output to, or throw it away | `Process.spawn` (open) / `Process.read` (write) |
| `env` | {} | mapping | variables **added** to taskmaster's environment | `Process.spawn` → `execvpe` |
| `workingdir` | inherit | path | `chdir` before exec | `_exec_child` |
| `umask` | inherit | octal | `umask` before exec | `_exec_child` |
| `user` (bonus) | none | user name | `setgid` + `setuid` before exec (taskmaster must run as root) | `_exec_child` |

### 8.4 Global options (`taskmaster:` section)

| option | default | meaning |
|---|---|---|
| `logfile` | /tmp/taskmaster.log | event log |
| `socket` | /tmp/taskmaster.sock | control socket (keep the path short: there is a ~100-character limit) |
| `syslog` | false | bonus: copy every event to syslog |
| `http_alert` | none | bonus: POST a JSON alert to this URL on FATAL or unexpected exits |
| `email_alert` | none | bonus: `{host, port, from, to}`, sends an e-mail for the same events |

### 8.5 YAML traps
* `umask: 022` → YAML reads a leading `0` as octal, which is what we want.
  Plain `umask: 22` would be decimal 22, so always write the leading 0.
* `autostart: yes`/`no` → YAML reads these as true/false, so they work.
  For `autorestart`, write the words `always`, `never` or `unexpected`.
* `env` values are turned into strings (`ANSWER: 42` → `"42"`).
* `stdout: discard` (or leaving the option out) throws the output away.

---

## 9. Logging and alerts (`tmlib/logger.py`)

Every event goes to the log file, with a timestamp:

```
spawned: 'ticker:0' with pid 1234
success: ticker:0 entered RUNNING state, process has stayed up for > than 2 seconds (starttime)
exited: ticker:0 (terminated by SIGKILL; not expected)
restarting: ticker:0 (autorestart=unexpected)
backoff: crasher will retry in 1s (1/3)
gave up: crasher entered FATAL state, too many start retries too quickly (3)
stopping: stubborn (pid 99) with SIGTERM, SIGKILL in 3s if still alive
killing: stubborn (pid 99) still alive after 3s, sending SIGKILL
stopped: stubborn (terminated by SIGKILL)
reload requested (SIGHUP) / configuration reloaded: ... / reload failed, keeping ...
console attached to echo / shutdown: stopping every program / taskmaster stopped
```

Bonus: `syslog: true` sends the same lines to the system log. **FATAL** and **unexpected exits** are
also sent to `http_alert` (JSON POST) and `email_alert` (SMTP). The sending happens in a small
background thread, so a slow server never freezes the loop.

---

## 10. Defense checklist (follows the grading scale)

Terminal A:

```bash
make            # first time only: creates .venv and installs PyYAML
make run        # same as ./taskmaster -c taskmaster.yaml, using the .venv Python
```

Terminal B:

```bash
tail -f logs/taskmaster.log
```

| item | what to show |
|---|---|
| Control shell | `help`, `status`, `stop ticker`, `start ticker`, `restart all`, TAB, ↑ history, Ctrl-C |
| Config file | `config ticker` → every parsed option. `status` → all programs from the file |
| Logging | terminal B while doing the rest |
| Hot reload | `status` (note the PIDs). Edit the file: ticker `numprocs: 3`, stubborn `stopsignal: USR1`. Run `kill -HUP $(pgrep -f "taskmaster -c")` and then `reload`. Only stubborn gets a new PID, ticker:2 appears, everything else keeps its PID |
| cmd | `config ticker`, `ps aux \| grep ticker` |
| numprocs | `status` → ticker:0, ticker:1 |
| autostart | exiter and flood: `STOPPED Not started` |
| autorestart / exitcodes | scenario E with `exiter` (`start exiter`, edit, `reload`, `start exiter`) |
| starttime | `restart ticker:0`, `status`: STARTING for 2 s, then RUNNING |
| startretries | crasher → BACKOFF 1/3, 2/3, 3/3 → FATAL. `cat logs/crasher.stderr` |
| stopsignal / stoptime | `stop stubborn` takes 3 s + "killing" in the log. With `USR1` + reload it is instant |
| stdout/stderr | `cat logs/ticker.stdout`, `logs/crasher.stderr`. showenv's stderr is discarded |
| env / workingdir / umask | `cat logs/showenv.stdout` and `ls -l /tmp/show_env_created_file` → `-rw-------` |
| Tests | `kill -9` a ticker PID → restarted. crasher → FATAL. `start flood` (huge output, stays fast). Try to break it |
| Bonus client/server | `make daemon`, then `make ctl` (or `./taskmasterctl status`), then `shutdown` |
| Bonus attach (2 pts) | `attach echo`, type `hi` → `ECHO: HI`, Ctrl-D. `status echo` shows the same PID |
| Bonus alerts | `make bonus`: it starts the HTTP and e-mail receivers, the daemon with `bonus.yaml` (alerts + syslog already on) and the shell. In another terminal: `tail -f logs/webhook.log logs/smtp.log`. In the shell: `start crasher` → FATAL → the alert appears in both logs. `kill -9` a ticker → `UNEXPECTED_EXIT` alert. `make bonus-stop` stops everything |
| Bonus user | uncomment `asnobody` in `bonus.yaml`, run `sudo .venv/bin/python ./taskmaster -c bonus.yaml`, then `tail asnobody` → `uid=...(nobody)` |

Test programs in `programs/`:
* `ticker.py`: prints forever.
* `crasher.py`: fails at once.
* `exit_with.py CODE DELAY`: exits with CODE after DELAY seconds.
* `stubborn.py`: ignores TERM, exits on USR1.
* `show_env.py`: prints its env, cwd and umask.
* `flood.py`: huge output.
* `echo_repl.py`: echoes stdin in upper case.
* `webhook_server.py` / `fake_smtp.py`: receive the alerts.

---

## 11. Questions the grader will probably ask

**How do you know a process is dead?** `waitpid(-1, WNOHANG)` in every loop turn (0.1 s).
The kernel gives us the exact PID and status. That is also what removes the zombies.

**What's the difference between fork and exec?** fork copies the process. exec replaces its
program while keeping the PID, fds, cwd, umask and uid. We set those up between the two.

**Why `os._exit` and not `sys.exit` in the child?** `sys.exit` raises an exception that would run
taskmaster's code (finally blocks, the loop...) inside the child. `_exit` stops the child immediately.

**What does "successfully started" mean?** The process stayed alive `starttime` seconds (STARTING → RUNNING).
A death before that is a failed start and counts toward `startretries`.

**Difference between EXITED and FATAL?** EXITED = it ran correctly, then exited. FATAL = it never
managed to start (it died before `starttime` more than `startretries` times).

**Why can't you ignore SIGKILL?** The kernel handles it directly; the program never sees it.
That is why it is used after `stoptime`.

**Why doesn't Ctrl-C kill the programs?** `setsid()` puts them in their own session and process group,
and the terminal only signals its foreground group.

**Why doesn't reload restart unchanged programs?** Every program's old and new `ProgramConfig` are
compared field by field. Equal → the code doesn't touch it.

**What if the new config is broken?** `ConfigError`: logged and shown, and the old config keeps running.

**Why threads/locks? / Why no locks?** One loop owns all the state. Signal handlers and shells only
send requests (a flag or a socket message), so nothing is ever modified from two places at once.

**Which external libraries?** Only PyYAML, for the config, which the subject allows. Everything else is the
Python standard library (`os`, `signal`, `select`, `socket`, `cmd`, `readline`, `logging`...).

---

## 12. Shell commands, one by one

### 12.1 How every command travels

All commands take the same path, except `help` and `quit`/`exit` in `taskmasterctl`, which never leave the shell.

1. **`Shell.run`** (`shell.py:91`) shows the `taskmaster>` prompt. `cmd.Cmd` reads the line with
   readline, which gives you line editing, ↑/↓ history and TAB completion. It then calls the method
   named `do_<first word>`, for example `do_stop` for `stop ticker`.
2. **`send_command`** (`shell.py:110`) splits the rest of the line into arguments and calls
   **`Connection.request`** (`shell.py:56`).
3. **`Connection.request`** writes one JSON line to the socket, for example
   `{"cmd": "stop", "args": ["ticker"]}`, then waits for one JSON line back: `{"output": "..."}`.
4. On the engine side, **`poll_io`** (`engine.py:102`) sees the socket is readable. **`read_client`**
   (`engine.py:176`) reads the line and **`handle`** (`engine.py:195`) calls the matching `cmd_<name>`
   function.
5. A `cmd_` function either returns the answer text, which `handle` sends at once, or returns `None`
   and registers a **`when`** (`engine.py:138`). With `when`, the answer is sent later, once the
   processes have reached the expected state.
6. The shell prints the `output` text and shows the prompt again.

**Process names** (accepted by `status`, `start`, `stop`, `restart`, `pid`), handled by
**`resolve`** (`engine.py:215`):

| you type | means |
|---|---|
| `ticker` or `ticker:*` | every process of the program `ticker` |
| `ticker:1` | only instance 1 (programs with `numprocs` > 1) |
| `all` | every process of every program |
| `nope` | `nope: ERROR (no such process)` |

You can give several names at once: `stop ticker:0 echo`.


**Calls used by every command:**

| call | where | what it does here |
|---|---|---|
| `socket.socket(AF_UNIX)` + `connect()` | `shell.py:31-32` | the shell opens a connection to `/tmp/taskmaster.sock` (once, at startup) |
| `json.dumps` + `sock.sendall()` | `shell.py:37` | sends the command as one line |
| `sock.recv()` + `json.loads` | `shell.py:45`, `shell.py:54` | reads the answer |
| `select.select()` | `engine.py:106` | the engine sleeps until a socket or a pipe has data (max 0.1 s) |
| `server.accept()` | `engine.py:109` | the engine accepts a new shell connection |
| `sock.recv()` + `json.loads` | `engine.py:178`, `engine.py:188` | the engine reads the command |
| `json.dumps` + `sock.sendall()` | `engine.py:35` | the engine sends the answer |
| `os.waitpid(-1, WNOHANG)` | `engine.py:128` | runs on every loop turn, whatever the command: collects dead children |
| `time.monotonic()` | `engine.py:95` | the clock used for `starttime`, `stoptime`, backoff and uptime |

---

### 12.2 `status [name...]`

**What it does:** prints one line per process: name, state, and details.

```
ticker:0             RUNNING   pid 4242, uptime 0:01:13
crasher              FATAL     Exited too quickly (process log may have details)
exiter               EXITED    exit status 2 (expected)
```

**How it works:**
- **`do_status`** (`shell.py:121`) sends `status`.
- **`cmd_status`** (`engine.py:236`) runs:
  - with no name, it uses **`all_processes`** (`engine.py:74`), so the list includes processes still
    dying after a reload;
  - with names, it uses `resolve`.
- Each line is built by **`Process.status_line`** (`process.py:247`):
  - RUNNING shows the pid and uptime;
  - STARTING and STOPPING show the pid;
  - every other state shows `info`, the reason for the last change, such as
    `Stopped (terminated by SIGTERM)` or `exit status 2 (unexpected)`.
- It answers immediately.


**Calls used:** no system call. Only `time.monotonic()` (`engine.py:238`) to compute the uptime.
The state shown is accurate because `os.waitpid` (`engine.py:128`) updates it on every loop turn.

---

### 12.3 `start <name...>`

**What it does:** starts the processes and answers once each one is RUNNING or has failed.

```
taskmaster> start exiter
exiter: started
```

**How it works:**
- **`do_start`** (`shell.py:125`) sends the request; **`cmd_start`** (`engine.py:241`) calls
  **`start_and_reply`** (`engine.py:245`).
- Processes already STARTING, RUNNING or STOPPING are skipped:
  `exiter: ERROR (already running)`.
- Every other process gets `Process.start(now, manual=True)` (`process.py:148`), which forks and execs
  as in scenario A. `manual=True` resets the retry counter, so `start` can relaunch a FATAL process.
- A `when` waits until no process is STARTING anymore, for at most `starttime` + 1 seconds. That is
  why `start` takes about `starttime` seconds to answer. Then each process gets one line:
  - **RUNNING** → `name: started`;
  - **BACKOFF or FATAL** → `name: ERROR (BACKOFF: Exited too quickly ...)`. This means it died before
    `starttime`. It keeps retrying on its own, as in scenario C.


**Calls used** (in the order they run):

| call | where | why |
|---|---|---|
| `open(path, "ab")` | `process.py:56` | opens the `stdout`/`stderr` log files in append mode |
| `os.environ` copy + `dict.update` | `process.py:62-63` | builds the environment with the `env:` option |
| `os.pipe()` ×3 | `process.py:69-71` | stdin, stdout and stderr pipes |
| **`os.fork()`** | `process.py:73` | creates the child process |
| *child:* `os.setsid()` | `process.py:94` | new session and process group (Ctrl-C won't reach it, `killpg` can target it) |
| *child:* **`os.dup2()`** ×3 | `process.py:95-97` | the pipes become fd 0, 1 and 2 |
| *child:* `signal.signal(SIGPIPE, SIG_DFL)` | `process.py:98` | restores the default action that Python changed |
| *child:* `os.umask()` | `process.py:100` | `umask:` option |
| *child:* `os.chdir()` | `process.py:102` | `workingdir:` option |
| *child:* `os.setgroups/setgid/setuid` | `process.py:106-108` | bonus `user:` option |
| *child:* **`os.execvpe()`** | `process.py:110` | replaces the child with the program |
| *child, if exec failed:* `os.write(2, ...)` + `os._exit(127)` | `process.py:112-113` | error message to stderr, exit without running taskmaster code |
| *parent:* `os.close()` ×3 | `process.py:79-81` | closes the child's ends of the pipes |
| *parent:* `os.set_blocking(fd, False)` | `process.py:83` | the loop never blocks on a pipe |
| later: `select` + `os.read()` | `engine.py:106`, `process.py:121` | reads the program's output |
| later: `os.waitpid` + `os.WEXITSTATUS` | `engine.py:128`, `process.py:220` | detects a death before `starttime` (failed start) |

---

### 12.4 `stop <name...>`

**What it does:** stops the processes and answers only when they are really dead.

```
taskmaster> stop stubborn          (waits 3 s: stoptime)
stubborn: stopped
```

**How it works:**
- **`do_stop`** (`shell.py:129`) sends the request; **`cmd_stop`** (`engine.py:274`) calls
  **`stop_all`** (`engine.py:263`), which calls **`Process.stop`** (`process.py:179`) on each process.
- What `Process.stop` does depends on the state:
  - **STARTING or RUNNING:** it sends `stopsignal` to the process group with `killpg`, moves to
    STOPPING, and sets `kill_time = now + stoptime`.
  - **BACKOFF:** it cancels the pending retry and goes straight to STOPPED.
  - **any other state:** it answers `name: ERROR (not running)`.
- A `when` waits until none of these processes is alive, for at most `stoptime` + 2 seconds.
  Meanwhile, **`Process.tick`** (`process.py:202`) sends SIGKILL if `stoptime` passes.
- When **`waitpid`** reports a death, **`Process.exited`** marks the process STOPPED and the shell
  prints `name: stopped`.


**Calls used:**

| call | where | why |
|---|---|---|
| **`os.killpg(pid, stopsignal)`** | `process.py:195` | sends the stop signal to the whole process group |
| `os.kill(pid, sig)` | `process.py:198` | fallback if the group is already gone |
| `os.killpg(pid, SIGKILL)` | `process.py:209` → `process.py:195` | after `stoptime`, sent from `Process.tick` |
| **`os.waitpid(-1, WNOHANG)`** | `engine.py:128` | collects the dead process (no zombie) |
| `os.WIFSIGNALED` / `os.WTERMSIG` / `os.WEXITSTATUS` | `process.py:216-220` | decode how it died ("terminated by SIGTERM") |
| `os.close(stdin_fd)` | `process.py:224` | closes our end of its stdin pipe |
| `os.read()` returns `b""` → `os.close()` | `process.py:121`, `process.py:127` | its stdout/stderr pipes reach end of file and are closed |

---

### 12.5 `restart <name...>`

**What it does:** stops the processes, waits until they are dead, then starts them again.

```
taskmaster> restart ticker:1
ticker:1: started
```

**How it works:**
- **`cmd_restart`** (`engine.py:284`) calls `stop_all`, ignoring "not running" errors so that
  `restart` also works on stopped, EXITED or FATAL processes.
- It then registers a `when`: once they are all dead, it runs `start_and_reply`, which is the same
  logic as `start`.
- The answer is the one from `start`.


**Calls used:** all the calls of `stop` (`killpg`, `waitpid`…), then all the calls of `start`
(`pipe`, `fork`, `setsid`, `dup2`, `umask`, `chdir`, `execvpe`…).

---

### 12.6 `reload`

**What it does:** reads the config file again and applies only what changed. It is the same as
`kill -HUP <taskmaster pid>`.

```
taskmaster> reload
ticker: numprocs 2 -> 3
stubborn: changed, restarted
flood: removed
newprog: added
unchanged, not restarted: crasher, exiter, showenv, echo
```

**How it works:**
- **`do_reload`** (`shell.py:137`) sends the request; **`cmd_reload`** (`engine.py:291`) calls
  **`reload`** (`engine.py:294`). This is the same function the SIGHUP handler triggers.
- `reload` compares each program's old and new `ProgramConfig` (scenario F), then:
  - stops removed or changed programs;
  - starts added programs;
  - leaves identical programs alone.
- It answers immediately with the report. A changed program starts its new version on its own once
  its old processes are dead.
- If the new file is invalid, the answer is `ERROR: ...` and `configuration NOT changed`. The running
  config stays as it was.


**Calls used:**

| call | where | why |
|---|---|---|
| `signal.signal(SIGHUP, on_sighup)` | `main.py:59` (`main.py:114` daemon) | for the `kill -HUP` path: the handler only sets a flag |
| `open()` + **`yaml.safe_load()`** | `config.py:157` | reads the file again |
| `shlex.split()` | `config.py:49` | splits `cmd` into argv like a shell would (checks it parses) |
| `pwd.getpwnam()` | `config.py:127` | checks that the `user:` exists |
| dataclass `==` | `engine.py:307` | compares the old and new config of each program |
| `killpg` / `waitpid` and `fork` / `execvpe` | as in `stop` and `start` | only for programs that changed, were added or were removed |

---

### 12.7 `config [program]`

**What it does:** shows the configuration as taskmaster understood it, after defaults were filled in
and relative paths turned into absolute ones. Use it in the defense to show the grader each option.

```
taskmaster> config showenv
[showenv]
  cmd          /…/programs/show_env.py
  numprocs     1
  umask        077
  workingdir   /tmp
  ...
```

**How it works:**
- **`cmd_config`** (`engine.py:380`) reads the stored `ProgramConfig` with `vars(prog)` and prints
  each field. `umask` is shown in octal.
- With no argument, it prints the config file path and the global `taskmaster:` settings: log file,
  socket and alerts.
- It never re-reads the file. It shows what is **loaded**, which is what matters after a `reload`.


**Calls used:** no system call. `vars(prog)` reads the fields of the stored `ProgramConfig`
(`engine.py:380`), and an f-string with `:03o` prints the umask in octal.

---

### 12.8 `tail <name> [stdout|stderr]`

**What it does:** shows the most recent output of one process. `stdout` is the default.

```
taskmaster> tail ticker:0
[tick] tick 41
[tick] tick 42
```

**How it works:**
- Each time the engine reads a process's pipe, **`Process.read`** (`process.py:116`) appends the
  bytes to the log file (if one is set) and keeps the **last 8 KB in memory** in `tails[stream]`
  (`process.py:135`).
- **`cmd_tail`** (`engine.py:406`) finds the process with **`find_one`** (`engine.py:398`), which
  needs a single process: `tail ticker` asks you to choose `ticker:0` or `ticker:1`. It then returns
  that memory buffer.
- Because the buffer is in memory, `tail` works even with `stdout: discard`.


**Calls used:** none at the moment you type it, because it reads the in-memory buffer. The bytes were
read earlier by **`os.read()`** (`process.py:121`), when `select` reported the pipe as readable.

---

### 12.9 `pid [name...]`

**What it does:**
- `pid` alone prints taskmaster's own PID. Use it with `kill -HUP` or `kill`.
- `pid ticker` prints each process's PID. The PID is 0 when the process is not running.

```
taskmaster> pid ticker
ticker:0: 4242
ticker:1: 4243
```

**How it works:** **`cmd_pid`** (`engine.py:415`) returns `os.getpid()`, or `Process.pid` for each
process returned by `resolve`. From another terminal: `kill -9 $(./taskmasterctl pid ticker:0 | awk '{print $2}')`.


**Calls used:** `os.getpid()` (`engine.py:417`) for taskmaster's own PID. A program's PID is the value
**`os.fork()`** returned in the parent (`process.py:73`), stored in `Process.pid`.

---

### 12.10 `attach <name>` (bonus)

**What it does:** connects your terminal to a running process:
- you see what it prints, live;
- each line you type is written to its stdin;
- **Ctrl-D** (or Ctrl-C) detaches you, and the process keeps running.

```
taskmaster> attach echo
--- attached to echo (Ctrl-D to detach) ---
hello
ECHO: HELLO
^D
--- detached from echo (it keeps running) ---
```

**How it works:**
- **`do_attach`** (`shell.py:172`) sends `attach`. **`cmd_attach`** (`engine.py:427`) sets
  `client.attached = process` and sends back the last 2 KB of its output.
- After that, every time **`poll_io`** reads this process's output, it forwards the output to every
  attached client.
- The shell uses `select` to wait on your keyboard and the socket at the same time (`shell.py:190`):
  - output that arrives is printed;
  - a line you type is sent as `{"cmd": "input"}`. **`cmd_input`** (`engine.py:436`) calls
    **`Process.write_stdin`**, which writes it into the process's stdin pipe.
- Ctrl-D sends `detach`. **`cmd_detach`** (`engine.py:440`) sets `attached = None`.
- An attachment survives restarts of the process, because it is linked to the `Process` object, not
  to one PID.


**Calls used:**

| call | where | why |
|---|---|---|
| **`select.select([sys.stdin, sock])`** | `shell.py:190` | the shell waits on the keyboard and the socket at the same time |
| `sys.stdin.readline()` | `shell.py:194` | reads the line you typed (`""` means Ctrl-D) |
| `sock.sendall()` / `sock.recv()` | `shell.py:37`, `shell.py:45` | sends your input and receives the output |
| `os.read(pipe)` | `process.py:121` | the engine reads what the program printed |
| `sock.sendall()` | `engine.py:35` | the engine forwards that output to the attached shell |
| **`os.write(stdin_fd, ...)`** | `process.py:142` | the engine writes your line into the program's stdin pipe |

---

### 12.11 `shutdown`

**What it does:** stops every program, then stops taskmaster itself. In `taskmasterctl`, this stops
the daemon.

```
taskmaster> shutdown
taskmaster: all programs stopped, bye
```

**How it works:**
- **`do_shutdown`** (`shell.py:153`) sends the request. **`cmd_shutdown`** (`engine.py:363`) calls
  **`shutdown`** (`engine.py:368`), which stops everything with `stop_all`.
- A `when` waits for all deaths; SIGKILL is sent after each program's `stoptime`.
- **`finish`** then answers and sets `finished = True`. The main loop ends and **`cleanup`** deletes
  the socket file.
- If you run it twice, the second time answers `already shutting down`.


**Calls used:**

| call | where | why |
|---|---|---|
| `os.killpg(stopsignal)`, then `SIGKILL` | `process.py:195`, `process.py:209` | stops every program, as `stop all` does |
| `os.waitpid(-1, WNOHANG)` | `engine.py:128` | waits until every program is collected |
| `sock.close()` + **`os.unlink(socket path)`** | `engine.py:152-156` | `cleanup`: closes the connections and deletes `/tmp/taskmaster.sock` |
| `os.kill(os.getpid(), SIGTERM)` | `main.py:66` | `./taskmaster` only, when the shutdown came from `taskmasterctl`: wakes up its own shell |
| `thread.join()` | `main.py:78` | `./taskmaster` waits for the engine thread before exiting |

---

### 12.12 `quit` / `exit` / Ctrl-D

**What it does:**
- **In `./taskmaster`:** the same as `shutdown`, because the program runs in the foreground, so
  leaving the shell means stopping taskmaster.
- **In `taskmasterctl`:** only leaves the shell. The daemon and its programs keep running.

**How it works:**
- **`do_quit`** (`shell.py:159`) checks `self.embedded`, which is True only for the shell started
  by `./taskmaster`:
  - if True, it calls `do_shutdown`;
  - otherwise it sets `finished = True` and returns.
- Ctrl-D at the prompt sends end-of-file, which `cmd.Cmd` turns into **`do_EOF`** (`shell.py:168`).
  That method calls `do_quit`.


**Calls used:** in `./taskmaster`, the same calls as `shutdown`. In `taskmasterctl`, nothing:
the shell returns and its socket is closed when the program exits. Ctrl-D is not a signal: it makes
readline return end-of-file, which `cmd.Cmd` turns into `do_EOF`.

---

### 12.13 `help [command]`

**What it does:**
- `help` lists the commands.
- `help stop` prints the one-line description of `stop`.

**How it works:** it is built into Python's `cmd.Cmd`. The text shown is the docstring (the first
`"""..."""` line) of each `do_` method in `shell.py`. It never contacts the engine.


**Calls used:** none. `cmd.Cmd` prints the docstrings; the engine is never contacted.

---

### 12.14 Keys and hidden commands

| key / command | effect | where |
|---|---|---|
| **TAB** | completes command names, then process names | `cmd.Cmd` completes command names from `get_names` (`shell.py:117`), which hides `do_EOF`; `completedefault` (`shell.py:113`) completes process names by asking the engine with the hidden command **`names`** (`engine.py:421`) |
| `help` | lists the documented commands. `EOF` (Ctrl-D) is hidden by `get_names` (`shell.py:117`), but Ctrl-D still quits | `cmd.Cmd.do_help` |
| **↑ / ↓** | browse command history, saved in `~/.taskmaster_history` | `setup_readline` (`shell.py:65`) |
| **Ctrl-C** | cancels the current line or wait and shows a new prompt. Taskmaster keeps running | `Shell.run` (`shell.py:97-99`), scenario K |
| unknown word | `*** Unknown command: foo (type 'help')` | `Shell.default` (`shell.py:107`) |
| `input`, `detach` | internal messages used only by `attach`; you never type them | `engine.py:436-444` |

**Calls used by the keys:**

| call | where | why |
|---|---|---|
| `readline.parse_and_bind()` | `shell.py:69`, `shell.py:71` | binds TAB to completion (`bind ^I rl_complete` on macOS's libedit, `tab: complete` on Linux) |
| `readline.set_completer()` | `shell.py:73` | readline asks `Shell.complete` for suggestions |
| `readline.read_history_file()` / `write_history_file()` | `shell.py:76`, `shell.py:79` | loads history at start and saves it at exit (`atexit`) |
| SIGINT (Ctrl-C) → `KeyboardInterrupt` | `shell.py:97` | Python's default SIGINT handler raises it in the main thread |
| `sock.close()` + `socket.socket()` + `connect()` | `shell.py:60` | `reconnect` after Ctrl-C |

---

### 12.15 Summary: every system call and where it is used

| call | file:line | used for | commands that trigger it |
|---|---|---|---|
| `os.fork()` | `process.py:73`, `main.py:85`, `main.py:88` | create a program's process / daemonize `taskmasterd` | `start`, `restart`, `reload`, autostart, autorestart |
| `os.execvpe()` | `process.py:110` | replace the child with the program | same as `fork` |
| `os.pipe()` | `process.py:69-71` | stdin/stdout/stderr pipes | same as `fork` |
| `os.dup2()` | `process.py:95-97`, `main.py:93` | pipes → fd 0/1/2; `/dev/null` → fd 0/1/2 for the daemon | same as `fork` |
| `os.setsid()` | `process.py:94`, `main.py:87` | own session/process group | same as `fork` |
| `os.umask()` | `process.py:100`, `engine.py:64` | `umask:` option; socket only usable by its owner | `start` / taskmaster startup |
| `os.chdir()` | `process.py:102`, `main.py:90` | `workingdir:` option; daemon moves to `/` | `start` / `taskmasterd` |
| `os.setgid()` / `os.setuid()` / `os.setgroups()` | `process.py:106-108` | bonus `user:` option | `start` |
| `os._exit()` | `process.py:113`, `main.py:86`, `main.py:89` | leave a child without running Python cleanup | failed exec, daemonize |
| `os.waitpid(-1, WNOHANG)` | `engine.py:128` | collect dead children, know who is alive | every loop turn |
| `os.WIFSIGNALED` / `WTERMSIG` / `WEXITSTATUS` | `process.py:216-220` | decode the exit status | every death |
| `os.killpg()` / `os.kill()` | `process.py:195`, `process.py:198`, `main.py:66` | send `stopsignal` / SIGKILL; wake the foreground shell | `stop`, `restart`, `reload`, `shutdown` |
| `signal.signal()` | `main.py:59-61`, `main.py:114-116`, `process.py:98` | SIGHUP → reload, SIGTERM/SIGQUIT/SIGINT → shutdown; reset SIGPIPE in the child | `kill -HUP`, `kill` |
| `os.read()` / `os.write()` | `process.py:121`, `process.py:142`, `process.py:112` | read program output; write to its stdin; error message in the child | output, `attach` |
| `os.close()` | `process.py:79-81`, `process.py:127`, `process.py:224` | close unused or finished pipe ends | `start`, deaths |
| `os.set_blocking()` | `process.py:83` | non-blocking pipes | `start` |
| `select.select()` | `engine.py:106`, `shell.py:190` | wait on many fds at once | every loop turn, `attach` |
| `socket` `bind` / `listen` / `accept` | `engine.py:63-71`, `engine.py:109` | the control socket (server side) | startup, each new shell |
| `socket` `connect` / `sendall` / `recv` | `shell.py:31-45`, `engine.py:35`, `engine.py:178` | shell ⇄ engine messages | every command |
| `os.unlink()` | `engine.py:60`, `engine.py:156` | remove a stale socket file / the socket at exit | startup, `shutdown` |
| `os.getpid()` | `engine.py:417`, `main.py:66` | taskmaster's own PID | `pid` |
| `yaml.safe_load()` | `config.py:157` | parse the config file | startup, `reload` |
| `threading.Thread` | `main.py:68`, `logger.py:45` | engine thread in `./taskmaster`; alert sending | startup, alerts |
