# Image used for BOTH bots and for every worker: one build, three roles.
# The role is passed as an argument to main.py (see deploy/makemyno.service.template
# for why it must not come from the environment), so a worker can never be handed
# a bot token it has no business holding.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System packages are a FALLBACK ONLY, and this step must never fail the build.
#
# Pillow, cryptography and asyncssh all ship manylinux wheels, so on a normal
# build nothing here is needed at all — it only matters if pip is forced to
# compile. Meanwhile a fresh VPS routinely has one flaky apt mirror, and my
# earlier version let that kill the whole image: provisioning failed with a wall
# of apt output while the actual Python install would have worked fine.
#
# `|| true` is therefore deliberate, and matches what the base project learned
# the same way.
RUN (apt-get update -qq \
     && apt-get install -y --no-install-recommends \
        gcc libffi-dev libjpeg62-turbo-dev zlib1g-dev curl \
     && rm -rf /var/lib/apt/lists/*) || true

# Dependencies before the source, so editing code does not invalidate this layer.
COPY requirements.txt .
# Upgrading pip first matters: an old pip may ignore a manylinux wheel and try to
# build from source, which is exactly when the apt step above becomes load-bearing.
# The retries and longer timeout are for servers with slow or unreliable networks.
# --prefer-binary is the important flag. A provisioning attempt was seen COMPILING
# telethon from its source tarball — thousands of files through bdist_wheel — which
# on a small VPS is slow enough to look hung and can exhaust memory outright.
# telethon is pure Python and publishes a ready wheel; pip only fell back to
# building because an index mirror served the sdist first. --prefer-binary tells it
# to take the wheel wherever one exists.
# `-i https://pypi.org/simple` is NOT redundant, and both reference projects carry
# it for a reason I rediscovered the hard way: a server can ship a pip.conf
# pointing at a local mirror, and a mirror that serves the sdist instead of the
# wheel makes pip COMPILE telethon from source — thousands of files through
# bdist_wheel, which on a small VPS is slow enough to look hung and can fill the
# disk outright. Naming the index explicitly removes that variable.
RUN pip install --upgrade pip \
 && pip install --prefer-binary --retries 10 --timeout 180 \
      -i https://pypi.org/simple -r requirements.txt

# The .git directory is copied deliberately: a worker has no git binary, and
# /ping reads the commit straight out of .git so the owner panel can show which
# workers are running old code.
COPY . .

# Sessions and job state live here; the host bind-mounts it so a container
# rebuild never loses a login.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8765

# A worker. The two bots override this with their own role argument.
CMD ["python", "main.py", "worker"]
