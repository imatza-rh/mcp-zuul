# Time-Based Filtering for Zuul MCP

This document describes the new time-based filtering capabilities added to the `list_builds` and `list_buildsets` tools.

## Overview

The Zuul API does not natively support time-based filtering. This enhancement adds client-side time filtering to allow you to query builds and buildsets within specific time windows.

## New Parameters

Both `list_builds` and `list_buildsets` now support these additional parameters:

- `completed_after` (string): Filter builds/buildsets completed after this time (ISO 8601 format)
- `completed_before` (string): Filter builds/buildsets completed before this time (ISO 8601 format)
- `started_after` (string): Filter builds/buildsets started after this time (ISO 8601 format)
- `started_before` (string): Filter builds/buildsets started before this time (ISO 8601 format)

## ISO 8601 Format

All time parameters accept ISO 8601 formatted timestamps:

- `"2026-04-18T00:00:00Z"` - UTC time (Z suffix)
- `"2026-04-18T00:00:00+00:00"` - UTC with explicit offset
- `"2026-04-18T14:30:00-05:00"` - With timezone offset
- `"2026-04-18T14:30:00"` - No timezone (assumes UTC)

## Usage Examples

### Find builds that failed in the last 48 hours

```python
from datetime import datetime, timedelta, timezone

# Calculate timestamp for 48 hours ago
now = datetime.now(timezone.utc)
two_days_ago = now - timedelta(hours=48)

# Query builds
result = await list_builds(
    ctx=ctx,
    result="FAILURE",
    completed_after=two_days_ago.isoformat(),
    limit=50
)
```

### Find all builds for a project completed today

```python
from datetime import datetime, timezone

# Today's date at midnight UTC
today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

result = await list_builds(
    ctx=ctx,
    project="openstack/nova",
    completed_after=today.isoformat(),
    limit=100
)
```

### Find buildsets in a specific time window

```python
result = await list_buildsets(
    ctx=ctx,
    pipeline="check",
    completed_after="2026-04-18T00:00:00Z",
    completed_before="2026-04-19T00:00:00Z",
    limit=50
)
```

## Implementation Details

### Client-Side Filtering

Since the Zuul API doesn't support time-based filtering, the implementation:

1. Fetches results from the API (with a 3x multiplier on `limit` when time filters are active)
2. Parses timestamps from build/buildset objects
3. Filters results based on the provided time criteria
4. Returns the filtered results up to the requested `limit`

### Timestamp Fields

- **Builds**: Uses `end_time` for completion filters, `start_time` for start filters
- **Buildsets**: Uses `last_build_end_time` for completion filters, `first_build_start_time` for start filters

### Performance Considerations

- Time filtering happens client-side after fetching from the API
- When time filters are active, the tool automatically fetches up to 3x the requested limit (capped at 300) to account for filtering
- For large time windows with many results, you may need to increase the `limit` parameter or use pagination with `skip`

## Testing

Run the test suite to verify the implementation:

```bash
uv run pytest tests/test_helpers.py::TestParseIsoTimestamp -v
```

## Future Enhancements

Potential improvements could include:

1. **Use `idx_min`/`idx_max` API parameters**: If Zuul's build indices are chronological, these could provide more efficient range-based filtering
2. **Auto-pagination**: Automatically fetch additional pages when filtering reduces results below the requested limit
3. **Relative time strings**: Support for "last 48h", "yesterday", etc.
4. **Caching**: Cache timestamp-to-index mappings for more efficient queries
