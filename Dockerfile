FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg converts the browser's WebM/Opus recording into the 16 kHz mono WAV
# that the ASR path expects (app/core/codecs.py: to_wav_16k). Without it the
# whole audio pipeline fails on the first turn.
#
# Static build instead of `apt-get install ffmpeg`: the apt closure weighed
# 457MB uncompressed / 170MB compressed — 70% of the entire image pull
# (docs/问题清单.md #18: TCR 个人版偶发慢拉总是卡在这一层). The binary is the
# johnvansickle static ffmpeg 7.0.2 shipped inside the pinned imageio-ffmpeg
# wheel — the same sourcing family setup/bootstrap.ps1 already trusts on
# Windows (its win_amd64 wheel comes from PyPI too).
# Tripwires so a broken upstream can never ship silently: wheel sha256 pinned
# below, and the build refuses to finish unless the binary is fully static,
# has the libopus encoder, and a real encode→wav16k round trip succeeds.
RUN python -m pip download -q --no-cache-dir --no-deps --only-binary=:all: \
        --platform manylinux2014_x86_64 \
        -d /tmp/iiff "imageio-ffmpeg==0.6.0" \
    && echo "c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282  /tmp/iiff/imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl" | sha256sum -c - \
    && python -c "import zipfile, shutil, os; z = zipfile.ZipFile('/tmp/iiff/imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl'); src = z.open('imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2'); dst = open('/usr/local/bin/ffmpeg', 'wb'); shutil.copyfileobj(src, dst); dst.close(); os.chmod('/usr/local/bin/ffmpeg', 0o755)" \
    && ldd /usr/local/bin/ffmpeg 2>&1 | grep -q "not a dynamic executable" \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q " libopus " \
    && ffmpeg -v error -f lavfi -i "sine=d=0.2" -c:a libopus -b:a 24k -y /tmp/rt.opus \
    && ffmpeg -v error -i /tmp/rt.opus -ar 16000 -ac 1 -f wav -y /tmp/rt.wav \
    && rm -rf /tmp/iiff /tmp/rt.opus /tmp/rt.wav

WORKDIR /app

COPY requirements.txt ./
# --no-compile: pip's .pyc cache weighed 52MB on disk / ~10MB online (docs/
# 问题清单.md #19). A resident container compiles into memory on first import
# anyway; PYTHONDONTWRITEBYTECODE above only governs runtime, not this.
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# Code plus the shipped sample textbooks (app/bundled_curriculum/) and the
# preset teacher profiles (app/bundled_teachers/). The user's material -
# data/curriculum/, student data, prompts, logs, teacher runtime state - is
# excluded by .dockerignore and arrives at runtime through the data/ bind
# mount of the deployment command (README「Linux VPS 一键部署」).
COPY app ./app
COPY static ./static
COPY data ./data
# Root-level channel entry points (run.py / run_feishu.py / run_repl.py).
# New channels only add a run_<name>.py here — the Dockerfile never changes
# and app/core/ stays unaware of any concrete channel.
COPY run*.py ./
# Deployment helpers. setup/install.sh runs inside a throwaway container
# (`docker run --rm … bash setup/install.sh`), so a fresh host needs nothing
# but Docker — no Python, no repo files. setup/check_env.py is also what
# `run_feishu.py --selftest` calls, so this copy fixes that entry point too.
COPY setup ./setup
# Tripwire: a CRLF shell script dies at `set -o pipefail` on Linux (image 1.4).
# .gitattributes enforces LF on checkout; this makes the build fail loudly if
# that ever regresses instead of shipping a broken installer.
RUN python -c "import glob,sys; bad=[f for f in glob.glob('setup/*.sh') if b'\r' in open(f,'rb').read()]; sys.exit('FATAL: CRLF line endings in: '+', '.join(bad)) if bad else None"

# App version baked at build time: tools/build_image.sh passes
# --build-arg APP_VERSION=<ver>, same source as the
# org.opencontainers.image.version label (tools/verify_image.sh asserts
# both agree). app/config.APP_VERSION reads it for the 「版本」 command
# and the upgrade-complete notice (#13 upgrade loop). Deliberately near
# the end: a version bump must never invalidate the ffmpeg/pip cache.
# "dev" = builds outside the release pipeline (local docker build).
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

EXPOSE 8000

# Bind to loopback: the web form is local-machine only (app/config.py pins the
# address). The feishu container overrides this CMD with run_feishu.py, which
# starts no HTTP server at all.
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
