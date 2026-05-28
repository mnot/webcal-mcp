# webcal-mcp

A small read-only [Model Context Protocol](https://modelcontextprotocol.io/) server
that exposes one or more remote iCalendar feeds (the `webcal://` or `.ics` kind)
as queryable tools for an LLM agent.

## Install

```
pipx install webcal-mcp
```

Or from a checkout:

```
pipx install .
```

This puts a `webcal-mcp` script on your `PATH`. Python 3.10 or later is required.

## Configure

Create `~/.config/webcal-mcp/config.toml` (or point `WEBCAL_MCP_CONFIG` at any
other path). See `config.example.toml`:

```toml
default_ttl_seconds = 900

[calendars.personal]
url = "webcal://example.com/personal.ics"
description = "Personal calendar"

[calendars.work]
url = "https://example.com/work.ics"
description = "Work calendar"
```

`webcal://` and `webcals://` URLs are normalized to `https://`.

### Local calendars via EventKit (macOS)

On macOS, any calendar already set up in Calendar.app — iCloud, local,
subscribed `.ics`, CalDAV, Google — can be read directly through Apple's
EventKit framework. No `webcal://` URL hunting required:

```toml
[calendars.personal]
source = "eventkit"
identifier = "Personal"   # display name from Calendar.app, or its UUID
description = "iCloud personal calendar"
```

`identifier` matches the calendar's display name first, then falls back
to its UUID. Run `webcal-mcp list-eventkit` to see what's available
(this dumps `title  uuid  [source]` for every calendar).

First use triggers a one-time TCC prompt for Calendar access; the grant
is bound to the binary that invokes EventKit (the Python interpreter
behind `webcal-mcp`), so re-granting may be needed if you reinstall.
The PyObjC dependency installs automatically on macOS only.

## Run

```
webcal-mcp
```

The server speaks MCP over stdio. Wire it into an MCP-aware client (Claude
Desktop, Claude Code, etc.) by pointing at the `webcal-mcp` script. For
example, for Claude Code:

```
claude mcp add webcal -- webcal-mcp
```

## Tools

| Tool | Purpose |
| --- | --- |
| `list_calendars` | Names, descriptions, capability flags for the configured calendars. |
| `list_events` | Events in a date range, with optional `query`, `categories`, `location` filters and `brief` / `full` / `markdown` detail modes. Either bound may be omitted for open-ended windows. |
| `events_on` | All events occurring on a given date. |
| `get_event` | Full record for a single UID. |

Recurring events are expanded within the requested window. Responses are
cached in memory with a per-calendar TTL; revalidation uses HTTP `ETag` and
`Last-Modified`.

## Read-only

Calendars are read-only today. The `CalendarSource` abstraction reserves a
`writable` capability flag so future write-capable backends can be added
without changing the tool surface.
