VENV    = .venv
PYTHON  = $(VENV)/bin/python
CONFIG ?= taskmaster.yaml
BONUS  ?= bonus.yaml

all: setup

# Create a virtual environment and install the dependencies (PyYAML) in it
setup: $(VENV)/.installed

$(VENV)/.installed: requirements.txt
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --quiet --upgrade pip
	$(PYTHON) -m pip install --quiet -r requirements.txt
	@touch $@
	@echo "setup done: make run (mandatory), make bonus (bonus demo)"

run: setup          ## foreground taskmaster with its shell
	$(PYTHON) ./taskmaster -c $(CONFIG)

daemon: setup       ## bonus: background daemon
	$(PYTHON) ./taskmasterd -c $(CONFIG)

ctl: setup          ## bonus: control shell for the daemon
	$(PYTHON) ./taskmasterctl -c $(CONFIG)

# Bonus demo: alert receivers + daemon ($(BONUS)) + control shell.
# Watch the alerts with:  tail -f logs/webhook.log logs/smtp.log
bonus: setup
	@$(PYTHON) programs/webhook_server.py 8080 > logs/webhook.log 2>&1 & echo $$! > logs/webhook.pid
	@$(PYTHON) programs/fake_smtp.py 1025 > logs/smtp.log 2>&1 & echo $$! > logs/smtp.pid
	@sleep 0.5
	$(PYTHON) ./taskmasterd -c $(BONUS)
	@echo "alerts: tail -f logs/webhook.log logs/smtp.log   |   stop everything: make bonus-stop"
	$(PYTHON) ./taskmasterctl -c $(BONUS)

bonus-stop:         ## stop the daemon and the alert receivers
	-@$(PYTHON) ./taskmasterctl -c $(BONUS) shutdown
	-@kill `cat logs/webhook.pid logs/smtp.pid 2>/dev/null` 2>/dev/null; rm -f logs/*.pid

clean:              ## remove logs and caches
	rm -rf logs/*.stdout logs/*.stderr logs/*.log logs/*.pid tmlib/__pycache__

fclean: clean       ## also remove the virtual environment
	rm -rf $(VENV)

re: fclean setup

.PHONY: all setup run daemon ctl bonus bonus-stop clean fclean re
