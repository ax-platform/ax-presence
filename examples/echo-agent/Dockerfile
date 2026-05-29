# Minimal container for the echo agent. stdlib-only, so no pip install.
# The device-code approval happens interactively on first run, and the token
# is written under /state — mount a volume there so it survives restarts.
FROM python:3.12-slim

WORKDIR /app
# bring the whole repo so echo_agent.py can import ax_presence_listener + monitor_core
COPY . /app

ENV AX_BASE=https://paxai.app \
    HOME=/state
VOLUME /state

# AX_AGENT_HANDLE / AX_SPACE_ID are provided at run time (see docker-compose.yml).
ENTRYPOINT ["python3", "examples/echo-agent/echo_agent.py"]
