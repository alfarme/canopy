-- Idempotent migration: plant lifecycle additions
-- Adds plant_id to identify_photos and the sensor_labels view.

ALTER TABLE identify_photos ADD COLUMN IF NOT EXISTS plant_id INTEGER REFERENCES plants(id);

CREATE OR REPLACE VIEW sensor_labels AS
SELECT s.id AS sensor_id,
       COALESCE(p.display_name || ' (' || s.name || ')', s.name, s.mac_or_device_id) AS label
FROM sensors s
LEFT JOIN plant_assignments pa ON pa.sensor_id = s.id AND pa.released_at IS NULL
LEFT JOIN plants p ON p.id = pa.plant_id;
