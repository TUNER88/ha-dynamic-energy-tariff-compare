# Contributing

## Development

```bash
uv venv -p 3.14 .venv            # Home Assistant 2026.10 needs Python >= 3.14.2
uv pip install -p .venv ruff -r requirements_test.txt
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

`custom_components/dynamic_energy_tariff_compare/calc.py` holds all pure calculation logic (slot
splitting, units, price-list parsing, accumulation). It has no Home Assistant imports and is
covered by `tests/test_calc.py`. `tests/test_integration.py` and `tests/test_backfill.py`
run the integration in a real Home Assistant test instance
(`pytest-homeassistant-custom-component`).

## Workflow

`main` is protected. Every change goes through a pull request, including changes by admins.

* Required checks: **Ruff & pytest**, **Hassfest**, **HACS validation**. The branch must be
  up to date with `main`.
* Only squash merges are allowed. The **PR title** becomes the commit message, so write it
  as a conventional commit (`feat: …`, `fix: …`, `docs: …`).
* Merged branches are deleted automatically.

## Versioning and releases

Every merged PR creates a release automatically (`.github/workflows/release.yml`):

1. The workflow takes the latest `vX.Y.Z` tag. If no tag exists yet, it uses the version in
   `manifest.json` (first release).
2. It picks the bump in this order:
   * PR label `major`, `minor` or `patch`;
   * otherwise the PR title: `type!:` or `BREAKING CHANGE` → major, `feat:` → minor;
   * otherwise patch.
3. It builds `dynamic_energy_tariff_compare.zip` with that version written into the zipped
   `manifest.json`.
4. It creates the tag `vX.Y.Z` and a GitHub release with generated notes and the zip attached.

Add the label `skip-release` to merge without a release. A release can also be started
manually under *Actions → Release → Run workflow*.

**Why the version in the repo's `manifest.json` isn't bumped:** a bump commit would have to
be pushed straight to the protected `main` branch, or need a second "release PR". Instead,
the tag is the source of truth. HACS installs the release zip (`zip_release: true` in
`hacs.json`), and that zip contains the correct version. The `manifest.json` in the repo is
only a placeholder. It holds the version of the first release, and you may update it in a
normal PR, but you don't have to.
