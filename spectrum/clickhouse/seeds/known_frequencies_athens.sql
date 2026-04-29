-- Athens-area known_frequencies seed for the spectrum.known_frequencies table.
--
-- Loaded once after `docker compose up -d` to bias the classifier toward known
-- transmitters in the Polygono / Athens region. For other locations, copy this
-- file to `known_frequencies_<your_location>.sql`, replace the rows, and load
-- via:
--
--     cat spectrum/clickhouse/seeds/known_frequencies_<your_location>.sql \
--       | docker exec -i clickhouse-spectrum clickhouse-client \
--           --user spectrum --password spectrum_local
--
-- Idempotent: the WHERE clause guards against double-insertion if the table
-- is already populated.

INSERT INTO spectrum.known_frequencies (freq_hz, bandwidth_hz, name, class_id, modulation, notes)
SELECT * FROM (
    SELECT 99600000 AS freq_hz, 200000 AS bandwidth_hz, 'Kosmos FM 99.6' AS name, 'fm' AS class_id, 'WFM' AS modulation, 'Strong local FM' AS notes
    UNION ALL SELECT 105800000, 200000, 'Skai 105.8', 'fm', 'WFM', 'Strong local FM'
    UNION ALL SELECT 118100000, 8333, 'Athens Tower', 'airband', 'AM', 'Airport tower'
    UNION ALL SELECT 118575000, 8333, 'Athens Approach', 'airband', 'AM', 'ATC approach control'
    UNION ALL SELECT 121500000, 8333, 'Guard / Emergency', 'airband', 'AM', 'International distress'
    UNION ALL SELECT 136125000, 8333, 'Athens ATIS', 'airband', 'AM', 'Automated weather'
    UNION ALL SELECT 137100000, 50000, 'NOAA 19 / Meteor M2-3', 'satcom', 'APT', 'Weather satellite'
    UNION ALL SELECT 137620000, 50000, 'NOAA 15', 'satcom', 'APT', 'Weather satellite'
    UNION ALL SELECT 137912500, 50000, 'NOAA 18', 'satcom', 'APT', 'Weather satellite'
    UNION ALL SELECT 156800000, 25000, 'Marine Ch16', 'marine', 'NFM', 'Distress/calling'
    UNION ALL SELECT 161975000, 25000, 'AIS Ch87', 'marine', 'digital', 'Ship positions'
    UNION ALL SELECT 162025000, 25000, 'AIS Ch88', 'marine', 'digital', 'Ship positions'
    UNION ALL SELECT 384000000, 25000, 'Greek TETRA', 'tetra', 'digital', 'Emergency services'
    UNION ALL SELECT 144775000, 12500, 'Greek 2m Ham', 'ham', 'NFM', 'Observed voice conversation'
    UNION ALL SELECT 148000000, 200000, 'Military/Gov VHF', 'gov', 'NFM', 'Strong persistent signal'
    UNION ALL SELECT 150500000, 200000, 'Military/Gov VHF', 'gov', 'NFM', 'Strong persistent signal'
    UNION ALL SELECT 152500000, 200000, 'Business Radio', 'business', 'NFM', 'Commercial repeater'
    UNION ALL SELECT 156050000, 25000, 'Marine Ch1', 'marine', 'NFM', 'Port operations - Piraeus'
    UNION ALL SELECT 156650000, 25000, 'Marine Ch13', 'marine', 'NFM', 'Bridge-to-bridge'
    UNION ALL SELECT 158080000, 25000, 'Marine Coast Stn', 'marine', 'NFM', 'Piraeus Radio coast station'
    UNION ALL SELECT 160130000, 25000, 'Marine Coast TX', 'marine', 'NFM', 'Coast station duplex TX'
    UNION ALL SELECT 160730000, 25000, 'Marine Coast Rpt', 'marine', 'NFM', 'Piraeus coast radio repeater'
    UNION ALL SELECT 146390000, 12500, 'Military VHF 146.39', 'gov', 'NFM', 'Strong persistent repeater'
    UNION ALL SELECT 150490000, 12500, 'Military VHF 150.49', 'gov', 'NFM', 'Strong persistent repeater'
    UNION ALL SELECT 169000000, 1000000, 'DAB/Business VHF', 'broadcast', 'digital', 'Digital radio infrastructure'
    UNION ALL SELECT 182110000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch6'
    UNION ALL SELECT 186210000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch7'
    UNION ALL SELECT 191250000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch8'
    UNION ALL SELECT 195350000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch9'
    UNION ALL SELECT 200140000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch10'
    UNION ALL SELECT 433920000, 0, 'ISM 433.92', 'ism', 'mixed', 'Sensors, remotes'
    UNION ALL SELECT 446006250, 12500, 'PMR446 Ch1', 'pmr', 'NFM', 'License-free radios'
    UNION ALL SELECT 446018750, 12500, 'PMR446 Ch2', 'pmr', 'NFM', 'License-free radios'
    UNION ALL SELECT 446031250, 12500, 'PMR446 Ch3', 'pmr', 'NFM', 'License-free radios'
    UNION ALL SELECT 446210000, 12500, 'PMR446 (observed)', 'pmr', 'NFM', 'Ringing preamble + transmission'
) AS seed
WHERE (SELECT count() FROM spectrum.known_frequencies) = 0;
