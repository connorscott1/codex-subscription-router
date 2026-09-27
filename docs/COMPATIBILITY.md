# Compatibility

The patcher is intentionally tied to known ChatGPT desktop bundle structures.
It verifies every modified renderer, main-process, and native binary anchor and
stops instead of applying a partial patch.

## Release 0.1.0

### Verified official builds

| ChatGPT version | Bundle build | `app.asar` SHA-256 | Architecture |
| --- | --- | --- | --- |
| `26.803.61601` | `6396` | `d5a44ed9e2f1db5f81dbbe85408aed256f3203c5b16f00817bb9d7cd941343cf` | Apple silicon (`arm64`) |
| `26.915.31945` | `9922` | `1f7939c1c781887c167043c4d1d307af3400d324685cfc315dfe2f80e634f483` | Apple silicon (`arm64`) |

Build 9922 has its own reviewed renderer layout. Its source desktop package
contains 53 Computer Use bundle-ID references: four are in two distribution
provisioning profiles that are removed from the independent copy, leaving 49
patchable references whose exact count is enforced. Its ASAR contains 16
references, also enforced exactly. Every build-specific renderer, updater,
desktop-profile, managed-helper, and Computer Use instruction anchor must match
exactly once.

A different official version may work when all anchors remain identical, but
it is unverified. The patcher rejects a version, build, or ASAR hash mismatch by
default; `--allow-untested-source` is an explicit diagnostic override. Never
weaken an anchor-count or binary-constant check merely to make a new build
complete. Review the upstream change and update the patch deliberately.
