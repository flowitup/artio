-- Each stored workflow's Load Image slots, so the /workflows list can show one file input per slot
-- without parsing every stored graph on each render. Rows stored before this migration are
-- backfilled from their own graph with the same rule as custom_workflows.image_slots().
ALTER TABLE workflows ADD COLUMN image_inputs_json TEXT NOT NULL DEFAULT '[]';
UPDATE workflows SET image_inputs_json = (
  SELECT json_group_array(json_object(
    'node', key,
    'title', coalesce(nullif(substr(trim(json_extract(value, '$._meta.title')), 1, 80), ''),
                      json_extract(value, '$.class_type'))))
  FROM json_each(workflows.graph_json)
  WHERE json_extract(value, '$.class_type') IN ('LoadImage', 'LoadImageMask')
    AND json_type(value, '$.inputs.image') = 'text'
);
