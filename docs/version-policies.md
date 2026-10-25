<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Version policies

Tags are registry labels, not proof of API compatibility or security fixes. The engine
compares numeric versions using `packaging`, with prefix, distribution suffix and release
channel constraints. Candidate tags must have the same number of numeric components,
except for PostgreSQL's transition from three components before version 10 to two thereafter.
A version match alone does not establish platform availability for the candidate image.
Only metadata is read; no image configuration or layers are downloaded.

| Scheme | Classification |
|---|---|
| auto | Recognizes three-component numeric tags, conservative `v`/distribution prefixes, YYYY.month.release CalVer, and the `postgres` repository |
| semver | Requires three components; changed first/second/third components mean major/minor/patch |
| zabbix | Three components, preserving prefixes such as `ubuntu-` and suffixes |
| postgres | PostgreSQL 10+: changed first component is major, remaining maintenance component is patch; before 10 the first two components define the major family |
| calver | YYYY.month.release: same year/month updates are patch; changed month or year is minor; no inferred major category |
| maintenance | First component defines major family; subsequent changes are patch |
| custom | Numeric comparison with explicit prefix/suffix and optional component boundaries |

For `13.2.4`, tags `13.2.5`, `13.3.1` and `14.0.2` populate patch, minor and major
independently. Each category selects its highest allowed candidate. The largest tag does
not hide maintenance releases. `18.6-bookworm` stays on `-bookworm` by default.
`ubuntu-7.0.32` stays on `ubuntu-`. CalVer year changes never imply a major migration.
PostgreSQL alias tags such as `18` do not identify a maintenance version and remain unknown.
Auto comparison of two-component tags is deliberately unsupported unless a repository
profile is known; choose `maintenance` or `postgres` explicitly.

```yaml
rules:
  - match: {image: registry.example.org/team/application}
    version:
      scheme: custom
      prefix: release-
      suffix: -linux
      preserve_variant: true
      include_prerelease: false
      allowed_majors: [3, 4]
      channels: [stable]
      tag_patterns: ['release-*-linux']
      major_components: 1
      minor_components: 2
```

`prefix` and `suffix` are literal strings removed before parsing and required on all tags.
Without explicit settings, the automatic parser recognizes `v`, `ubuntu-`, `alpine-` and
`debian-` prefixes and retains suffixes as variants. Unknown vendor prefixes require an
explicit prefix. Glob tag filters are declarative and do not execute code.
`major_components` and `minor_components` are numeric prefix lengths (0–3) defining update
families for numeric profiles; CalVer uses its fixed calendar classification.
`channels` supports `stable` and `prerelease`. Prereleases additionally require
`include_prerelease: true` (in version or defaults). SemVer-like `alpha`, `beta`, `rc`,
`a`, `b` and `dev` markers are recognized. Arbitrary vendor prerelease conventions may need
an explicit policy and can remain unsupported. No broad claim of full arbitrary tag grammar
support is made. `preserve_variant: false` permits suffix and recognized prefix migrations;
use `tag_patterns` to restrict the target family. An explicitly configured literal prefix or
suffix still constrains every tag, including the current reference.

Floating tags (`latest`, `stable`, `stable-alpine`, `edge`, `main`, `master`, `nightly`,
`rolling`, with optional suffixes) use manifest-digest monitoring. The first observation
establishes a baseline and emits no update. Later changes emit `digest_changed`, rather than
inventing a release version. This event is reported on the observation that changes the
baseline; the next unchanged scan has no new digest event. With `monitor_immutable: true`,
the same mechanism also monitors numeric tags. Persistent digest state must be writable.

The digest is the SHA-256 of the complete OCI index or manifest bytes. For an index,
platform descriptors are listed but child manifests are not fetched. Index-to-manifest
scope changes reestablish the baseline. This avoids comparing one host architecture to
another. Updates to an unrelated architecture can still change the index digest.
Digest-pinned references are verified against manifest bytes and reported as `pinned`;
they have no floating target and receive no speculative tag upgrades. A tag plus digest
is checked by its pinned digest. A missing configured tag or registry failure is unknown,
never proof that the image is current.
