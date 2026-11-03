# Legacy Distribution Build Scripts

These are **inherited Odysseus packaging recipes**, not finished Mnerys-branded distribution artifacts.

Run from the repository checkout:

```bash
./scripts/build/build-macos-app.sh
```

For Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build\build-windows-portable.ps1
```

Both scripts resolve the **repository root** before finding dependency manifests, assets and output directories. They keep bundled attribution files at their original root paths. The macOS build requires macOS tooling, and the Windows build requires a Windows environment; a Linux syntax or static test is **not** an end-to-end packaging validation.

Do not distribute output as an official Mnerys release before completing branding, licensing and native platform smoke tests.
