# Taskmaster

A job control daemon in Python 3, like `supervisor`.
Standard library only, plus PyYAML for the config file.

```bash
make            # creates a virtual environment (.venv) and installs PyYAML in it
make run        # foreground taskmaster + control shell (mandatory part)
make daemon     # bonus: background daemon
make ctl        # bonus: separate control shell for the daemon
make bonus      # bonus demo: alert receivers + daemon (bonus.yaml) + control shell
make bonus-stop # stop the bonus daemon and the alert receivers
make clean      # remove logs  |  make fclean: also remove .venv  |  make re
```

Use another config file with `make run CONFIG=other.yaml`.
Without make, the same commands are `./taskmaster -c taskmaster.yaml`, `./taskmasterd`, `./taskmasterctl`
(PyYAML must then be installed: `pip3 install -r requirements.txt`).

Shell commands: `status`, `start`, `stop`, `restart` (`name`, `name:N` or `all`), `reload`,
`shutdown`, `quit`, `config`, `tail`, `pid`, `attach`, `help`.

Hot reload: run the `reload` command, or `kill -HUP <pid>`.

**Read [GUIDE.md](GUIDE.md)**. It explains everything: fork/exec/waitpid, the signals, the main
loop, the state machine, step-by-step scenarios, every config option, and the defense checklist.

Authors: mougnou, omanar
