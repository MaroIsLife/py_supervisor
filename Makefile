VENV    = .venv
PYTHON  = $(VENV)/bin/python
CONFIG ?= taskmaster.yaml

all: setup

# Create a virtual environment and install the dependencies (PyYAML) in it
setup: $(VENV)/.installed

$(VENV)/.installed: requirements.txt
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --quiet --upgrade pip
	$(PYTHON) -m pip install --quiet -r requirements.txt
	@touch $@
	@echo "setup done: run 'make run' (or 'make daemon' + 'make ctl')"

run: setup          ## foreground taskmaster with its shell
	$(PYTHON) ./taskmaster -c $(CONFIG)

daemon: setup       ## bonus: background daemon
	$(PYTHON) ./taskmasterd -c $(CONFIG)

ctl: setup          ## bonus: control shell for the daemon
	$(PYTHON) ./taskmasterctl -c $(CONFIG)

clean:              ## remove logs and caches
	rm -rf logs/*.stdout logs/*.stderr logs/*.log tmlib/__pycache__

fclean: clean       ## also remove the virtual environment
	rm -rf $(VENV)

re: fclean setup

.PHONY: all setup run daemon ctl clean fclean re
