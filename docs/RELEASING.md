# Releasing

Releases are source-only. Never attach a patched app, ASAR, extracted official
file, signing certificate, provisioning profile, or account data.

1. Update `VERSION`, `package.json`, and both version fields in
   `package-lock.json`.
2. Move changelog entries from Unreleased into `## [x.y.z] - YYYY-MM-DD`.
3. Record the tested official app version, build, architecture, and ASAR hash in
   `docs/COMPATIBILITY.md`.
4. Run `npm ci --ignore-scripts`, `npm run check`, and
   `npm run release:check` on macOS.
5. Complete `docs/SMOKE-TEST.md` with a locally selected team-backed signature.
   Record the exact commit, macOS version, and pass/fail results, but never the
   local signing identity, team ID, certificate fingerprint, keychain identifiers,
   or raw signing diagnostics. Use synthetic test fixtures.
6. Review `git diff --check`, all commits being published, and the staged files.
   Confirm no credentials, personal signing metadata, private notes, or app
   bundles are included. Ignore rules and automated release checks alone do not
   establish that history or diagnostic text is safe to publish.
7. Configure the protected `release` environment, tag the reviewed commit as
   `vX.Y.Z`, and push the tag.

The release workflow verifies that the tag matches `VERSION`, repeats all
checks, and creates a draft GitHub source release with generated notes. Review
the draft and smoke-test record before publishing it manually.
