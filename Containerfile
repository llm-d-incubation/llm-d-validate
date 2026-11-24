# The unattended llmdv runner: llmdv, both publishing backends and Claude Code, running `llmdv job`.
# Build from the repository root:
#
#   podman build -f Containerfile --build-arg LLMDV_GIT_SHA="$(git rev-parse HEAD)" -t llmdv .
#
# Pins: the base image digest, requirements.lock and the native Claude Code checksums below.
# See deploy/README.md for how it runs and refreshes.
FROM registry.access.redhat.com/ubi9/python-311@sha256:a0bdb55576fc5b8d6704279307817828ef027e1065533ceba133fe9516003a6c

ARG LLMDV_GIT_SHA=unknown
ARG CLAUDE_CODE_VERSION=2.1.330
ARG CLAUDE_CODE_SHA256_AMD64=f390eaa0814e6a1bff74cbb47acb74cb9c89d30ced809fe0af5af00c7bfe8ebb
ARG CLAUDE_CODE_SHA256_ARM64=6016f90cac783e367fb1aa97c768a2efb94a0128a5b46b70fb3d8fcbbd9dd444

USER 0
# The native binary includes its runtime. Keep it outside /work and /tmp, which the Job mounts
# over, and outside any user's home so OpenShift's arbitrary UID can execute it.
# Checksums: https://downloads.claude.ai/claude-code-releases/2.1.330/manifest.json
RUN case "$(uname -m)" in \
      x86_64) platform=linux-x64; checksum="${CLAUDE_CODE_SHA256_AMD64}" ;; \
      aarch64) platform=linux-arm64; checksum="${CLAUDE_CODE_SHA256_ARM64}" ;; \
      *) echo "Claude Code requires linux/amd64 or linux/arm64" >&2; exit 1 ;; \
    esac \
 && curl --fail --show-error --location \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/${platform}/claude" \
      --output /usr/local/bin/claude \
 && echo "${checksum}  /usr/local/bin/claude" | sha256sum --check --strict \
 && chmod 755 /usr/local/bin/claude \
 && test "$(/usr/local/bin/claude --version)" = "${CLAUDE_CODE_VERSION} (Claude Code)"

COPY requirements.lock /tmp/llmdv/requirements.lock
COPY pyproject.toml README.md LICENSE /tmp/llmdv/
COPY src /tmp/llmdv/src
# /work is group-0 writable, so OpenShift's arbitrary UID can write run directories.
RUN pip install --no-cache-dir --require-hashes -r /tmp/llmdv/requirements.lock \
 && pip install --no-cache-dir --no-deps /tmp/llmdv \
 && rm -rf /tmp/llmdv \
 && mkdir -p /work \
 && chgrp 0 /work \
 && chmod g=u /work

# The root filesystem is read-only in the Job: no bytecode or pytest cache writes.
ENV LLMDV_GIT_SHA=${LLMDV_GIT_SHA} \
    LLMDV_WORK_DIR=/work/runs \
    HOME=/work \
    DISABLE_AUTOUPDATER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider"

WORKDIR /work
USER 1001
ENTRYPOINT ["llmdv", "job"]
