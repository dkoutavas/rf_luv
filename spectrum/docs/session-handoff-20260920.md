# Spectrum scanner verification — session handoff (2026-09-20)

## What was verified

The scanner has been running on V4 :1234 for ~20 hours. All components were
audited and the intelligence pipeline was installed for the first time on the
Omen.

### Data flow (end-to-end, all verified with row counts)

```
scanner.py → scan_ingest.py → spectrum.scans (160K+)
                            → spectrum.peaks (1.4K+)
                            → spectrum.sweep_health (176+, no clipping)
feature_extractor.py (5 min timer) → spectrum.peak_features (173)
classifier.py (5 min timer) → spectrum.signal_classifications (173)
classifier_health.py (5 min timer) → spectrum.classifier_health (1)
```

### Classifications (real Athens RF, first run)

| Frequency | Class | Confidence |
|-----------|-------|------------|
| 144.65 MHz | nfm_voice_repeater | 1.0 |
| 144.75 MHz | nfm_voice_repeater | 1.0 |
| 156.63 MHz | marine_vhf_channel | 1.0 |
| 127.66–132.56 MHz | am_airband_atc | 0.8 |

### Grafana

All 8 pipeline folders present. Three spectrum dashboards provisioned:
- Spectrum Scanner - 88-470 MHz (main)
- Spectrum Classifier Health (newly populated)
- Spectrum (Run Comparison)

Logging form on :8084 running (nginx container).

### Timers installed

```
spectrum-features.timer         (5 min, feature_extractor.py)
spectrum-classifier.timer       (5 min, classifier.py)
spectrum-classifier-health.timer (5 min, classifier_health.py)
```

### SDR++ listening path

SDR++ v1.2.1 installed, `rtl_tcp_source.so` present. Connect to V3 on
`127.0.0.1:1235` for live listening while the scanner runs on V4 :1234.

**Watchdog fix (same session, later).** The original `has_external_client()`
only skipped non-loopback peers. SDR++, the scanner, and the spirit box all
connect from 127.0.0.1, so the watchdog treated them as invisible.
Result on 2026-09-20:
- V3: 14 soft restarts, 5 watchdog crashes (`TimeoutError` in `probe()`)
- V4: 7 soft restarts, 1 crash, 28 scanner reconnect retries

Root cause: the watchdog probed every 30 s, kicked the current client off,
then restarted rtl_tcp on the first failure. Stopping the unit by hand did
not help because the watchdog timer resurrected it.

Fix (branch `fix/watchdog-client-detection`):
1. `has_external_client` replaced with `has_active_client`: any ESTABLISHED
   peer (loopback included) skips the probe.
2. `probe()` greeting loop now catches `socket.timeout` (no more crashes).
3. First failed probe logs and waits; recovery starts at fail 2.
4. `ops/rf-mode` script: `rf-mode listen` stops timer then unit, `rf-mode
   scan` starts unit then timer. For direct-USB tools only.

QUICKREF.md and all four `scripts/*.sh` caveats updated to use `rf-mode`.

### Shell scripts

`scripts/{airband,ais,ism,satellite}*.sh` use direct USB, not rtl_tcp. A
caveat block in each says to run `rf-mode listen` first, `rf-mode scan` after.

### What was not done

- Listening Playbook and Run Comparison dashboards not opened (need the GUI).
- Logging form test entry not written (need the browser).
- `ops/install-trip-hardening.sh` not run (escalator, probes, notify).
  These are the remaining ops-layer installers from RESTORE.md step 8.
- Scanner reconnect-per-sweep churn (28 restarts, 1 SIGABRT). Needs its own
  evidence cycle.

## 2026-09-22: plug and play (branch `fix/plug-and-play-dongles`)

Reboot with the V3 unplugged: `rtl-tcp@v3-01` (`RTL_TCP_DEVICE_INDEX=0`)
opened the V4, `rtl-tcp@v4-01` (index 1) restart-looped on
`usb_claim_interface error -6`, scanner restarted with it and wrote 116 empty
`scan_runs` rows in 2 h. A V4 replug later left rtl_tcp alive with a dead
handle (`Failed to submit transfer 0`); the watchdog caught it as starvation
and restarted after 2 ticks.

Fix: identity by serial (`rtl_tcp -d <serial>`), udev `SYSTEMD_USER_WANTS`
starts the unit on plug, per-serial `BindsTo=` drop-in stops it on unplug,
watchdog skips while `/dev/rtl_sdr_<serial>` is missing. See
`spectrum/docs/dongle_identity.md`, section "Plug and play".
