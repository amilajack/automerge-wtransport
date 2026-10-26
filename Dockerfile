FROM rust:1.90-bookworm AS build
WORKDIR /work
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY schema.automerge ./schema.automerge
RUN cargo build --release --locked
FROM debian:bookworm-slim AS server
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates iproute2 procps ethtool && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /work/target/release/automerge-wtransport /app/server
COPY dist ./dist
EXPOSE 4433/tcp 4433/udp
ENTRYPOINT ["/app/server"]
FROM mcr.microsoft.com/playwright:v1.56.1-noble AS clients
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci && apt-get update && apt-get install -y --no-install-recommends iproute2 ethtool && rm -rf /var/lib/apt/lists/*
COPY scripts ./scripts
COPY bench ./bench
COPY certs/spki.txt ./certs/spki.txt
ENV CHROMIUM_CHANNEL=chromium
ENTRYPOINT ["node","bench/run.mjs"]
