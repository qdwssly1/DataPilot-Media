# Relationships

Wren project layout v5 loads relationships from the project-root
`relationships.yml` file.

`stream_sessions` and `alarm_events` are both event-grain fact models. A
row-level relationship on region, CDN, and hour would be many-to-many and
would multiply session metrics whenever several alarms occur in one hour.
The loaded relationship list therefore remains intentionally empty. Use the
`hourly_alarm_correlation` view, which aggregates both sides before joining.

This directory is reserved for human-readable relationship design notes. Do
not place executable Wren relationship YAML here; the current loader will not
read it.
