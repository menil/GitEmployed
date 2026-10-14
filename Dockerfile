# Stage 1: Build pdf-vdiff Rust CLI and download pdfium shared library
# Uses the official rust:1-slim-bookworm (Rust 1.x stable) to satisfy MSRV for crates.io dependencies
FROM rust:1-slim-bookworm AS rust-builder
ARG TARGETARCH
ARG PDFIUM_VERSION=7881
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN cargo install --git https://github.com/menil/pdf-vdiff.git --root /opt/pdf-vdiff

RUN mkdir -p /opt/pdf-vdiff/lib && \
    case "${TARGETARCH:-amd64}" in \
      "arm64") PDFIUM_FILE="pdfium-linux-arm64.tgz" ;; \
      *)       PDFIUM_FILE="pdfium-linux-x64.tgz" ;; \
    esac && \
    curl -fsSL "https://github.com/bblanchon/pdfium-binaries/releases/download/chromium/${PDFIUM_VERSION}/${PDFIUM_FILE}" | tar -xz -C /opt/pdf-vdiff/lib/

# Stage 2: Base runner environment
# Minimal shared environment with Python 3.12, git, gh, curl, and uv
FROM python:3.12-slim-bookworm AS runner-base

# Prevent interactive prompts during apt package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install minimal system dependencies for git CLI, gh CLI, and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gh \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for ultra-fast python package installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set the project environment to target system Python
ENV UV_PROJECT_ENVIRONMENT=/usr/local

# Stage 3: Full runner tools (Chromium, Bun, resumed, and pdf-vdiff)
# Pre-installs heavy PDF rendering dependencies independently of app code to preserve layer cache
FROM runner-base AS full-tools

# Install system dependencies for just, fontconfig, and Chromium (+ sandbox helper)
# for headless PDF export via resumed/Puppeteer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    shellcheck \
    fonts-dejavu \
    fonts-liberation \
    chromium \
    chromium-sandbox \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged account for running Chromium (see the `bun add -g` step below).
RUN useradd --create-home --shell /bin/sh pptruser

# Install Casey's 'just' command runner
RUN curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to /usr/local/bin

# Install Bun: the JS runtime/package manager for the JSON Resume `resumed`
# CLI. Bun is npm-registry-compatible and ships its own npx equivalent, so no
# separate Node/npm is needed.
COPY --from=oven/bun:latest /usr/local/bin/bun /usr/local/bin/bunx /usr/local/bin/

# Install pdf-vdiff binary and pdfium library from builder stage
COPY --from=rust-builder /opt/pdf-vdiff/bin/pdf-vdiff /usr/local/bin/pdf-vdiff
COPY --from=rust-builder /opt/pdf-vdiff/lib/lib/libpdfium.so /usr/local/lib/libpdfium.so
RUN ldconfig

# Point Puppeteer (used by `resumed export`) at the apt-installed Chromium
# above instead of downloading its own bundled copy.
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Install Bun's global packages under /opt/bun rather than the default ~/.bun
ENV BUN_INSTALL=/opt/bun
ENV PATH="/opt/bun/bin:${PATH}"

# Install resumed, puppeteer, and the pinned default resume theme as global Bun packages
RUN bun add -g resumed@7.0.0 puppeteer@25.11.0 @jsonresume/jsonresume-theme-professional@1.0.22 \
    && chmod -R a+rX /opt/bun

# Stage 4: Slim runner image
# Lightweight runner with Python 3.12 and GitEmployed CLI pre-installed
FROM runner-base AS slim

WORKDIR /workspace

# Copy the project so uv can build and install it. README.md is required:
# pyproject.toml declares it as the package readme, so the build fails without it.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Sync dependencies AND install the project system-wide, so the gitemployed CLI
# runs from any working directory without PYTHONPATH or a src/ checkout
RUN uv sync --frozen

# Stage 5: Full runner image (default)
# Complete runner with full PDF export, visual diffing, and GitEmployed CLI pre-installed
FROM full-tools AS full

WORKDIR /workspace

# Copy the project so uv can build and install it.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Sync dependencies AND install the project system-wide
RUN uv sync --frozen

