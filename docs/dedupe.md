# Source URL de-duplication

MarkBase prevents repeat web page and YouTube submissions from creating duplicate library items. De-duplication is based on a normalized source URL, not on title or folder name.

## What is matched

MarkBase checks the submitted URL against existing item metadata before conversion starts. If an existing item has the same normalized `source_url`, ingestion returns that item path instead of creating another folder.

The queue also checks for active duplicates. If the same source is already queued or processing, the existing job is reused instead of adding another queued job.

## URL normalization

For ordinary web URLs, MarkBase normalizes the scheme and host, removes trailing slashes, sorts remaining query parameters, and drops common tracking parameters.

Ignored tracking parameters include:

- `utm_*`
- `fbclid`
- `gclid`
- `igshid`
- `mc_cid`
- `mc_eid`
- `msclkid`

Meaningful query parameters are kept. For example, documentation version parameters such as `view=powershell-7.5` remain part of the de-duplication key.

## YouTube URLs

YouTube videos are canonicalized to their video ID. These forms match the same library item:

```text
https://www.youtube.com/watch?v=VIDEO_ID
https://youtu.be/VIDEO_ID
https://www.youtube.com/shorts/VIDEO_ID
```

Playlist, channel, and other YouTube URLs are not treated as the same as a single video unless they resolve to the same video URL before being queued.

## Existing duplicates

This feature prevents future duplicates. It does not automatically merge older duplicate folders that already exist in the library. Existing duplicates should be reviewed and moved to trash through MarkBase so they remain recoverable.

## What is not de-duplicated

MarkBase does not currently compare full document content hashes, titles, or uploaded file bytes. Local file uploads can still create separate items if uploaded more than once.
