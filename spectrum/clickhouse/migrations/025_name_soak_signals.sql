-- Migration 025: name the persistent signals from the 2026-09-28 soak (V3,
-- patio antenna) that fell into unknown_* or were mislabelled.
--
-- 420-430 MHz carriers: an IQ capture at 425.2 MHz (2026-09-28, 2 s at
-- 2.048 MS/s) showed continuous carriers 19.5-21 kHz wide on a 25 kHz raster
-- with a 14.2 ms envelope period, TETRA's 14.17 ms timeslot: TETRA base
-- stations in the civil/PAMR downlink band. The tetra class requires a
-- tetra_gr allocation, so the band gets one.
--
-- 390-393 MHz carriers: continuous in 89-98 % of sweeps inside the existing
-- tetra_gr allocation; public-safety TETRA downlinks. Positions come from the
-- scanner's 100 kHz bins (about +/-50 kHz); the band is encrypted and was not
-- IQ-captured.
--
-- 144.800 MHz: the IARU Region 1 APRS frequency (AX.25 packets, 1200 Bd AFSK),
-- added with its own class. The operator-confirmed voice entry at 144.775
-- ("Greek 2m Ham", listening_log 2026-04-02) stays: both are real. The
-- scanner's 100 kHz bins cannot separate them 25 kHz apart, and the
-- operator confirmation overrides every bin within 150 kHz of 144.775.
--
-- Not named: a persistent peak at 116.82 MHz. It falls about 2.6 dB per dB of
-- gain (a real carrier falls 1:1), so it is FM intermodulation inside the
-- overloaded, bandstop-less V3, not a navaid.
--
-- Every statement is guarded, so a re-run changes nothing.

INSERT INTO spectrum.allocations (freq_start_hz, freq_end_hz, service, region, source, notes)
SELECT 420000000, 429999999, 'tetra_gr', 'GR', 'obs',
       'Civil/PAMR TETRA downlink, observed 2026-09-28 (20 kHz carriers, 14.17 ms slots). Do not decode.'
WHERE (SELECT count() FROM spectrum.allocations WHERE freq_start_hz = 420000000) = 0;

INSERT INTO spectrum.signal_classes
    (class_id, name, bw_min_hz, bw_max_hz, modulation, duty_pattern, burst_min_s, burst_max_s, evidence_rules)
SELECT 'aprs_packet', 'APRS packet (AX.25, 1200 Bd AFSK)', 12500, 16000, 'AFSK', 'bursty_low', 0.3, 2,
       '{"bw_hz":[200000,500000],"duty_pattern":["bursty_low"],"duty_24h_range":[0.001,0.2],"center_freq_hz_near":[144800000],"center_freq_tolerance_hz":60000,"requires_allocation_in":["amateur_2m"]}'
WHERE (SELECT count() FROM spectrum.signal_classes WHERE class_id = 'aprs_packet') = 0;

INSERT INTO spectrum.known_frequencies (freq_hz, bandwidth_hz, name, class_id, modulation, notes)
SELECT freq_hz, bandwidth_hz, name, class_id, modulation, notes FROM (
    SELECT 144800000 AS freq_hz, 12500 AS bandwidth_hz, 'APRS (IARU Region 1)' AS name,
           'aprs_packet' AS class_id, 'AFSK' AS modulation,
           'AX.25 packet position reports; 25 kHz from the confirmed voice at 144.775' AS notes
    UNION ALL SELECT 424800000, 25000, 'TETRA BS 424.800 (civil)', 'tetra', 'pi/4DQPSK', 'IQ capture 2026-09-28: continuous, 14.2 ms slots'
    UNION ALL SELECT 425100000, 25000, 'TETRA BS 425.100 (civil)', 'tetra', 'pi/4DQPSK', 'IQ capture 2026-09-28: continuous, 14.2 ms slots'
    UNION ALL SELECT 425425000, 25000, 'TETRA BS 425.425 (civil)', 'tetra', 'pi/4DQPSK', 'IQ capture 2026-09-28: continuous, 14.2 ms slots'
    UNION ALL SELECT 425650000, 25000, 'TETRA BS 425.650 (civil)', 'tetra', 'pi/4DQPSK', 'IQ capture 2026-09-28: continuous, 14.2 ms slots'
    UNION ALL SELECT 390500000, 25000, 'TETRA BS ~390.5 (public safety)', 'tetra', 'pi/4DQPSK', 'Scanner bins +/-50 kHz; encrypted, not captured'
    UNION ALL SELECT 391450000, 25000, 'TETRA BS ~391.45 (public safety)', 'tetra', 'pi/4DQPSK', 'Scanner bins +/-50 kHz; in 98 % of sweeps'
    UNION ALL SELECT 391750000, 25000, 'TETRA BS ~391.75 (public safety)', 'tetra', 'pi/4DQPSK', 'Scanner bins +/-50 kHz; in 89 % of sweeps'
    UNION ALL SELECT 392600000, 25000, 'TETRA BS ~392.6 (public safety)', 'tetra', 'pi/4DQPSK', 'Scanner bins +/-50 kHz; encrypted, not captured'
) AS seed
WHERE seed.freq_hz NOT IN (SELECT freq_hz FROM spectrum.known_frequencies);
