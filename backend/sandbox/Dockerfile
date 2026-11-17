# Sandbox image for agent command execution.
#
# The API runs a project command as
#   docker run --rm --network none --memory 1g --cpus 1 --pids-limit 256 \
#     --cap-drop ALL --security-opt no-new-privileges \
#     --workdir /workspace --volume <workspace>:/workspace <image> sh -lc '<command>'
#
# so this image only has to provide a shell and the toolchain a coding task
# expects. It carries no credentials and no network access is granted by default.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/usr/local/bin:/usr/bin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        make \
        nodejs \
        npm \
        openssh-client \
        passwd \
        patch \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
        tar \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user: a command that escapes its working directory still owns
# nothing on the host beyond the mounted workspace.
RUN /usr/sbin/useradd --create-home --uid 10001 --shell /bin/bash sandbox \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace

CMD ["sh", "-lc", "echo 'agentos sandbox ready'"]
