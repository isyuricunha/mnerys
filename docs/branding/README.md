# Mnerys visual identity

**Mnerys — Built to be yours.**

## Product and mascot are separate

- **Mnerys:** the product. Its icon, favicons, PWA assets, and wordmarks use a neutral geometric M.
- **Nery Mizuki:** the mascot. A red-haired chibi with amber eyes and cream bows. She appears in welcome/login screens, documentation and future illustrations, but is never substituted for the app icon.

## Default interface theme: Eclipse

Mnerys uses the **original Eclipse preset** as its default interface theme. The palette is identical to the inherited preset rather than an approximate re-creation:

| Role | Color |
| --- | --- |
| Canvas | `#17191C` |
| Panel / sidebar | `#0F1113` |
| Border | `#3C4248` |
| Teal accent | `#46C2B3` |
| Foreground | `#E8E4DC` |

The default background effect is **starfield-depth**, inherited from Eclipse. The login, workspace fallbacks and PWA theme-color use this palette. The default/reset action now restores Eclipse and its effect.

**The Mnerys logo remains a neutral geometric M.** Existing charcoal/terracotta wordmarks, static icons, brand art and Nery illustrations remain unchanged: choosing the Eclipse **UI theme** does not modify the mascot or silently replace brand assets.

## Presets, original themes and compatibility

The former Mnerys terracotta UI is still available as **Mnerys Terracotta** (internal preset `dark`): background `#15110F`, surface `#211915`, border `#3A2C26`, accent `#CC785C`, text `#F3E8E2`. This is distinct from the **Classic Dark** preset (`classic_dark`), which preserves Odysseus's original `dark` palette.

All **19 original Odysseus theme palettes** remain unchanged, including Eclipse; only the selected default differs. The legacy `odysseus-theme` and `odysseus-custom-themes` localStorage keys intentionally remain unchanged as compatibility contracts. **Existing saved selections and custom themes take precedence over the new default**, including previously saved terracotta choices; users can select Eclipse or reset the theme to adopt the new default.

The main app and login honor saved base colors. Other built-in presets and user-authored themes remain selectable and intact.

Original theme colors are frozen in [the regression fixture](../../tests/fixtures/odysseus_original_themes.json).

## Artwork inventory

- Wordmarks: [dark](../../assets/branding/mnerys-wordmark-dark.svg) and [light](../../assets/branding/mnerys-wordmark-light.svg)
- Brand hero illustration: [mnerys-hero.svg](../../assets/branding/mnerys-hero.svg) (not an application screenshot)
- App icon: [mnerys-icon.svg](../../static/icons/mnerys-icon.svg), plus favicon and PWA PNGs
- Nery's compact app avatar: [nery-mizuki-avatar.webp](../../assets/mascot/nery-mizuki-avatar.webp) (128 × 157 px; preserved in welcome/login)
- Nery's complete [kit v1](../../assets/mascot/kit-v1/manifest.json) (approved sheet, 8 native-size poses, transparent PNG/WebP exports, square avatar alternatives)
- [Desktop, mobile and login screenshots](../screenshots/README.md)

The approved 1536 × 1024 character sheet is preserved in [the kit](../../assets/mascot/kit-v1/source/approved-character-sheet.png), but the individual figure crops are smaller (full body: 195 × 403 px). Do not present resized crops as new original high-resolution art. **Nery artwork is proprietary, © 2026 Yuri Cunha, all rights reserved**: see the [separate mascot license](../../assets/mascot/LICENSE.md). Only use/display inside an authorized Mnerys installation is permitted without additional authorization; redistribution requires the rights holder's written permission. The AGPL software license does not license this artwork.

## Public GitHub Pages landing

The [Mnerys Pages entry point](../../website/index.html) and its [Eclipse styling](../../website/site.css) use the documented Eclipse interface palette. The landing is **an informational project page**, not an interactive deployed Gateway or a fabricated product UI. Its architecture graphic is a labeled, conceptual diagram; its preview image is a byte-identical copy of the [real Eclipse login screenshot](../screenshots/README.md), with the original preserved. The [official geometric M icon](../../website/assets/mnerys-icon.svg) is used as the favicon and product brand; Nery's [licensed avatar](../../website/assets/nery-mizuki-avatar.webp) is displayed separately as the mascot.

The eight imported upstream demo videos remain unchanged and are documented as [historical footage](../../website/legacy-previews.md). They are not loaded on the Mnerys landing, where old visuals, joke testimonials and upstream marketing claims would misrepresent the current project. The legacy Pages guides may still include upstream-era technical instructions and are **not** the source of truth for Mnerys installation; use the [current native preview documentation](../deployment/NATIVE_PREVIEW.md), the [PostgreSQL 17 guide](../deployment/POSTGRESQL17.md), and the repository README.

**Publication:** source changes to the landing require the existing GitHub Pages workflow to finish after an authorized push. A local Chromium capture or passing test is not proof that the public Pages deployment is up to date.

## Validation and boundaries

Visual regression checks cover the icon, text and theme definitions, and Chromium screenshots were captured for desktop and mobile. Existing user preferences, services, backend/runtime identifiers, and license acknowledgements were not migrated by this visual pass.

Preserve AGPL obligations and original author acknowledgments: [LICENSE](../../LICENSE), [ACKNOWLEDGMENTS.md](../../ACKNOWLEDGMENTS.md).
