# Customizing the spectrum scanner for your location

The defaults in this repo are tuned for an Athens-area RTL-SDR setup
(see `../README.md` for context). To deploy the same pipeline at your
location, walk through the four sections below. None of them require
editing source — everything is exposed via env vars or seed files.

## 1. Match your dongle

The scanner tags every emitted row with a `dongle_id`, so multi-dongle
deployments don't conflate data. Find your dongle's EEPROM serial:

```bash
rtl_eeprom -d 0           # add -d 1, -d 2 etc. for additional dongles
# look for "Serial number:"
```

Set it via env (or `spectrum/.env`):

```ini
SCAN_DONGLE_ID=v3-01      # match exactly the EEPROM string
```

If you have two dongles, write distinct serials with `rtl_eeprom -s` and run
one scanner+ingest pair per dongle. The systemd templates in
`../ops/rtl-tcp/` instantiate per-serial via `rtl-tcp@<serial>.service`.

For udev-stable `/dev/rtl_sdr_*` symlinks, see the udev rules in
`../setup/install-windows.md` (Linux equivalent: install via
`bash ops/rtl-tcp/install.sh` which writes them to `/etc/udev/rules.d/`).

## 2. Match your location

### Antenna metadata

Free-form strings recorded with each scan run for later A/B comparisons.
None of them affect DSP — they're pure documentation.

```ini
SCAN_ANTENNA_POSITION=window_north   # or rooftop_tripod, patio, etc.
SCAN_ANTENNA_ARMS_CM=53              # quarter-wave for ~137 MHz
SCAN_ANTENNA_ORIENTATION=180         # bearing degrees, 0 = N
SCAN_ANTENNA_HEIGHT_M=2
SCAN_NOTES=stock dipole, sea-facing
```

### Known frequencies — the classifier prior

`spectrum.known_frequencies` is the table that biases the classifier when a
peak lands within ±150 kHz of a known signal. It starts empty after a fresh
install. Two paths to populate it:

1. **Use an existing seed** — Athens is provided as an example:
   ```bash
   cat spectrum/clickhouse/seeds/known_frequencies_athens.sql \
     | docker exec -i clickhouse-spectrum clickhouse-client \
         --user spectrum --password "${CLICKHOUSE_PASSWORD:-spectrum_local}"
   ```
2. **Write your own** — copy
   `spectrum/clickhouse/seeds/known_frequencies_template.sql.example`
   to `known_frequencies_<your_location>.sql`, fill in your local FM
   stations, ATC, marine, ISM, etc., then run the same `docker exec` line.

The seed files are idempotent (`WHERE (SELECT count() ...) = 0`) — re-running
won't duplicate rows.

### DVB-T exclusion zone

The scanner excludes the country's DVB-T (digital TV) range from its
clipping report so strong DTV transmitters don't pollute "worst-case
clipping" diagnostics.

| Region | DVB-T Band III range | Env value |
|---|---|---|
| Greece / most of Europe | 174–230 MHz | default |
| US (no Band III DVB-T) | none | set start = end = 0 |
| Australia | 174–230 MHz | default |
| China | 470–862 MHz (Band IV/V) | adjust to match |

```ini
SCAN_DVBT_EXCLUDE_START=174000000
SCAN_DVBT_EXCLUDE_END=230000000
```

## 3. Choose your sweep band

Defaults sweep 88–470 MHz (FM through UHF pagers). Narrow or widen via:

```ini
SCAN_FREQ_START=88000000      # 88 MHz
SCAN_FREQ_END=470000000       # 470 MHz
SCAN_BIN_WIDTH=100000         # 100 kHz bins
```

For HF (shortwave, < 30 MHz), the V3 dongle's direct-sampling mode is
required — that's a different scanner setup not covered here.

The two built-in presets (`full` and `airband`) are hardcoded in
`scanner.py`'s preset list; if you want to sweep a different fast-cadence
band (e.g. marine VHF instead of airband), override `SCAN_AIRBAND_START` /
`SCAN_AIRBAND_END` for the simple case, or edit `scanner.py`'s preset list
for more.

## 4. Connect to a remote rtl_tcp

The scanner connects via TCP to wherever rtl_tcp is running. Three layouts:

| Layout | rtl_tcp host | `RTL_TCP_HOST` |
|---|---|---|
| Same machine, Docker scanner | `host.docker.internal` (default) | `host.docker.internal` |
| Same machine, native systemd scanner | `127.0.0.1` | `127.0.0.1` |
| Remote (rtl_tcp on a different box) | the box's IP/hostname | e.g. `192.168.1.50` |

The remote case is useful when the dongle lives on a Raspberry Pi or
similar that's better-positioned for the antenna, while ClickHouse +
Grafana run on a beefier machine elsewhere on the network.

```ini
RTL_TCP_HOST=192.168.1.50
RTL_TCP_PORT=1234
```

`rtl_tcp` itself must bind to `0.0.0.0` (not `127.0.0.1`) for the remote
case to work, and the host's firewall must allow inbound TCP on port 1234.

## What we did *not* parameterize

Some knobs aren't in `.env.example` because they're tied to dongle hardware
(sample rate, FFT size) or detection tuning that affects the schema (peak
threshold, transient threshold). Edit `scanner.py` directly if you need to
move those.

The 21 SQL migrations under `clickhouse/migrations/` define the schema —
they should run unchanged on any deployment. Migration 011
(`signal_catalog`) seeds ~150 reference rows; a few dozen are
Athens-specific and can be deleted by hand if they bother you, but the
classifier doesn't read from `signal_catalog` so leaving them in is
harmless.
