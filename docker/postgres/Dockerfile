# The same Postgres our data volume already runs (postgres:16-alpine, pinned
# by digest to 16.14 on Alpine 3.23), with pgvector built in for
# fastmcp-feedback's embeddings. Staying on Alpine keeps musl's collation:
# the Debian pgvector image would silently misorder our text indexes.
# with_llvm=no: the base image's JIT bitcode wants a clang Alpine may not ship.
# Based on fastmcp-feedback's tested docker/pgvector-alpine Dockerfile.
FROM postgres:16-alpine@sha256:890480b08124ce7f79960a9bb16fe39729aa302bd384bfd7c408fee6c8f7adb7
ARG PGVECTOR_VERSION=v0.8.6
RUN apk add --no-cache --virtual .build-deps git build-base \
 && git clone --depth 1 --branch ${PGVECTOR_VERSION} https://github.com/pgvector/pgvector.git /tmp/pgvector \
 && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no \
 && make -C /tmp/pgvector install with_llvm=no \
 && rm -rf /tmp/pgvector \
 && apk del .build-deps
