-- Labels now lead with the sensor number ("#14 Cannabis" rather than
-- "Cannabis (14)") so Grafana legends, which sort as text, sort by sensor.
--
-- The number is zero-padded to two digits for exactly that reason: a plain
-- "#2" sorts after "#14" alphabetically, which defeats the point. Two digits
-- covers the fleet (sensors run 1..30) and matches how Canopy already renders
-- them ("SENSOR 04"), so the two UIs agree.
--
-- Padding is guarded on an all-digits name: a sensor with a non-numeric
-- name (e.g. a hand-named device) must pass through untouched.
--
-- The "(#N)" strip from display_name is inherited from migration 03 — the
-- duplicate-plant naming scheme embeds the sensor number in display_name
-- ("Cannabis (#14)"), which would otherwise print twice.
CREATE OR REPLACE VIEW sensor_labels AS
SELECT s.id AS sensor_id,
       COALESCE(
         NULLIF(
           trim(
             CASE
               WHEN s.name IS NULL       THEN ''
               WHEN s.name ~ '^[0-9]+$'  THEN '#' || lpad(s.name, 2, '0')
               ELSE s.name
             END
             || ' ' ||
             COALESCE(regexp_replace(p.display_name, ' \(#[^)]+\)$', ''), '')
           ),
           ''
         ),
         s.mac_or_device_id
       ) AS label
FROM sensors s
LEFT JOIN plant_assignments pa ON pa.sensor_id = s.id AND pa.released_at IS NULL
LEFT JOIN plants p ON p.id = pa.plant_id;
