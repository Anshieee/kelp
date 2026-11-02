# Mobile fallback reuse and attribution

Inventory completed against `milind-soni/OpenMausBot` commit
`f52effc1a5871e4e54b3d2c699336a28c6cb1f97` (shallow checkout, 2026-11-03).
The repository is Apache-2.0 (`LICENSE`) and its attribution file is `NOTICE`.
Attribution: OpenMausBot, Copyright 2026 Milind Soni and OpenMausBot contributors,
Apache License 2.0. Source: <https://github.com/milind-soni/OpenMausBot>.

The checked files included `apps/docs/app/{layout,page}.tsx`, the phone-application
dialog and setup flow, the app's safe-area styles, the phone-web-app verification
screenshots, and `companion/README.md` plus its implementation. The phone UI is a
native companion / desktop-pairing flow, not a standalone web client for the Kelp
v1 contract; it is not suitable for reuse here. **No source, assets, runtime,
backend, brand marks, mascot, or service integration were reused** in this fallback,
so no third-party license text or NOTICE content is bundled. This note records the
requested license review and attribution; the app itself is original Kelp UI and
calls only the Kelp v1 facade.
