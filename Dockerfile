# ax-presence listener — stdlib-only, so the image is tiny and dependency-free.
# The container keeps ONE agent present: holds the SSE stream, heartbeats so the
# agent shows online, shows live check-ins, and prints NOTIFY/CONTEXT/REMINDER
# lines to stdout (container logs). Bridging those lines into an agent *runtime*
# (Claude Code monitor, an openclaw runner, a daemon that spawns a run per NOTIFY)
# is the host's choice — see docs/ADDING-AN-AGENT.md.
FROM python:3.12-slim

WORKDIR /app
COPY ax_presence_listener.py /app/

# Identity comes entirely from env (AX_AGENT_HANDLE / AX_AGENT_ID / AX_SPACE_ID /
# AX_SPONSOR) and a mounted token file (AX_TOKEN_FILE). Nothing is baked in, so the
# same image runs any agent — peach, hermes, an openclaw agent — by env alone.
ENV PYTHONUNBUFFERED=1
CMD ["python3", "ax_presence_listener.py"]
