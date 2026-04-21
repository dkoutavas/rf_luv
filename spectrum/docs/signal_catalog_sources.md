# signal_catalog — structure and sources

The `spectrum.signal_catalog` table (migration 011) is the **expected-signals reference** for 88–470 MHz at the Polygono/Athens receiver. It is deliberately distinct from `known_frequencies`:

| Table | Role | Cardinality | Consumer |
|---|---|---|---|
| `known_frequencies` | **Classifier prior** — each row biases `classifier.py` scoring via `min_confidence` | ~35 | `classifier.py`, `classifier_health.py` |
| `signal_catalog` | **Reference taxonomy** — citation-backed "what could be at this frequency?" | ~66 (expandable) | humans, Phase 2 archaeology, Phase 5 forensic-target selection |

Merging them would either flood the classifier with non-prior rows or require an `is_prior BOOL` gate that becomes a de facto two-table join. The split is intentional.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `id` | UInt32 | Numeric ID; gaps are intentional, reserved for future sibling groups (e.g. 30-49 VHF gov, 50-69 marine) |
| `freq_lo_hz`, `freq_hi_hz` | UInt32 | Lower/upper bounds of the allocation or channel |
| `freq_center_hz`, `freq_span_hz` | UInt32 | Convenience `(lo+hi)/2` and `hi-lo` |
| `service` | String | Short service tag — e.g. `broadcast_fm`, `maritime_vhf_ch16`, `mil_uhf_aero` |
| `allocation_source` | String | Citation — see [Citation conventions](#citation-conventions) |
| `modulation_expected` | String | `WFM` / `AM` / `NFM` / `OFDM` / `GMSK` / `pi/4DQPSK` / `APT` / `mixed` / `unknown` |
| `typical_duty` | String | `continuous` / `bursty_low` / `bursty_high` / `tdma` / `pulsed` / `unknown` |
| `confidence` | String | See [Confidence tiers](#confidence-tiers) |
| `notes` | String | Free-form human notes, including Polygono-specific context |

`spectrum.signal_catalog_view` exposes `freq_*_mhz` columns (human-scale) and a computed `confirmed_here BOOL` that is true when at least one row in `known_frequencies` falls inside this catalog row's span.

## Confidence tiers

| Tier | Meaning | When to use |
|---|---|---|
| `high` | ITU/CEPT binding regulation **or** locally observed/verified | ITU RR frequency assignments, CEPT ERC/DEC decisions, observed Greek broadcasters, confirmed ATC frequencies from AIP Greece that we've also seen in scans |
| `medium` | Regional convention, widely published, but not verified at this receiver | ECC recommendations, IARU bandplans, ITU-R M.1084 maritime channels (individual channels before verification) |
| `low` | Inferred from public sources, **needs cross-check with AIP/EETT** | Secondary ATC sectors, pager bands we haven't observed, legacy service bands |
| `unknown` | Mystery zones flagged for investigation | 303–305 MHz after the Apr 12 and Apr 21 events |

**Training-data-derived frequencies are NOT trusted.** Prior Claude-supplied LGAV ATC frequencies were wrong and had to be corrected by scanning. Only Tower (118.1), Approach (118.575), ATIS (136.125), and Guard (121.5) are in the `high` tier for aviation voice — everything else in that band has confidence `low` with a note to cross-check AIP Greece.

## Citation conventions

| `allocation_source` value | Meaning |
|---|---|
| `ITU RR 5.NNN` | International Radio Regulations, footnote 5.NNN (e.g. `5.208` for NOAA APT) |
| `ECC` / `ECC/DEC/(YY)NN` | CEPT ECC regulations (Electronic Communications Committee) |
| `CEPT ERC/REC 70-03 annex N` | CEPT recommendation (e.g. annex 1 = ISM 433) |
| `ICAO Annex 10` | International Civil Aviation Organization radiotelephony annex |
| `IARU R1 Bandplan 2022` | Amateur radio Region 1 bandplan (ham bands) |
| `ITU-R M.1084 Appendix 18` | Maritime VHF channel assignments |
| `ITU-R M.1371` | AIS (Automatic Identification System) |
| `AIP Greece GEN 3.4` | Aeronautical Information Publication — Greece, general/comms section |
| `EETT national plan` / `EETT / ...` | Greek EETT (Hellenic Telecommunications & Post Commission) national frequency plan |
| `NATO AFIC` | NATO Allied Frequency Information Catalogue (mil UHF) |
| `observed` | Verified locally on this receiver (either in `known_frequencies` or `signal-log.txt`) |

## Seeding status

Initial seed (2026-04-21): **66 entries** covering:

- FM broadcast (88–108) — 3 entries
- Aviation nav (108–118) — 2 entries (VOR, ILS loc)
- Aviation voice (118–137) — 7 entries (Tower, Approach, ATIS, Guard confirmed; rest `low`)
- Amateur 2m (144–146) — 4 entries
- Gov/military VHF (146–156) — 9 entries including observed repeaters at 146.39/148.44/150.49 and the 164.73/166.77/168.82 cluster
- Maritime VHF (156–162.025) — 11 entries (Ch01/06/13/16/70, coast stations, AIS-1/AIS-2)
- Land mobile safety (162.05–174) — 2 entries
- DVB-T Band III (174–230) — 6 entries (5 Hymettus muxes + band allocation)
- Mil UHF aero (225–400) — 3 entries including **303–305 MHz mystery zone**
- TETRA (380–400 + 410–430) — 2 entries
- Amateur 70cm (430–440) — 1 entry
- ISM 433 — 2 entries
- PMR446 (446.0–446.2) — 10 entries (full 8 CEPT channels + the 446.21 observed channel)
- NOAA APT satellites — 3 entries
- UHF land mobile (450–470) — 1 entry

This is starting coverage. Gaps deliberately left blank for future expansion (e.g. DAB+ Block 10, narrower ACARS channels, amateur repeater sub-bands, EETT-specific mil UHF sub-allocations if they ever become public).

## Adding new entries

Use a follow-up migration (`NNN_extend_signal_catalog.sql`) with an `INSERT INTO ... SELECT ... WHERE id NOT IN (SELECT id FROM signal_catalog)` pattern so re-runs are idempotent. Cite source in `allocation_source`. When an entry becomes locally confirmed, add a row to `known_frequencies` — `confirmed_here` in the view will flip to true automatically.

## Known limitations

- **EETT national plan** is not well-documented in English. Several entries cite `EETT national plan (general)` where specific frequencies within the band aren't publicly enumerated. Cross-check with the EETT Regulatory Framework document if you need specifics.
- **Greek mil UHF** is catalogued only as broad range (225–400 MHz NATO UHF aero). Specific sub-allocations aren't public. The 303–305 MHz mystery zone is the most interesting unresolved sub-band.
- **Aviation sector frequencies beyond Tower/ATIS/Approach/Guard** are flagged `low` with a note. Do not trust Claude-derived sector frequencies — cross-check AIP Greece.
- **LGAV full ATC picture** (Clearance Delivery, Ground, Departure, multiple sector frequencies) awaits reading AIP Greece GEN 3.4 or similar.
