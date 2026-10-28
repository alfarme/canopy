-- Duplicate-plant naming now embeds the sensor number directly in
-- display_name ("Cannabis (#10)"), so sensor_labels' own "(sensor-name)"
-- suffix duplicated it ("Cannabis (#10) (10)"). Strip any existing
-- "(#N)" suffix from display_name before re-appending the sensor number,
-- so every label reads "Name (N)" once, not twice.
CREATE OR REPLACE VIEW sensor_labels AS
SELECT s.id AS sensor_id,
       COALESCE(
         regexp_replace(p.display_name, ' \(#[^)]+\)$', '') || ' (' || s.name || ')',
         s.name,
         s.mac_or_device_id
       ) AS label
FROM sensors s
LEFT JOIN plant_assignments pa ON pa.sensor_id = s.id AND pa.released_at IS NULL
LEFT JOIN plants p ON p.id = pa.plant_id;
