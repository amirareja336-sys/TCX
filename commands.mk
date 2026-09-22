PYTHON ?= python
SCRIPT  ?= v6_server.py
PORT    ?= 8080
HOST    ?= localhost

.PHONY: all start server v6 svc

# default target
all: start

# start the service (foreground)
start:
	$(PYTHON) $(SCRIPT) --port $(PORT) --host $(HOST)

# aliases for launching the same service
server: start
v6: start
svc: start

# example: make PORT=9090 HOST=0.0.0.0 start
