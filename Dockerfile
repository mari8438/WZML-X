FROM python:3.12-slim-bookworm AS nllb-builder

RUN python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==2.12.1" \
    && python -m pip install --no-cache-dir \
        "ctranslate2==4.8.1" \
        "transformers<5" \
        sentencepiece \
    && ct2-transformers-converter \
        --model facebook/nllb-200-distilled-600M \
        --output_dir /opt/models/nllb-200-distilled-600M-int8 \
        --quantization int8 \
        --copy_files tokenizer.json tokenizer_config.json sentencepiece.bpe.model special_tokens_map.json

FROM python:3.12-slim-bookworm AS mega-sdk-builder

# The MEGA Python API is a native binding, not the unrelated ``mega.py``
# package. Build the official SDK against the same Python version as the bot
# so ``from mega import MegaApi`` works in the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        autoconf \
        automake \
        build-essential \
        ca-certificates \
        git \
        libboost-system-dev \
        libc-ares-dev \
        libcrypto++-dev \
        libcurl4-openssl-dev \
        libfreeimage-dev \
        libicu-dev \
        libsodium-dev \
        libsqlite3-dev \
        libssl-dev \
        libtool \
        pkg-config \
        swig \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mega-sdk
RUN python -m pip install --no-cache-dir "setuptools<70" wheel \
    && git clone --depth 1 --branch v4.30.0 https://github.com/meganz/sdk.git . \
    && ./autogen.sh \
    && ./configure --disable-silent-rules --enable-python --with-python3 --disable-examples \
    && make -j"$(nproc)" \
    && cd bindings/python \
    && python setup.py bdist_wheel

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/wzvenv \
    PATH="/wzvenv/bin:${PATH}"

WORKDIR /usr/src/app

RUN sed -i 's/Components: main/Components: main contrib non-free/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        aria2 \
        bash \
        build-essential \
        ca-certificates \
        coreutils \
        cpulimit \
        curl \
        ffmpeg \
        fontconfig \
        fonts-dejavu-core \
        git \
        imagemagick \
        jq \
        libc-ares2 \
        libcrypto++8 \
        libcurl4 \
        libffi-dev \
        libfreeimage3 \
        libicu72 \
        libmagic1 \
        libsqlite3-0 \
        libsodium23 \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        mediainfo \
        mktorrent \
        netcat-openbsd \
        nodejs \
        p7zip-full \
        par2 \
        pkg-config \
        procps \
        qbittorrent-nox \
        rclone \
        sabnzbdplus \
        tini \
        tzdata \
        unrar-free \
        unzip \
        util-linux \
        zlib1g-dev \
        zip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp's current YouTube challenge solver requires a supported JavaScript
# runtime. Debian's Node.js 18 is intentionally kept for other tooling, while
# Deno provides a supported runtime for yt-dlp-ejs.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && deno --version

RUN python -m venv /wzvenv \
    && python -m pip install --no-cache-dir --upgrade pip uv setuptools wheel

COPY --from=mega-sdk-builder /opt/mega-sdk/bindings/python/dist/ /tmp/megasdk/
COPY requirements.txt .
RUN uv pip install --python /wzvenv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /wzvenv/bin/python --no-cache /tmp/megasdk/*.whl \
    && apt-get purge -y --auto-remove \
        build-essential \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        pkg-config \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY --from=nllb-builder /opt/models/nllb-200-distilled-600M-int8 /opt/models/nllb-200-distilled-600M-int8
RUN test -s /opt/models/nllb-200-distilled-600M-int8/model.bin \
    && test -s /opt/models/nllb-200-distilled-600M-int8/config.json \
    && test -s /opt/models/nllb-200-distilled-600M-int8/tokenizer_config.json

COPY . .
RUN chmod +x start.sh setpkgs.sh

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "bash", "start.sh"]
