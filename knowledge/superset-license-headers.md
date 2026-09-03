---
name: "Every new file in Superset needs the Apache license header"
trigger_description: "When creating a new file of any type in apache/superset"
---
The `lint-check` job runs Apache RAT and fails on any new file without the ASF license header. Copy the header from a neighbouring file of the same type. There is no DCO and no CLA.
