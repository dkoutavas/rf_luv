#!/bin/bash
set -eo pipefail
# POCSAG runs USB-mode, like ADS-B: rtl_fm cannot read rtl_tcp on this
# hardware, so pipeline.sh frees the dongle with `rf-mode listen` first and
# this container opens it directly over USB.
#
# rtl_fm opens the dongle by serial (-d takes an index or, via
# verbose_device_search, a serial string — the same match rtl_tcp uses),
# FM-demods the paging channel to 22050 Hz raw audio, and pipes it to
# multimon-ng. multimon-ng decodes POCSAG at all three baud rates and prints
# one text line per page, which pocsag_ingest.py parses and inserts.
#
# pipefail: if rtl_fm or multimon-ng dies, the container exits instead of
# leaving the ingest reading a dead pipe.

exec rtl_fm \
    -M fm \
    -f "${POCSAG_FREQ:-169.6M}" \
    -s 22050 \
    -g "${POCSAG_GAIN:-40}" \
    -p "${POCSAG_PPM:-0}" \
    -d "${POCSAG_SERIAL:-v3-01}" \
    - | multimon-ng -t raw -a POCSAG512 -a POCSAG1200 -a POCSAG2400 - \
    | python3 -u /app/pocsag_ingest.py
