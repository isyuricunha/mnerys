# Legacy Odysseus Service Installer

The files in this directory belong to an **upstream-era system service**. They are retained for migration and provenance only.

- `install-service.sh` copies the adjacent `odysseus-ui.service` to a system unit directory and **starts/enables it**.
- `odysseus-ui.service` requires host-specific adaptation and is **not** the native Mnerys preview unit.

**Do not execute this installer for the Mnerys preview**. It could start an additional legacy web/worker process and conflict with the existing service or persistent data. Follow the [Mnerys native preview guide](../../../docs/deployment/NATIVE_PREVIEW.md) instead.
