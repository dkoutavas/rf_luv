# Second dongle pre-flight checklist

Every item is a runnable command or SQL query against leap / its ClickHouse.
If an item's expected output doesn't appear, **do not proceed** — fix the
discrepancy or open a followup first.

Curl queries assume ClickHouse is reachable at `localhost:8126` with user
`spectrum`/`spectrum_local`, matching the spectrum-scanner container's
current settings. From leap's shell.

## Before V4 plug-in

- [ ] **V3 serial is `v3-01`**

  ```bash
  systemctl --user stop rtl-scanner@v3-01 rtl-tcp@v3-01 2>/dev/null || \
    systemctl --user stop rtl_tcp.service 2>/dev/null
  rtl_eeprom -d 0 | grep -i serial
  systemctl --user start rtl-tcp@v3-01 rtl-scanner@v3-01 2>/dev/null || \
    systemctl --user start rtl_tcp.service 2>/dev/null
  ```
  Expect: `Serial number:  v3-01`. Budget: ~15s downtime, one missed airband.

- [ ] **V4 serial was written off-leap before V4 enters leap's USB bus**

  On dev laptop or any Linux host with V4 as the only RTL-SDR attached:
  ```bash
  rtl_eeprom -d 0 | grep -i serial
  ```
  Expect: `Serial number:  v4-01`. If it says `00000001`, re-run
  `rtl_eeprom -d 0 -s v4-01` BEFORE shipping V4 to leap. See
  `spectrum/docs/dongle_identity.md`.

- [ ] **udev rules installed on leap**

  ```bash
  ls -la /etc/udev/rules.d/99-rtl-sdr.rules && \
    diff /etc/udev/rules.d/99-rtl-sdr.rules ~/dev/rf_luv/ops/udev/99-rtl-sdr.rules
  ```
  Expect: file exists, diff returns no output (files match).

- [ ] **Migrations 017 / 018 / 019 / 020 applied**

  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT version FROM spectrum.schema_migrations WHERE version IN ('017','018','019','020') ORDER BY version FORMAT TSV"
  ```
  Expect: 4 rows — `017`, `018`, `019`, `020`.

- [ ] **All existing rows carry `dongle_id='v3-01'` (not blank)**

  ```bash
  for t in scans peaks events sweep_health scan_runs signal_classifications peak_features compression_events; do
    printf "%-25s " "$t"
    curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
        --data-binary "SELECT count() FROM spectrum.$t WHERE dongle_id='' OR dongle_id IS NULL FORMAT TSV"
  done
  ```
  Expect: `0` on every table. If any non-zero, the ALTER UPDATE mutations
  haven't finished yet — check `SELECT * FROM system.mutations WHERE
  is_done=0` and wait, or re-run the UPDATE.

- [ ] **Scanner running the dongle-aware code, emitting `v3-01`**

  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT dongle_id, count() FROM spectrum.scans WHERE timestamp > now() - INTERVAL 10 MINUTE GROUP BY dongle_id FORMAT TSV"
  ```
  Expect: exactly one row, `v3-01\t<large number>`. Any blank row means
  the scanner hasn't been restarted with the new code (see cutover runbook
  Phase 2/4).

- [ ] **`run_id` format is dongle-prefixed**

  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT run_id, dongle_id FROM spectrum.scan_runs WHERE started_at > now() - INTERVAL 1 HOUR ORDER BY started_at DESC LIMIT 3 FORMAT TSV"
  ```
  Expect: `run_id` starts with `run_v3-01_`. If it starts with `run_2026...`
  the scanner is running pre-dongle-aware code.

- [ ] **Template units active for V3; singletons disabled**

  ```bash
  systemctl --user is-active rtl-tcp@v3-01.service rtl-scanner@v3-01.service rtl-tcp-watchdog@v3-01.timer
  systemctl --user is-enabled rtl_tcp.service rtl-tcp-watchdog.timer 2>&1 | head -5
  ```
  Expect: first command prints `active` three times. Second command shows
  `disabled` or `masked` for the singletons.

- [ ] **`/dev/rtl_sdr_v3` symlink exists**

  ```bash
  ls -la /dev/rtl_sdr_v3
  ```
  Expect: symlink pointing into `/sys/bus/usb/devices/*`. If missing,
  the V3 dongle hasn't re-enumerated since udev rules were installed —
  unplug/replug or reboot.

- [ ] **`rtl-tcp@v4-01.service` fails cleanly (stub)**

  ```bash
  systemctl --user enable rtl-tcp@v4-01.service 2>&1 || true
  systemctl --user start rtl-tcp@v4-01.service 2>&1 || true
  systemctl --user is-failed rtl-tcp@v4-01.service
  journalctl --user -u rtl-tcp@v4-01 -n 10 --no-pager | grep -i 'not found on bus'
  ```
  Expect: `is-failed` prints `failed`; journal shows
  `rtl-tcp-by-serial: serial 'v4-01' not found on bus`. NOT a unit-not-found
  or config error — the failure must be from the wrapper script.

- [ ] **`/etc/rtl-scanner/v4-01.env` exists but has TODOs flagged**

  ```bash
  ls -la /etc/rtl-scanner/v4-01.env
  grep -c 'TODO' /etc/rtl-scanner/v4-01.env
  ```
  Expect: file exists. `TODO` count **must be 0** before V4 is plugged in
  (operator needs to populate antenna position, recalibrate gain, etc.).
  At this pre-flight stage, a non-zero count is an explicit "do this next".

- [ ] **`dongle_comparison_view` exists and returns V3 rows**

  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT hour, freq_mhz_tile, v3_avg_power_dbfs, v4_avg_power_dbfs FROM spectrum.dongle_comparison_view WHERE hour > now() - INTERVAL 2 HOUR ORDER BY hour DESC, freq_mhz_tile LIMIT 5 FORMAT TSV"
  ```
  Expect: 5 rows. `v4_avg_power_dbfs` must be `\N` (NULL). If any row has a
  non-NULL v4 column while V4 isn't plugged in yet, something is ingesting
  rows under `dongle_id='v4-01'` — investigate before proceeding.

- [ ] **sudoers entry allows per-serial USB reset**

  ```bash
  sudo -n /usr/local/sbin/rtl-usb-reset v3-01 --help 2>&1 | head -5 || \
    grep -E 'rtl-usb-reset' /etc/sudoers.d/rtl-usb-reset
  ```
  Expect: either the script runs (may fail with "must run as root" or
  exit cleanly — depending on args), or the sudoers file lists both `v3-01`
  and `v4-01` explicitly.

## Before FM filter install on V3

These fire only after V4 has been running for ≥7 days with the filter.

- [ ] **A/B week completed — ≥7 days of parallel ingest**

  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT count(DISTINCT hour) FROM spectrum.dongle_comparison_view WHERE v3_avg_power_dbfs IS NOT NULL AND v4_avg_power_dbfs IS NOT NULL FORMAT TSV"
  ```
  Expect: ≥168 (7 days × 24 hours).

- [ ] **Quantified: noise-floor delta in FM band meets threshold (≤ -3 dB)**

  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT avg(delta_noise_floor_db) FROM spectrum.dongle_comparison_view WHERE freq_mhz_tile BETWEEN 88 AND 107 AND hour > now() - INTERVAL 7 DAY FORMAT TSV"
  ```
  Expect: ≤ `-3.0`. If higher (less negative), the filter is weak — see
  `spectrum/docs/ab_comparison.md` decision framework.

- [ ] **Quantified: passband loss in airband is acceptable (≥ -1 dB)**

  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT avg(delta_noise_floor_db) FROM spectrum.dongle_comparison_view WHERE freq_mhz_tile BETWEEN 118 AND 136 AND hour > now() - INTERVAL 7 DAY FORMAT TSV"
  ```
  Expect: ≥ `-1.0` (i.e., V4 is no more than 1 dB lower than V3 in airband).

- [ ] **Quantified: clip rate reduction ≥ 80%**

  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT sum(v4_clip_count_per_hour) / sum(v3_clip_count_per_hour) FROM spectrum.dongle_comparison_view WHERE hour > now() - INTERVAL 7 DAY FORMAT TSV"
  ```
  Expect: ≤ `0.2` (V4 clips ≤ 20% as often as V3).

- [ ] **No unexplained V3-only peaks outside FM band**

  Per the query in `ab_comparison.md` §4. Manual review required — list
  the `v3_only` peaks, filter to those outside 88–108 MHz, verify each is
  either a known FM harmonic or a spur explained by LNA front-end physics.
  If anything unknown survives, either update `known_frequencies` or
  decline the filter.

- [ ] **Schema ready for `filter` tagging on new `scan_runs`**

  A followup migration (021) must add a `filter` column to `scan_runs` so
  the transition is marked in the data. Until that migration lands and
  scanner/ingest emit the `filter` field, the FM-filter-installed run is
  indistinguishable in scan_runs from a pre-filter run.
  ```bash
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "SELECT count() FROM system.columns WHERE database='spectrum' AND table='scan_runs' AND name='filter' FORMAT TSV"
  ```
  Expect: `1` once the followup is applied. This checklist item is the
  gate to complete that followup before proceeding with physical install.

- [ ] **Baseline re-derivation plan ready**

  V3 post-filter is a new noise regime. The `hourly_baseline` aggregate for
  `dongle_id='v3-01'` becomes invalid as a baseline for the filtered signal.
  Plan (documented here, executed during the filter-install window):
  ```bash
  # On install day, truncate V3's baseline and let it rebuild:
  curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
      --data-binary "ALTER TABLE spectrum.hourly_baseline DELETE WHERE dongle_id='v3-01'"
  # Wait ~24h for the MV to accumulate a fresh baseline. During the warmup,
  # detect_compression.py's sig_baseline flag for V3 will be unreliable
  # (few samples, high variance). Flag this in the compression_events notes.
  ```

- [ ] **detect_compression.py post-filter review**

  Per `spectrum/docs/followups/dongle_id_downstream.md`, `detect_compression.py`
  must filter `hourly_baseline` by `dongle_id` before V4 ingests. If this
  is already fixed, confirm behavior with:
  ```bash
  python3.11 ~/dev/rf_luv/spectrum/analysis/detect_compression.py --dry-run --dongle-id v3-01 2>&1 | head -20
  ```
  Expect: no errors referencing `hourly_baseline` column count mismatches.
  If the filter isn't implemented yet, it must be before V4 ingests — this
  pre-flight step is blocked on that followup.

  Also note: `sig_clip` logic uses 174–230 MHz band (DVB-T) and is unaffected
  by FM filter. Keep as-is. `sig_clip_fm` (88–108 MHz) should rarely fire
  post-filter — that's expected behavior, not a bug.

## Meta

- [ ] **All followups in `spectrum/docs/followups/dongle_id_downstream.md` are
      either completed or explicitly deferred with a written reason.**

  ```bash
  ls ~/dev/rf_luv/spectrum/docs/followups/ | grep -i dongle
  ```
  Open the file, go line-by-line through the consumers list. If any are
  still pending at V4-install time, the cutover has a known correctness gap.
