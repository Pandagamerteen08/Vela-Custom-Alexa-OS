# Cutting a release

A release is a tag, a pack attached to that tag, and a checksum file that makes
"did my download arrive intact" a one-liner. Nothing else.

## The one-minute version

```sh
# 1. make sure the tree is clean and the host checks pass
python tools/vela.py test

# 2. bump the version in CHANGELOG.md, commit it
git commit -am "release 0.1.0"

# 3. tag and push - CI does the rest
git tag -a v0.1.0 -m "0.1.0"
git push origin main --tags
```

`.github/workflows/release.yml` then builds the pack in the pinned container and
attaches `vela-0.1.0.tar.gz`, the matching `.zip`, `SHA256SUMS.txt` and
`release.json` to the GitHub Release for that tag. It uses the workflow's own
`GITHUB_TOKEN` — no personal access token is stored anywhere.

## What the pack contains

```
vela-<version>/
  images/            boot.img, boot.img.dev, root.squashfs, manifest.json,
                     initramfs.cpio.gz  (whichever were built)
  RELEASE.md         generated: what is inside, install steps, known limits
  SHA256SUMS.txt     sha256sum(1) and shasum(1) compatible
  README.md  CHANGELOG.md  LICENSE  Makefile  Dockerfile
  docs/  kernel/  initramfs/  rootfs/  src/  tests/  tools/
```

The docs and tools are in there on purpose: someone who downloads a release
should not need the repository to install it, and someone auditing it should not
need to trust a tarball they cannot compare.

## Doing it by hand

When CI is unavailable, or the pack needs to come from a machine with the kernel
toolchain:

```sh
# Linux host with a cross compiler:
make rootfs && make boot           # or: vela.py docker release
python tools/vela.py release --version 0.1.0 --out dist

gh release create v0.1.0 dist/* \
   --title "Vela 0.1.0" \
   --notes-file dist/vela-0.1.0/RELEASE.md \
   --prerelease
```

`--prerelease` is not modesty. Until a unit has booted and `tools/measure.sh`
has printed real numbers, no build here deserves to be called stable.

## Verifying a release, as a user

```sh
tar xzf vela-0.1.0.tar.gz && cd vela-0.1.0
sha256sum -c SHA256SUMS.txt        # Linux
shasum -a 256 -c SHA256SUMS.txt    # macOS
less RELEASE.md
```

If `images/` is empty, that build was cut on a host without the toolchain and the
release notes say so — it is a docs-and-tools release, not an installable one.

## Versioning

`MAJOR.MINOR.PATCH`, and while pre-hardware the minor bumps are allowed to break
the on-disk formats:

- **`slot.ptr` format** — the 16-byte dual record in `src/slotctl/slotctl.c`.
  Changing it means an existing device needs `slotctl init` again.
- **`manifest.json` format** — the rootfs/slot manifest. A format change must
  bump `"format"`, and the initramfs must accept the old one for one release.
- **kernel config fragments** — not versioned, but a fragment change that removes
  a symbol is a behaviour change and belongs in the CHANGELOG under *Changed*.

## Signing, and what is missing

Slot manifests are specified as Ed25519-signed (`docs/design.md` §7)
and the initramfs is specified to refuse unsigned ones in production mode. That
is not implemented yet, so today every build prints:

```
"note": "unsigned local build; production manifests are Ed25519-signed"
```

Do not describe a release as signed until `tools/` contains the verifier and the
initramfs calls it before mounting a slot. A release page that overstates its
integrity is worse than one that admits it has none.

## Release checklist

- [ ] `python tools/vela.py test` green on Linux, macOS and Windows (CI does this)
- [ ] `CHANGELOG.md` has an entry with a date, and the version matches the tag
- [ ] `make sizes` does not report anything over a gate
- [ ] `RELEASE.md` lists which artifacts are present and which were not built
- [ ] the "known limits" section still says `velad` does not exist (update when it does)
- [ ] tag is annotated (`git tag -a`), not lightweight, so the tag carries a message
