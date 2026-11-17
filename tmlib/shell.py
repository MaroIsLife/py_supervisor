"""The control shell (like supervisorctl).

It is only a client: every command is sent to the engine through the UNIX
socket, and the answer is printed. Line editing, history (~/.taskmaster_history)
and TAB completion come from Python's cmd + readline modules.
"""

import atexit
import cmd
import json
import os
import select
import socket
import sys

try:
    import readline
except ImportError:
    readline = None

HISTORY_FILE = os.path.expanduser("~/.taskmaster_history")


class ConnectionLost(Exception):
    pass


class Connection:
    def __init__(self, path):
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buffer = b""

    def send(self, obj):
        try:
            self.sock.sendall((json.dumps(obj) + "\n").encode())
        except OSError as e:
            raise ConnectionLost(f"connection lost: {e}")

    def has_message(self):
        return b"\n" in self.buffer

    def receive_more(self):
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionLost("connection closed by taskmaster")
        self.buffer += data

    def recv(self):
        while not self.has_message():
            self.receive_more()
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def request(self, command, args=()):
        self.send({"cmd": command, "args": list(args)})
        return self.recv().get("output", "")

    def reconnect(self):
        self.sock.close()
        self.__init__(self.path)


def setup_readline(shell):
    if readline is None:
        return
    if "libedit" in (readline.__doc__ or ""):          # macOS
        readline.parse_and_bind("bind ^I rl_complete")
    else:                                               # Linux (GNU readline)
        readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(" \t\n")
    readline.set_completer(shell.complete)
    shell.completekey = None          # we did the TAB binding ourselves
    try:
        readline.read_history_file(HISTORY_FILE)
    except OSError:
        pass
    atexit.register(lambda: readline.write_history_file(HISTORY_FILE))


class Shell(cmd.Cmd):
    prompt = "taskmaster> "

    def __init__(self, conn, embedded=False):
        super().__init__()
        self.conn = conn
        self.embedded = embedded   # True = shell of ./taskmaster (quit also stops it)
        self.finished = False

    def run(self):
        setup_readline(self)
        print("taskmaster control shell. Type 'help' for the list of commands.")
        while not self.finished:
            try:
                self.cmdloop()
            except KeyboardInterrupt:           # Ctrl-C: new prompt, like bash
                print("^C")
                self.conn.reconnect()           # drop an answer we may still be waiting for
            except ConnectionLost as e:
                print(f"\n{e}")
                return

    def emptyline(self):
        pass

    def default(self, line):
        print(f"*** Unknown command: {line.split()[0]} (type 'help')")

    def send_command(self, name, arg):
        print(self.conn.request(name, arg.split()))

    def completedefault(self, text, line, begidx, endidx):
        names = self.conn.request("names").split()
        return [n for n in names if n.startswith(text)]

    def get_names(self):     # hide do_EOF (Ctrl-D) from "help" and TAB completion
        return [n for n in super().get_names() if n != "do_EOF"]

    # ----------------------------------------------------------- commands
    def do_status(self, arg):
        """status [name...]       state of every process (or of some)"""
        self.send_command("status", arg)

    def do_start(self, arg):
        """start <name|name:N|all>   start processes"""
        self.send_command("start", arg)

    def do_stop(self, arg):
        """stop <name|name:N|all>    stop processes (stopsignal, SIGKILL after stoptime)"""
        self.send_command("stop", arg)

    def do_restart(self, arg):
        """restart <name|name:N|all> stop then start processes"""
        self.send_command("restart", arg)

    def do_reload(self, arg):
        """reload                 re-read the config file (same as kill -HUP)"""
        self.send_command("reload", arg)

    def do_config(self, arg):
        """config [program]       show the options of a program"""
        self.send_command("config", arg)

    def do_tail(self, arg):
        """tail <name> [stdout|stderr]   last output of a process"""
        self.send_command("tail", arg)

    def do_pid(self, arg):
        """pid [name]             pid of taskmaster or of processes"""
        self.send_command("pid", arg)

    def do_shutdown(self, arg):
        """shutdown               stop every program and taskmaster itself"""
        self.finished = True
        print(self.conn.request("shutdown"))
        return True

    def do_quit(self, arg):
        """quit                   leave the shell (in ./taskmaster: also shutdown)"""
        if self.embedded:
            return self.do_shutdown(arg)
        self.finished = True
        return True

    do_exit = do_quit

    def do_EOF(self, arg):
        print()
        return self.do_quit(arg)

    def do_attach(self, arg):
        """attach <name>          bonus: see a process' output live and type into its stdin.
                       Ctrl-D (or Ctrl-C) detaches, the process keeps running."""
        self.conn.send({"cmd": "attach", "args": arg.split()})
        first = self.conn.recv()
        if "error" in first:
            print(first["error"])
            return
        print(f"--- attached to {first['attached']} (Ctrl-D to detach) ---")
        sys.stdout.write(first["backlog"])
        sys.stdout.flush()
        try:
            while True:
                while self.conn.has_message():           # print what the process wrote
                    msg = self.conn.recv()
                    out = sys.stderr if msg.get("stream") == "stderr" else sys.stdout
                    out.write(msg.get("out", ""))
                    out.flush()
                ready, _, _ = select.select([sys.stdin, self.conn.sock], [], [])
                if self.conn.sock in ready:
                    self.conn.receive_more()
                if sys.stdin in ready:
                    line = sys.stdin.readline()
                    if not line:                          # Ctrl-D
                        break
                    self.conn.send({"cmd": "input", "args": [line]})
        except KeyboardInterrupt:
            pass
        self.conn.send({"cmd": "detach"})
        while not self.conn.recv().get("detached"):      # skip output still in flight
            pass
        print(f"\n--- detached from {first['attached']} (it keeps running) ---")


def run_ctl(socket_path, argv):
    """taskmasterctl: interactive shell, or one command (taskmasterctl status)."""
    try:
        conn = Connection(socket_path)
    except OSError as e:
        print(f"taskmasterctl: cannot connect to {socket_path}: {e.strerror or e}"
              " (is taskmasterd running?)", file=sys.stderr)
        return 1
    shell = Shell(conn)
    try:
        if argv:
            shell.onecmd(" ".join(argv))
        else:
            shell.run()
    except ConnectionLost as e:
        print(e)
        return 1
    return 0
