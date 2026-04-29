# Phase 4 — research roadmap

Open research questions and enhancement ideas, consolidated 2026-04-29 from five `20260421_*` followups. Each section is self-contained — pick whichever feels timely.

These items are *deliberately on the back burner*: the production pipeline runs without any of them. They're here so we don't lose the threads.

---

## 1. RANGE_HASHED dictionary over `signal_catalog`

**Question.** Promote `spectrum.signal_catalog` to a `CREATE DICTIONARY` resource (`LAYOUT(RANGE_HASHED)`) keyed on `freq_lo_hz`/`freq_hi_hz`, so `dictGet('signal_catalog_dict', 'service', (freq_hz, freq_hz))` becomes the idiomatic per-bin lookup.

**Why.** Classifier runs on a 5-min cadence; JOIN against 66 rows is instant — no urgency. But if the *scanner itself* ever wants to label bins in real time (annotate peaks during sweep), a Dictionary is the only structure that's fast enough. Dictionaries also make SQL readable — `WHERE signal_catalog_dict.service = 'marine_vhf'` reads like an API.

**Sketch:**
```sql
CREATE DICTIONARY IF NOT EXISTS spectrum.signal_catalog_dict (
    freq_lo_hz UInt32, freq_hi_hz UInt32,
    service String, confidence String,
    modulation_expected String, notes String
)
PRIMARY KEY freq_lo_hz
SOURCE(CLICKHOUSE(TABLE 'signal_catalog' DATABASE 'spectrum'
                  USER 'spectrum' PASSWORD 'spectrum_local'))
LAYOUT(RANGE_HASHED())
RANGE(MIN freq_lo_hz MAX freq_hi_hz)
LIFETIME(MIN 300 MAX 600);
```

**Sharp edges.** Multiple catalog rows can cover the same freq (broader allocation + specific channel) — Dictionary returns *first match*; may need an `authority_order` field. Refresh on LIFETIME expiry (~5-10 min) or `SYSTEM RELOAD DICTIONARY`.

**Revisit when:** real-time bin labeling lands in scanner; catalog grows past ~1000 entries; operator-facing queries get repetitive enough to want a `dictGet` helper.

---

## 2. `match_subtype` enum on `compression_events`

**Question.** Add a `match_subtype` enum to `compression_events` capturing whether the emitter was localised:
- `emitter_localised` — tier ≥ medium AND `estimated_emitter_freq_hz IS NOT NULL`
- `emitter_ambiguous` — tier ≥ medium AND emitter NULL (compression confirmed, source unknown)
- `spur_only` — tier = low

**Why.** Today the distinction is derived from NULL-ness of `estimated_emitter_freq_hz` — brittle for Grafana grouping. Phase 5 forensic capture should only trigger on `emitter_localised` events (need a frequency to tune to); listening-playbook entries similarly.

**Approach.** New migration adds `Enum8` column; populate in `detect_compression.py`; add a Grafana panel grouped by subtype. < 1 hour of work.

**Open design question.** Is this really a separate enum, or does "match_tier + emitter_null" already carry the same information? Lean toward explicit enum because humans skim columns, not joint conditions.

---

## 3. fs/4 spur hypothesis

**Question.** Is the +526 kHz spur pattern we see during LNA compression literally the dongle's well-known fs/4 image spur (2.048 MS/s ÷ 4 = 512 kHz; nearest 100-kHz bin is +526 kHz from tile center)?

**Why it matters.** If yes, rename signature to `fs4_spur_comb` and document the mechanism — "dongle self-spur amplified by compression," not "external emitter intermod." Compression marker is *still valid* (the spur sits at noise floor normally; only rises to -4 dBFS under LNA overload), but the naming and threshold design might tighten.

**Approach.**
1. Read rtl-sdr.com on R860/R820T2 fs/4 spur (search: "rtl-sdr fs/4 spur", "zero-IF image", "DC spike + quarter sample rate").
2. Cross-check the actual offset in ClickHouse — confirm ±500 vs ±600 kHz given `downsample_bins` center-of-bin indexing.
3. If confirmed, update `spectrum/docs/signature_detection.md`. Check whether the −174 kHz family has a similar `−fs/(n)` explanation.

No code change — detector logic is signature-agnostic; only naming/docs shift.

---

## 4. Infrastructure noise correlation

**Question.** Do the nine `medium+` compression events with −174 kHz spur offset (FM band, 1–2 dB depression, suspect cluster) correlate with leap-box scheduled jobs (cron, systemd timers, fwupd, snapper, btrfs scrub, fstrim)?

**Suspect cluster (UTC):** 2026-04-16 12:26 / 15:35; 2026-04-17 05:20 / 12:05 / 20:43 / 21:21; 2026-04-18 09:16; 2026-04-21 02:15 / 09:29.

**Why.** If "yes — Apr 21 02:15 correlates with daily backup," the detector is catching RFI from adjacent USB cable / drive / NIC, *not* external emitters. Changes the narrative from "mystery signal" to "known local noise" → suggests shielding / different USB port / dedicated power.

**Approach (5 min on leap):**
```bash
ls -la /etc/cron.daily /etc/cron.hourly /etc/cron.weekly
grep -rnH . /etc/cron.d/ 2>/dev/null
systemctl list-timers --all
journalctl --since '2026-04-17 05:15' --until '2026-04-17 05:25'
journalctl --since '2026-04-21 02:10' --until '2026-04-21 02:20'
systemctl status snapper-timeline snapper-cleanup btrfs-scrub* fstrim.timer 2>/dev/null
```

**Decision rule.** ≥3 events correlate → "we have a local RFI problem"; add an RFI mitigation followup. ≤3 → likely tuner artifact (see §3).

---

## 5. Principled threshold calibration

**Question.** Set each detector threshold at a principled quantile of the observed distribution over non-anomalous sweeps, instead of by hand from a single training event.

**Current thresholds in `detect_compression.py`** (anchored to the 2026-04-21 verified event):
- `MIN_SPUR_BLOCK_TILES = 10`
- `SPUR_OFFSET_STDDEV_MAX_HZ = 30_000`
- `SPUR_OFFSET_MIN_ABS_HZ = 100_000`
- `SPUR_MIN_MEDIAN_POWER_DBFS = -15.0`
- `DEPRESSION_MIN_DB = 5.0` (Apr 21 at 4.8 dB just misses, intentionally)

**Why.** A principled calibration might pick 4.0 dB (p99.9 of non-anomalous) or 6.5 dB (p99.99). The *right* answer is determined by the noise distribution, not by whether we think the Apr-21 event "should" fire. False-positive rate becomes deterministic — a real SLO instead of guessing.

**Approach.** Write a one-shot `spectrum/analysis/calibrate_thresholds.py` that prints proposed thresholds; humans decide if they're reasonable. Per-signature independence matters — calibrate each sub-signal on its own distribution. Spur-block-length is the hardest (bimodal: short runs at +26 kHz DC spike vs long runs at other offsets) — may need per-offset-bucket stats.

**Don't bake into `detect_compression.py` immediately** — calibration script first, then patch constants if reasonable.

---

*Originally five separate followups opened 2026-04-21; consolidated 2026-04-29 to keep the followups directory tight. Original files preserved in `git log spectrum/docs/followups/20260421_*.md`.*
