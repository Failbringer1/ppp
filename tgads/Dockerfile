FROM python:3.12-slim

# Node.js + Claude Code CLI: the AI works through your Claude subscription, no API key.
# Build with --build-arg INSTALL_CLAUDE=0 to skip it (AI then runs in manual copy/paste mode).
ARG INSTALL_CLAUDE=1
RUN if [ "$INSTALL_CLAUDE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
      && npm install -g @anthropic-ai/claude-code \
      && rm -rf /var/lib/apt/lists/* /root/.npm; \
    fi

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tgads ./tgads

ENV TGADS_HOST=0.0.0.0 \
    TGADS_PORT=8080 \
    TGADS_DATA_DIR=/app/data \
    CLAUDE_CONFIG_DIR=/app/data/claude \
    PYTHONUNBUFFERED=1
VOLUME /app/data
EXPOSE 8080
CMD ["python", "-m", "tgads"]
