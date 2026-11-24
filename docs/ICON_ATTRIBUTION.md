# Icon attribution

## Selected icon

- **Name:** `router`
- **Collection:** [Homarr Labs Dashboard Icons](https://github.com/homarr-labs/dashboard-icons)
- **Source SVG:** https://github.com/homarr-labs/dashboard-icons/blob/main/svg/router.svg
- **Direct source:** https://raw.githubusercontent.com/homarr-labs/dashboard-icons/main/svg/router.svg
- **Collection page:** https://dashboardicons.com/
- **Collection license:** [Apache-2.0](https://github.com/homarr-labs/dashboard-icons/blob/main/LICENSE)

This icon was selected because it depicts a router/gateway directly, which
matches Homerouter's purpose of viewing and monitoring FTTH ONT/router
information. It is not an unrelated application icon, and it remains legible on
the dashboard's dark background and at small PWA/favicon sizes.

## Local assets

The source SVG is committed locally at `static/dashboard-icons-router.svg`. The
PNG assets were generated from that SVG with transparent padding and committed
locally; the app never loads the icon from an external CDN at runtime:

- `static/favicon.png` — 64 × 64
- `static/icon-192.png` — 192 × 192
- `static/icon-512.png` — 512 × 512
- `static/manifest.webmanifest` and `templates/index.html` reference these local paths.

## FTTH viewer app icon

The installable PWA icon is the custom Option 1 "Modern Green (Cloud + Home)"
artwork: a deep green rounded tile with a bright green gradient edge and house
symbol inside a clean white cloud. It contains no text and is maintained as a
scalable master at `static/icon-master.svg`.

All raster assets are generated locally from that master with `ffmpeg` and its
librsvg SVG decoder:

- `static/icon-192.png` — 192 × 192, `any` purpose
- `static/icon-512.png` — 512 × 512, `any` purpose
- `static/icon-maskable-192.png` — 192 × 192, `maskable` purpose
- `static/icon-maskable-512.png` — 512 × 512, `maskable` purpose
- `static/apple-touch-icon.png` — 180 × 180 Apple touch icon
- `static/favicon.png` — 64 × 64 favicon

The full-bleed background keeps Apple touch and maskable crops opaque, while
the cloud/house motif stays inside the maskable safe zone. The service worker
cache is `gwmon-v9`; its install/activate handlers replace the previous cache
without touching `/api/` responses. An already-installed PWA may need one
online refresh or a close/reopen before the platform launcher updates its
cached name and icon, because some operating systems capture that metadata at
install time.

The earlier Homarr Labs router icon and its PNG derivatives are retained for
unrelated dashboard illustration resources; nothing is loaded from a CDN at
runtime.

All product names and trademarks remain the property of their respective
owners. The icon is used for identification only and does not imply endorsement.
