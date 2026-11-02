# Mnerys visual identity

**Mnerys — Built to be yours.**

## Product and mascot are separate

- **Mnerys:** the product. Its icon, favicons, PWA assets, and wordmarks use a neutral geometric M.
- **Nery Mizuki:** the mascot. A red-haired chibi with amber eyes and cream bows. She appears in welcome/login screens, documentation and future illustrations, but is never substituted for the app icon.

## Mnerys default dark palette

| Role | Color |
| --- | --- |
| Canvas | `#15110F` |
| Surface | `#211915` |
| Raised surface | `#2A211D` |
| Border | `#3A2C26` |
| Terracotta | `#CC785C` |
| Foreground | `#F3E8E2` |

The default brand uses charcoal/terracotta. **The original Odysseus themes remain available as optional presets**, without importing the old star, icons or visual identity anywhere outside the theme selection.

## Legacy themes and user settings

The 19 inherited theme palettes are preserved exactly. Mnerys replaces the default `dark` preset with its terracotta default and exposes the original upstream `dark` palette under **Classic Dark** (`classic_dark`). Every other upstream preset keeps its original colors.

The legacy `odysseus-theme` and `odysseus-custom-themes` localStorage keys intentionally remain unchanged. They are storage compatibility contracts, not visible branding. Changing them without a migration would lose users' selected palettes and custom themes.

Original theme colors are frozen in [the regression fixture](../../tests/fixtures/odysseus_original_themes.json).

## Artwork inventory

- Wordmarks: [dark](../../assets/branding/mnerys-wordmark-dark.svg) and [light](../../assets/branding/mnerys-wordmark-light.svg)
- Brand hero illustration: [mnerys-hero.svg](../../assets/branding/mnerys-hero.svg) (not an application screenshot)
- App icon: [mnerys-icon.svg](../../static/icons/mnerys-icon.svg), plus favicon and PWA PNGs
- Nery's approved compact preview: [nery-mizuki-avatar.webp](../../assets/mascot/nery-mizuki-avatar.webp) (128 × 157 px)
- [Desktop, mobile and login screenshots](../screenshots/README.md)

The original full-resolution Nery artwork is not present in this repository. Avoid using an upscaled thumbnail as the source for expression sheets or transparent illustrations.

## Validation and boundaries

Visual regression checks cover the icon, text and theme definitions, and Chromium screenshots were captured for desktop and mobile. Existing user preferences, services, backend/runtime identifiers, and license acknowledgements were not migrated by this visual pass.

Preserve AGPL obligations and original author acknowledgments: [LICENSE](../../LICENSE), [ACKNOWLEDGMENTS.md](../../ACKNOWLEDGMENTS.md).
