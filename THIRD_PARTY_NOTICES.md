# Third-Party Notices

## atom2ueki/mcp-server-synology

Repository:
https://github.com/atom2ueki/mcp-server-synology

Reference revision:
6afdaa3407e07c786d79644b92930152751af223

License:
MIT

PARDO usage:
- architecture and implementation reference;
- health-domain naming and behavior reference;
- candidate source for selectively adapted code;
- external MCP server for the synology-mcp implementation of
  storage.health@1.0.0, pinned at the revision above;
- not vendored into infra-tools and not imported via a local
  `_legacy/` path.

The synology-mcp adapter speaks MCP stdio and is allowed to call only:
- synology_list_nas
- synology_health_summary

The remaining upstream MCP surface, including mutating tools, is not
part of the PARDO public contract and must not be invoked.

Any source code copied or adapted from this project must retain the
applicable MIT copyright and license notice and be reviewed before
promotion into an approved PARDO implementation.

Copyright (c) 2025 Tony Li. MIT License.
