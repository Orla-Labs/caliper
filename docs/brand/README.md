# Caliper brand assets

| File | Use |
| --- | --- |
| `caliper-mark-dark.png`, `caliper-mark-light.png` (512 px), `*-1024.png` | Square mark: avatars, favicons, package listings |
| `caliper-hero-dark.png`, `caliper-hero-light.png` (1280x360) | README header (`<picture>` picks the one matching the reader's GitHub theme) |
| `caliper-social.png` (1280x640) | GitHub social preview (Settings > General > Social preview) |

The mark is a vernier caliper: a ruled beam, a fixed jaw, a sliding jaw, and the measured gap between them.

The `.svg` files are the sources. To regenerate everything:

```bash
uv run python docs/brand/make_brand.py   # writes the SVGs
node docs/brand/render.mjs               # PNGs via Playwright Chromium (from packages/label/e2e)
```
