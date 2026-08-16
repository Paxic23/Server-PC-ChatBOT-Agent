# Base image for every jarvis sandbox run (run_python + approved custom tools).
# Build with:
#   docker build -t jarvis-sandbox:py3.12 sandbox_image/
#
# Add simulation dependencies (numpy, scipy, whatever your scripts need) by
# extending this file, not by relying on network access at run time — the
# default network mode is "none", so pip installs at run time will fail
# unless the session explicitly uses "restricted" or "full" network mode.

FROM python:3.12-slim

# Matches sandbox.run_as_uid in config/config.yaml — must stay in sync.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin sandboxuser

RUN pip install --no-cache-dir numpy pandas matplotlib requests

WORKDIR /workspace
USER 10001:10001
