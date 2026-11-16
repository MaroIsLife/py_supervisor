# Taskmaster

A job control daemon in Python 3, like `supervisor`.
Standard library only, plus PyYAML for the config file.

```bash
pip3 install -r requirements.txt
./taskmaster -c taskmaster.yaml       # foreground program + control shell (mandatory)
./taskmasterd -c taskmaster.yaml      # bonus: background daemon
./taskmasterctl                       # bonus: separate shell (or: ./taskmasterctl status)
                                      # -c taskmaster.yaml / -s <path> if the socket is not /tmp/taskmaster.sock
```

Shell commands: `status`, `start`, `stop`, `restart` (`name`, `name:N` or `all`), `reload`,
`shutdown`, `quit`, `config`, `tail`, `pid`, `attach`, `help`.

Hot reload: run the `reload` command, or `kill -HUP <pid>`.

**Read [GUIDE.md](GUIDE.md)**. It explains everything: fork/exec/waitpid, the signals, the main
loop, the state machine, step-by-step scenarios, every config option, and the defense checklist.

Authors: mougnou, omanar
