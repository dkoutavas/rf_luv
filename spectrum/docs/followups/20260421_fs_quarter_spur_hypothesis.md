# Followup — is the +526 kHz spur the RTL-SDR's own fs/4 spur?

**Opened**: 2026-04-21

## Context
Today's compression archaeology surfaced two distinct spur-offset families in the detected events:
- **+526 kHz** offset from tile center (2 events, including the verified 2026-04-21 11:55 compression at 304.19 MHz)
- **−174 kHz** offset (9 events, mostly weak, likely false-positive flavour)

The +526 kHz offset is suspiciously close to `2.048 MS/s ÷ 4 = 512 kHz`. With 100 kHz bin resolution the nearest reported bin is +526 kHz from tile center. That strongly suggests this is the **RTL-SDR's well-known fs/4 image spur**, not a genuine external-signal intermod product.

## Question
Is the +526 kHz spur pattern we see during LNA compression literally the dongle's fs/4 spur becoming visible when the LNA is overloaded? Or does a real in-band intermod product happen to land at that frequency?

## Why it matters
- If it's the fs/4 spur → our naming/docs should reflect that. The compression signature is **still valid** as a compression marker (the spur is normally at noise floor; it only rises to -4 dBFS when the LNA is overloaded), but the mechanism is "dongle self-spur amplified by compression" not "external emitter intermod at tile center + 526 kHz."
- Also affects the threshold design: if the spur is a product of the dongle + current signal envelope, its magnitude relative to spur_block_median may be predictable, enabling a cleaner detector.

## Approach
1. Read `rtl-sdr.com` literature on the R860 / R820T2 fs/4 spur (search terms: "rtl-sdr fs/4 spur", "zero-IF image", "DC spike + quarter sample rate").
2. Cross-check the actual offset in ClickHouse: at 2.048 MS/s sample rate, fs/4 = 512_000 Hz exactly. With 100 kHz bins and the scanner's `downsample_bins` (center-of-bin indexing), the nearest reported bin offset from tile center should be 500 or 600 kHz. Confirm which.
3. If confirmed, update `spectrum/docs/signature_detection.md` to name this `fs4_spur_comb` and note the mechanism explicitly.
4. Check whether the −174 kHz family has a similar explanation — e.g. `−fs/(n)` or a known R860 image.

## Expected outcome
A two-paragraph note appended to `signal_catalog_sources.md` (or its own doc) explaining the fs/4 spur and how it shows up in our data. No code change required — the detector logic is signature-agnostic; only the documentation/naming shifts.
