# Installer catalog-before-extraction repair — 2026-09-10

The default extraction setup now runs after the OpenClaw catalog sync. This is
required because `--tier cheap` resolves every profile in the cheap fallback
chain, while an upgraded router can predate profiles newly supplied by the
dynamic catalog. Previously the installer attempted that resolution first and
failed before it reached the sync that would register the missing profile.

The change only moves the existing extraction setup block. A machine with an
OpenClaw configuration now synchronizes successfully before writing the
extraction policy and model config. A failed sync exits before extraction. A
machine without an OpenClaw configuration retains the existing offline path
and proceeds directly to extraction setup against its installed catalog.

A shell-level regression executes the real section extracted from
`deploy/macos/install.sh` with an isolated home, state directory, repository
stub, and Python command probe. The probe models an old router by refusing
extraction until catalog sync creates a readiness marker. It verifies the
successful call order is `sync`, then `extraction`, and verifies a failed sync
produces only the `sync` call and the installer's explicit failure message. A
third case verifies an offline install still calls extraction directly.

```text
python3 -m unittest tests.test_installer_model_catalog_order
Ran 3 tests — OK
```

No live installation, host catalog, model call, or deployment was performed.
