# POCSAG decoder + ingest in one container.
# rtl_fm (from rtl-sdr) FM-demods the paging channel over USB, multimon-ng
# decodes POCSAG, and pocsag_ingest.py batches rows into ClickHouse.
#
# multimon-ng is not packaged for Alpine, so build it from source at a pinned
# tag (the same fork setup/install-packages.sh falls back to).

FROM alpine:3.21 AS build
RUN apk add --no-cache build-base cmake git
RUN git clone --depth 1 --branch 1.6.2 https://github.com/EliasOenal/multimon-ng /src \
    && cmake -S /src -B /src/build \
    && cmake --build /src/build --target multimon-ng

FROM alpine:3.21
RUN apk add --no-cache rtl-sdr python3 bash libstdc++
COPY --from=build /src/build/multimon-ng /usr/local/bin/multimon-ng
WORKDIR /app
COPY pocsag_ingest.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
