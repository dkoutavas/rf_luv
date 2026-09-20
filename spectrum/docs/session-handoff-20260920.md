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
`127.0.0.1:1235` for live listening while the scanner runs on V4 :1234. The
watchdog detects non-loopback TCP clients and backs off. QUICKREF.md updated
with the SDR++ path as primary; the stale `rtl_fm -d tcp:` syntax removed
(does not work on rtl-sdr v2.0.3).

### Shell scripts

`scripts/{airband,ais,ism,satellite}*.sh` use direct USB, not rtl_tcp. A
caveat block added to each: stop `rtl-tcp@v3-01` first, or use SDR++ instead.

### What was not done

- Listening Playbook and Run Comparison dashboards not opened (need the GUI).
- Logging form test entry not written (need the browser).
- `ops/install-trip-hardening.sh` not run (escalator, probes, notify).
  These are the remaining ops-layer installers from RESTORE.md step 8.
