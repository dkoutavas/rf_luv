# POCSAG decoder + ingest in one container.
# rtl_fm (from rtl-sdr) FM-demods the paging channel over USB, multimon-ng
# decodes POCSAG, and pocsag_ingest.py batches rows into ClickHouse.
FROM alpine:3.21
RUN apk add --no-cache rtl-sdr multimon-ng python3 bash
WORKDIR /app
COPY pocsag_ingest.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
