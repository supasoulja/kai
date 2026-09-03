---
name: disk-space
description: Show what is using disk space — folder sizes across the whole machine plus the largest individual files
triggers: disk space, what is using my disk, storage, running out of space, biggest files
---
## Steps
- files.disk_usage top_n=8
- files.find_large top_n=10
