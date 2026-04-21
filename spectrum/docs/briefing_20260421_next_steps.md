# Briefing — spectrum monitor state & next steps
*Prepared 2026-04-21 for outside expert review.*

This is a self-contained briefing. The reader doesn't need prior context from the session that produced it — everything needed to form an opinion on "what should we do next" is below, organised for scanning.

---

## 1. What this project is

Hobby RTL-SDR station in Polygono (central Athens, ground floor, rooftop-tripod antenna 3 m, 57 cm dipole arms, SCAN_GAIN=12 dB → adaptive-reduced to SCAN_GAIN_MIN=2 dB). Goal: passive monitoring of 88–470 MHz with an observability-stack mindset (owner runs Kubernetes, Grafana, VictoriaMetrics, ClickHouse professionally).

- **Hardware**: RTL-SDR Blog V3 (R860 tuner, RTL2832U, 8-bit ADC, ~50 dB dynamic range). 2.048 MS/s max stable.
- **Deployment**: Scanner container on a dedicated openSUSE Leap box (`192.168.2.10`, "leap"). rtl_tcp runs on leap; the scanner opens/closes the connection per sweep. ClickHouse + Grafana + ingest all in docker-compose on the same box.
- **Data volume**: 16 days of baseline, 11.7 M scan rows, 2,441 full sweeps (88–470 MHz, 100 kHz bins), ~200 full sweeps/day + 1,440 airband (118–137 MHz) sweeps/day. 180-day TTL.

## 2. Existing pipeline (shipped before today)

A 3-stage classifier pipeline runs on systemd timers:

```
scanner.py ──► scan_ingest.py ──► ClickHouse
   │                                 │
   │                         ┌───────┴──────────────┐
   │                         ▼                      ▼
   │              feature_extractor.py      classifier_health.py
   │              (*:00/5 min)               (*:00/5:45)
   │                         │
   │                         ▼
   │                 classifier.py
   │                 (*:00/5:30)
   ▼
scan_runs, sweep_health, peaks, events, scans
```

- **scanner.py** (numpy + stdlib): connects rtl_tcp, FFT per tile, 186 tiles × 12.15 ms ≈ 2,261 ms/sweep. Emits JSON lines to stdout for ingest. **One timestamp per sweep** — all 3917 bins share a single `timestamp`. ADC clipping detected by raw-byte inspection per tile; only worst tile's stats retained (no per-tile table).
- **scan_ingest.py** (pure stdlib): routes JSON to MergeTree tables.
- **feature_extractor.py**: per-bin duty cycles (1h/24h/7d), burst p50/p95, diurnal/weekday patterns, bandwidth FWHM-like, harmonic detection. Writes `peak_features`.
- **classifier.py**: 10 canonical `signal_classes` (broadcast_fm, am_airband_atc/atis, nfm_voice_repeater, marine_vhf_channel, ais, dvbt_mux, tetra, 2× unknown buckets). Rule-based evidence scoring per class. Operator-confirmation override via `listening_log`. Writes `signal_classifications`.
- **classifier_health.py**: emits per-run regression sentinels (confidence_precision_tail, harmonic_cross_allocation, continuous_with_bursts, class_distribution_json, known-good-passing).

**Existing reference tables:**
- `known_frequencies` (35 rows, Athens-specific, doubles as classifier prior)
- `signal_classes` (10 rows, canonical targets with evidence_rules JSON)
- `allocations` (12 rows, broad service bands)
- `hourly_baseline` (AggregatingMergeTree MV: freq × hour → avg_power, std_power)
- `listening_log` (operator notes; was 1 row, 5 after today's backfill)

4 Grafana dashboards already exist: `spectrum-overview`, `spectrum-health`, `listening-playbook`, `run-comparison`.

## 3. The incident that motivated this work

**2026-04-21 11:55:04.960 UTC**: a ~0.93 s pulsed emission near 304.19 MHz compressed the RTL-SDR LNA. Signature:

- One full sweep captured it; adjacent sweeps (5 min before/after) and airband sweep 15 s later are all normal.
- Within the sweep: 27 consecutive tiles (170–226 MHz + 300–316 MHz range) showed peaks at a fixed **+526 kHz offset** from each tile center — an intermod spur comb.
- Real emitter peak at **304.19 MHz @ +3.5 dBFS** (ADC-rail). Normal bin there is −46 dBFS.
- FM broadcast carriers dropped 10–12 dB during the event (LNA compression visible in normally-strong bands).
- Baseline event rate: ~0.75 compression-like events/day by the raw `worst_clip_freq outside FM` proxy.

The pipeline flagged the anomaly but could not further classify the emitter: no IQ was retained, and peak frequency ± 50 kHz with unknown modulation is insufficient to identify.

## 4. What we built today

All of this is already applied on leap and committed-ready in the repo.

### Phase 1 — Expected-signals catalog (shipped)
- `migrations/011_add_signal_catalog.sql` — new table `signal_catalog` + a view `signal_catalog_view` that computes `confirmed_here` from known_frequencies.
- Seed: 66 entries across FM, aviation nav, aviation voice, amateur 2m/70cm, gov/military VHF, maritime VHF (ITU-R M.1084 channels 1/6/13/16/70 + coast + AIS 87B/88B), DVB-T Band III (5 Hymettus muxes), mil UHF aero (225–400 incl. **303–305 MHz mystery zone** marked `confidence='unknown'`), TETRA, ISM 433, PMR446 (all 16 channels + 446.21 observed), NOAA APT satellites.
- `migrations/014_backfill_listening_log.sql` — ingested 4 operator observations from `notes/signal-log.txt` that had never reached the DB (TETRA 384, PMR 446.21, Ham 144.775, Marine Ch13 156.65).
- `docs/signal_catalog_sources.md` — explains the schema, confidence tiers, citation conventions, and why we kept `signal_catalog` distinct from `known_frequencies` (the latter is a classifier prior; the former is reference taxonomy).

### Phase 2 — Compression archaeology (shipped)
- `migrations/012_add_compression_events.sql` — new table with 3 independent sub-flags (`sig_spur`, `sig_baseline`, `sig_clip`), aggregated into `match_tier ∈ {none,low,medium,high}`.
- `spectrum/analysis/detect_compression.py` (stdlib + numpy, 556 lines): reads scans + sweep_health + hourly_baseline, applies the 3-part signature, writes events. CLI modes: `--backfill`, `--since`, `--sweep`, `--dry-run`.

**Signature definitions:**
1. **sig_spur**: ≥10 consecutive tiles whose argmax-bin offset from tile center is within 30 kHz stdev, absolute offset ≥ 100 kHz (excludes DC-spike baseline), median peak power ≥ −15 dBFS (excludes low-amplitude baseline spurs).
2. **sig_baseline**: median attenuation of known-strong carrier bins (hourly_baseline > −35 dBFS, i.e. FM + DVB-T) vs **previous-hour** baseline ≥ 5 dB. Using previous-hour avoids self-pollution (a single 2 s compressed sweep contaminates ~20% of its own hour's 5-sweep average).
3. **sig_clip**: `sweep_health.worst_clip_freq_hz` outside 170–230 MHz AND `clipped_captures > 0`. Quick proxy, doesn't require IQ.

**Backfill result over 2,441 full sweeps / 16 days:**
| Tier | Count | % |
|---|---|---|
| none | 2,357 | 96.6% |
| low (1 sig) | 73 | 2.99% |
| medium (2 sigs) | 10 | 0.41% |
| high (all 3 sigs) | 1 | 0.04% |

**11 medium+ events** in the period. Full list with Athens-local interpretations:

| UTC | Tier | Sigs | Emitter MHz | Power dBFS | Spur offset | Spur-block span | Carrier depression |
|---|---|---|---|---|---|---|---|
| 2026-04-16 12:26:00 | **high** | 1,1,1 | 167.374 | +2.1 | −174 kHz | FM band tiles 0–11 | 5.2 dB |
| 2026-04-16 15:35:56 | medium | 1,0,1 | 171.470 | −3.9 | −174 kHz | 2–11 | 1.6 dB |
| 2026-04-17 05:20:15 | medium | 1,0,1 | 165.326 | −3.8 | −174 kHz | 2–11 | 1.5 dB |
| 2026-04-17 12:05:32 | medium | 1,0,1 | 169.422 | −3.3 | −174 kHz | 0–11 | 1.8 dB |
| 2026-04-17 20:43:43 | medium | 1,0,1 | 163.278 | −3.6 | −174 kHz | 2–11 | 1.8 dB |
| 2026-04-17 21:21:29 | medium | 1,0,1 | 167.374 | −3.3 | −174 kHz | 1–11 | 1.5 dB |
| 2026-04-18 09:16:02 | medium | 1,0,1 | 169.422 | −3.4 | −183 kHz | 2–12 | 2.1 dB |
| 2026-04-19 14:03:53 | medium | 1,1,0 | 166.874 | −4.3 | **+526 kHz** | **45–54** (113–133 MHz) | **8.6 dB** |
| 2026-04-21 02:15:36 | medium | 1,0,1 | 167.374 | −3.0 | −174 kHz | 1–12 | 1.3 dB |
| 2026-04-21 09:29:02 | medium | 1,0,1 | 165.326 | −3.0 | −174 kHz | 2–12 | 1.3 dB |
| 2026-04-21 11:55:04 | medium | 1,0,1 | **304.190** | **+3.5** | **+526 kHz** | **40–66** (170–226 MHz) | 4.8 dB |

### Phase 3, 4, 5 — design docs shipped, no code changes

- `docs/signature_detection.md`: live-detector design for compression (reuse Phase 2), narrow-persistent-carrier emergence (hysteresis on `peak_features.duty_cycle_1h`), transient narrow burst (**view, not detector** — joins existing `events` + `signal_classifications` + `compression_events`). Rtl_tcp concurrency verification is called out as a Phase-3 gate before any Phase-5 work.
- `docs/temporal_resolution.md`: new `scan_tiles` table (one row per tile per sweep) with per-tile timestamps, clip fractions, gain, and IQ-blob pointer. Migration 015. Scanner + ingest changes sketched. Storage: ~20 MB compressed total. Rationale: do NOT ALTER `scans` (21× replication per tile); asymmetric columns will grow.
- `docs/forensic_capture.md`: single-dongle priority-queue with **flock on `/run/rtl_tcp.lock`**, rate-limiting (max 3/hour), drop-on-floor if queue > 1 deep, disk rotation (2 GB cap on iq_captures/), manual SDR++ analysis downstream. Second-dongle sketch deferred until event rate demands it.

## 5. What we believe vs what we know

### Confident findings
- The Apr 21 11:55 event is a real wideband compression from a strong pulsed emitter near 304 MHz. Supported by direct inspection plus 3-signature detector agreement (medium tier).
- `signal_catalog` is semantically distinct from `known_frequencies` and should stay so. Merging would flood the classifier prior with ~200 rows, destabilising scoring.
- The existing classifier pipeline is mature. We extended it; we did not rebuild it.

### Plausible but unverified
- **2026-04-16 12:26** (high tier, 167 MHz, 5.2 dB depression) is the cleanest archaeological compression event we have. Carrier depression is real.
- **2026-04-19 14:03** (166.87 MHz, 8.6 dB depression) is the biggest depression on record. Likely real.
- There may be a recurring real emitter at **166–167 MHz** — 6 of 11 detected events point to this frequency, and it sits inside a documented Athens repeater cluster (164.73 / 166.77 / 168.82 MHz; see `notes/analysis-report-20260410.md`).

### Known unknowns
- **The −174 kHz spur family is unexplained.** 9 of 11 detected events have a spur comb at −174 kHz offset in FM-band tiles (0–12, i.e. 88–113 MHz) and report "emitter" frequencies at 163.28 / 165.33 / 167.37 / 169.42 / 171.47 MHz — exactly tile-spacing-aligned (2.048 MHz apart). This means the emitter estimator is picking **+526 kHz offset peaks in tiles adjacent to the spur block**, not real emissions. When two spur families co-exist in one sweep, the current code can't tell them apart without IQ.
- **Why does the −174 kHz pattern appear at all?** Is it an image from the R860's mixer/LO? A consistent DSP artifact of the specific rtl_tcp build? A response to specific nearby FM stations? We don't know.
- **The 303–305 MHz mystery.** One unambiguous event (Apr 21 +3.5 dBFS), one weaker prior at 303.89 MHz (Apr 12, +2.9 dBFS, but below the 10-tile threshold so not in `compression_events`). Band is NATO UHF aero (225–400 MHz). Hypothesis: close-passing military aircraft. Unverifiable without IQ.

## 6. Concrete constraints worth remembering

- **Single dongle**, shared via rtl_tcp. Only one pipeline runs at a time. Current state is: **spectrum pipeline is the steady tenant**; ADS-B, AIS, ISM pipelines exist in the repo but are not running concurrently.
- **Stdlib-only Python in the production path.** Scanner uses numpy (image already has it). feature_extractor, classifier, classifier_health use only urllib + stdlib. New code should follow the same rule unless there's a hard reason.
- **Boring > clever.** This is a hobby project. A correct thing that takes three weekends loses to a boring thing that ships next Saturday. `match_tier` is a count, not a calibrated probability, deliberately.
- **No ground truth for historical events.** We have one verified compression event (2026-04-21 11:55). Everything older is heuristic. The detector is honest about this — sub-flags stored independently, no fabricated probability.
- **All migrations recorded in `schema_migrations`** manually (011, 012, 014) so the next scanner-image rebuild won't re-apply. Migration 013 reserved for the Phase-3(c) transient-burst view (design only, SQL in `signature_detection.md`).

## 7. Where we'd welcome expert input

These are the specific questions where an outside reviewer can help. None of them block anything — we have a working system that detects events and a reasonable roadmap. These are improvements.

### 7.1 Is the −174 kHz "spur family" a real phenomenon or a detector ghost?

The 8 medium-tier events with a −174 kHz spur block in FM tiles might be:
- (a) **Genuine but mild LNA compression** from strong FM broadcast coupled with some other source. The depression signal is 1–2 dB, so it's subtle.
- (b) **A tuner artifact** (image, LO leak, spurious response of the R860) that happens whenever FM is present. If so, `sig_spur` should refuse to fire on this offset specifically.
- (c) **A real but different phenomenon** than Apr 21's compression — e.g. near-field coupling from a nearby digital device. The recurrence (at similar times across days) is suspicious.

An expert with RTL-SDR / R820T2/R860 frontend knowledge could likely eliminate or confirm (b) at a glance. If it's a tuner artifact, we should filter it out and re-run the archaeology.

### 7.2 Should we tune signature thresholds?

Current values are calibrated from one event:
- `MIN_SPUR_BLOCK_TILES = 10` (Apr 21 had 27)
- `SPUR_OFFSET_STDDEV_MAX_HZ = 30_000`
- `SPUR_OFFSET_MIN_ABS_HZ = 100_000`
- `SPUR_MIN_MEDIAN_POWER_DBFS = −15.0`
- `DEPRESSION_MIN_DB = 5.0`

The Apr 21 event's baseline depression is 4.8 dB — barely below the 5 dB threshold. If we dropped to 4 dB, Apr 21 becomes "high" tier. We haven't because we don't want to overfit to the one training example. Is there a principled way to set this threshold without bias-corrupting the one signal?

### 7.3 Phase 4 vs Phase 5 build order

**Phase 4** (per-tile timestamps + cheap event-triggered IQ save): gives us ~12 ms temporal resolution instead of 2.3 s, plus ~75 KB IQ saved automatically when any bin hits > 0 dBFS. Storage cost ~20 MB/year. Modifies scanner.py + scan_ingest.py.

**Phase 5** (forensic mode: triggered 2 MHz × 5 s IQ capture at estimated emitter center): gives us 20 MB of usable IQ per trigger for SDR++ analysis. Requires flock coordination with sweeper; stops baseline scanning briefly per trigger.

Our instinct is **Phase 4 first, then Phase 5**. Reasons:
- Phase 4 doesn't steal from the scanner; Phase 5 does.
- Phase 4 ALSO produces IQ (the cheap save), so a subset of the Phase 5 benefit comes for free.
- If Phase 4 reveals that the −174 kHz spur is a tuner artifact, Phase 5 scope might change.

Is there a reason to prefer Phase 5 first? Or a combined migration?

### 7.4 Signal catalog structure — did we get it right?

We kept `signal_catalog` separate from `known_frequencies` on the grounds that merging would pollute the classifier prior. The catalog is deliberately skewed toward high-confidence ITU/ECC/ITU-R/AIP-Greece citations; observed-here signals are in `known_frequencies`. Cross-reference is via the `signal_catalog_view` (LEFT JOIN). Is there a ClickHouse-idiomatic way (Dictionary with RANGE_HASHED?) to make "what service covers this freq?" a single `dictGet` call at classifier-scoring time? Would reducing that to O(1) lookup unlock anything in `classifier.py`?

### 7.5 303–305 MHz mystery — what else can we do without IQ?

Given that:
- Two events at 303.89 (Apr 12) and 304.19 (Apr 21) MHz, both +2.9 to +3.5 dBFS,
- Band is NATO UHF aero,
- Athens has heavy military air traffic via LGAV approaches,

...are there **non-IQ** investigation avenues? Some ideas we have:
- Cross-reference Apr 12 / Apr 21 event times with publicly-available military aircraft ADS-B (some mil aircraft do broadcast — or cross-reference with known military exercise dates).
- Set `SCAN_FREQ_END=470000000` unchanged but also run a narrow airband-analogous sweep of just 295–315 MHz every 30 s, so a future event has higher chance of being caught.
- Correlate with meteorological/atmospheric conditions (ducting?).

We're not expecting an answer without IQ, but would appreciate a sanity-check: are we missing a cheap investigation avenue?

### 7.6 Anything we're overlooking for live operations?

With Phase 1–2 live and Phases 3–5 still in design, where does the pipeline break first under real load? Candidates:
- Compression detector systemd timer running every 5 min with no back-pressure if ClickHouse is slow
- Cache-miss on `hourly_baseline` if ClickHouse restarts
- Rate-limiting assumes trigger inserter respects it; nothing enforces it at the DB level

What else?

## 8. Files an expert might want to read

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Project-wide context and hardware profile |
| `spectrum/scanner.py` | Sweep geometry, clipping detection |
| `spectrum/clickhouse/init.sql` | Base schemas for scans/peaks/events/sweep_health/scan_runs |
| `spectrum/clickhouse/migrations/003_add_classifier_tables.sql` | allocations/signal_classes seed |
| `spectrum/clickhouse/migrations/011_add_signal_catalog.sql` | Today's catalog |
| `spectrum/clickhouse/migrations/012_add_compression_events.sql` | Today's events schema |
| `spectrum/analysis/detect_compression.py` | Detector logic |
| `spectrum/analysis/compression_events.md` | Archaeology findings |
| `spectrum/docs/signature_detection.md` | Phase-3 live detector design |
| `spectrum/docs/temporal_resolution.md` | Phase-4 per-tile timing design |
| `spectrum/docs/forensic_capture.md` | Phase-5 forensic IQ capture design |
| `spectrum/docs/unknowns_pool_diagnostic_20260418.md` | Prior diagnosis — 744 unclassified bins, 28% mislabeled |
| `spectrum/classifier.py` | Rule-based scoring logic with evidence_rules JSON |
| `notes/analysis-report-20260410.md` | 5-day inventory identifying 164–168 MHz Piraeus cluster |

## 9. Running commands (read-only)

Query `compression_events` directly:
```sql
SELECT timestamp, match_tier, estimated_emitter_freq_hz/1e6 AS mhz,
       estimated_emitter_power_dbfs AS dbfs,
       spur_offset_hz/1000 AS off_khz, baseline_depression_db AS dep
FROM spectrum.compression_events
WHERE match_tier IN ('medium', 'high')
ORDER BY timestamp
```

Re-run archaeology (~10 min):
```bash
cd ~/dev/rf_luv/spectrum
python3 analysis/detect_compression.py --backfill --dry-run --min-tier low
```

Look up catalog for a given frequency:
```sql
SELECT service, confidence, notes
FROM spectrum.signal_catalog_view
WHERE freq_lo_mhz <= <MHZ> AND freq_hi_mhz >= <MHZ>
```
