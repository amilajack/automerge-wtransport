FROM rust:1.90-alpine AS build
RUN apk add --no-cache build-base perl
WORKDIR /work
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY schema.automerge ./schema.automerge
RUN --mount=type=cache,id=amwt-lambda-registry,target=/usr/local/cargo/registry --mount=type=cache,id=amwt-lambda-target,target=/work/target cargo build --release --locked --features lambda --bin lambda-client && cp target/release/lambda-client /bootstrap
FROM scratch
COPY --from=build /bootstrap /var/runtime/bootstrap
ENTRYPOINT ["/var/runtime/bootstrap"]
