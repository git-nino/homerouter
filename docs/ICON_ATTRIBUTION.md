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

All product names and trademarks remain the property of their respective
owners. The icon is used for identification only and does not imply endorsement.
