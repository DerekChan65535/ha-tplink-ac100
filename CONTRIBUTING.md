# Contributing

Use Python 3.14.2+ and uv. Run from the repository root:

```console
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest
```

The lockfile pins Home Assistant 2026.11.1 and the official pytest-homeassistant
2026.11.1 test package. Tests must be offline: mock the controller and network, and
never require a user's HA instance, credentials, or production Wi-Fi.

## Changes and evidence

- Keep protocol logic separate from HA entity behavior. Add small tests for changed
  request/response handling and the observable HA behavior.
- State whether evidence is a synthetic fixture, a read-only hardware check, or an
  authorized live write. Do not call mock coverage hardware verification.
- Never reboot or reconfigure a user's network to complete a test.
- Independently implement protocol behavior. Do not copy, translate, or rename
  unlicensed third-party source. Document references and asset licenses in THIRD_PARTY.md.
- Submit no passwords, real controller/client identifiers, raw captures, local HA
  configuration, or private research. Use synthetic/redacted fixtures.
- Contributions are made under this repository's MIT license; keep required notices
  for any separately licensed material, and obtain permission before including it.

## Release checklist

1. Verify the supported hardware/firmware and HA scope, including any untested controls.
2. Run project tests, HACS validation, and hassfest without ignored failures.
3. Update README files and the version's CHANGELOG.md section. Record compatibility,
   breaking changes, migrations, known limitations, and rollback restrictions.
4. Keep pyproject.toml version, manifest.json version, and tag (optional leading `v`)
   in agreement. Sync and commit uv.lock if project metadata changes.
5. Audit the entire tracked tree/history for secrets and unlicensed material.
6. Push the reviewed version commit and tag. The release workflow creates a **draft**
   only after all checks pass. Review the draft, run installation/update/rollback
   checks, and publish it manually. Never move an existing release tag.
7. Publish a GitHub Release, not just a git tag. HACS custom repositories do not require
   default-directory approval. Apply for default inclusion only when all current
   HACS requirements are met.

Actions use pinned commits; review and update those pins periodically. The HACS and
hassfest validators may still use moving images internally. Passing their checks does
not establish runtime firmware compatibility or write safety.
