# ---- Build stage ----
FROM rust:1.82-slim AS builder

WORKDIR /app

# Install minimal build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Cache dependencies separately from source
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release --locked
RUN rm -rf src

# Build actual source
COPY src ./src
# Touch main.rs so cargo rebuilds (the cached dummy above prevents a full rebuild)
RUN touch src/main.rs
RUN cargo build --release --locked

# ---- Runtime stage ----
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/target/release/bottie /usr/local/bin/bottie

# Config and data directories — mount these as volumes
RUN mkdir -p /app/data

# Non-root user for safety
RUN useradd -m -u 1000 bottie && chown -R bottie:bottie /app
USER bottie

ENTRYPOINT ["bottie"]
