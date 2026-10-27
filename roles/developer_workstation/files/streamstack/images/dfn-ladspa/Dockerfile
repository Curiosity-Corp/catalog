# syntax=docker/dockerfile:1
#
# The DeepFilterNet LADSPA plugin (libdeep_filter_ladspa.so) from a pinned upstream release, built by BuildKit on
# the K8s runners (`dfn-ladspa-image` in .gitlab-ci.yml). The image is only the carrier: `dfn-ladspa-package` copies
# /opt/dfn-ladspa out of it and publishes it to the project's Generic Package Registry (see docs/audio-chain.md).
#
#   source      the upstream tag archive, refused unless it matches the pinned sha256 (ADD --checksum)
#   build       cargo build --locked --profile release-lto -p deep-filter-ladspa (upstream's own release recipe)
#   checks      the licence policy and the CycloneDX SBOM (supply_chain.py), then a load-and-run smoke test (smoke.py)
#   result      /opt/dfn-ladspa: the .so, SHA256SUMS, the SBOM, LICENSES.json, the upstream licences, BUILD-INFO.txt
#
# Bumping DeepFilterNet: change the tag and the sha256 below and the pin in src/streamstack/audiochain/plugin.py
# (tests/test_dfn_ladspa_ci.py fails when they disagree); raise BUILD_REVISION in both for a rebuild of the same tag.
#
# Rust is pinned to 1.75.0, not the latest stable, on purpose: upstream v0.5.6's Cargo.lock (August 2023) holds
# crates, e.g. time 0.3.28, that no longer compile on Rust 1.80 and later. The lockfile is the reproducible input
# (`--locked`); the toolchain of its day is the one it was written for.

FROM harbor.focuscell.org/dockerhub-proxy/library/rust:1.75.0-bookworm AS build

# The pin (kept literal: `ADD --checksum` must see the digest).
ADD --checksum=sha256:0f9a219d06c404bc4200f228e7e224f108cbbca04d7227a6de7d2ce974c2f579 \
    https://github.com/Rikorose/DeepFilterNet/archive/refs/tags/v0.5.6.tar.gz /dfn-0.5.6.tar.gz

ENV CARGO_HOME=/cargo \
    CARGO_INCREMENTAL=0 \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    RUSTFLAGS="--remap-path-prefix=/src=/build --remap-path-prefix=/cargo=/cargo"

RUN mkdir /src && tar -xzf /dfn-0.5.6.tar.gz -C /src --strip-components=1
WORKDIR /src
RUN cargo build --locked --profile release-lto -p deep-filter-ladspa

COPY supply_chain.py smoke.py /tools/
RUN set -eu; \
    mkdir -p /opt/dfn-ladspa; \
    cp target/release-lto/libdeep_filter_ladspa.so /opt/dfn-ladspa/libdeep_filter_ladspa.so; \
    cargo metadata --locked --format-version 1 --filter-platform x86_64-unknown-linux-gnu > /tmp/metadata.json; \
    python3 /tools/supply_chain.py \
      --metadata /tmp/metadata.json --lock Cargo.lock --so /opt/dfn-ladspa/libdeep_filter_ladspa.so \
      --out /opt/dfn-ladspa --version 0.5.6-1 \
      --source-url https://github.com/Rikorose/DeepFilterNet/archive/refs/tags/v0.5.6.tar.gz \
      --source-sha256 0f9a219d06c404bc4200f228e7e224f108cbbca04d7227a6de7d2ce974c2f579; \
    python3 /tools/smoke.py /opt/dfn-ladspa/libdeep_filter_ladspa.so; \
    cp LICENSE LICENSE-MIT LICENSE-APACHE /opt/dfn-ladspa/; \
    { echo "upstream: Rikorose/DeepFilterNet v0.5.6"; \
      echo "source sha256: 0f9a219d06c404bc4200f228e7e224f108cbbca04d7227a6de7d2ce974c2f579"; \
      echo "build revision: 1"; \
      rustc --version; cargo --version; } > /opt/dfn-ladspa/BUILD-INFO.txt; \
    cd /opt/dfn-ladspa && sha256sum libdeep_filter_ladspa.so libdeep_filter_ladspa.cdx.json LICENSES.json > SHA256SUMS

FROM harbor.focuscell.org/dockerhub-proxy/curlimages/curl:8.23.0
LABEL org.opencontainers.image.title="streamstack-dfn-ladspa" \
      org.opencontainers.image.description="DeepFilterNet LADSPA plugin 0.5.6, build revision 1" \
      org.opencontainers.image.licenses="MIT OR Apache-2.0" \
      org.opencontainers.image.source="https://git.developerdojo.org/Curiosity-Corp/streamstack"
COPY --from=build /opt/dfn-ladspa /opt/dfn-ladspa
