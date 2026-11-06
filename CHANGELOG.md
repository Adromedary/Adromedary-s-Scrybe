# Changelog

## 1.0.0 - 2026-11-06

First public release, renamed from "MTG Bulk Card Image Downloader" to Adromedary's Scrybe.

- Decklist parser for plain text, Arena, Moxfield, Archidekt, Forge `.dck` and MTGO
  `.dek`, with sections, set codes, collector numbers and foil markers.
- Exact printings honoured when a set and collector number are given.
- Automatic Scryfall bulk-data download, compact on-disk index and daily freshness check;
  Live API and own-file modes.
- Normal-card filter extended: retro frames, The List reprints, Universes Beyond basics,
  non-cosmetic promo types. Final Fantasy sub-series tags no longer count.
- Art preference: newest normal frame, original printing, or classic old border.
- Random distinct basic-land art, one set each where possible; set-pinned variety.
- Back faces, tokens and emblems, per-copy files, card-type subfolders, CSV manifest.
- Fuzzy typo correction, Archidekt URL import, batch folders.
- Parallel image downloads, Scryfall rate limiting with Retry-After, timeout requeue
  (3 attempts).
- Issues tab split into problems and notes, with copy and CSV export.
- Saved settings, app icon, Windows version details, one-click `build.bat`.
