# Managed toolkit provisioning

Toolkit executables belong to the checkout's `tools/bin` directory. Runtime
resolution prefers these files to PATH; PATH availability must not substitute
for provisioning. Archives and extracted executables are independently SHA-256
pinned. Initial downloads can use Python's urllib without an installed aria2.

## Verified aria2 assets

Version 1.37.0 from `abcfy2/aria2-static-build` GitHub releases supplies
`aria2-x86_64-linux-musl_static.zip` and
`aria2-aarch64-linux-musl_static.zip`, each containing `aria2c` at archive root.
Archive digests were checked against GitHub's release-asset digests; executable
digests are pinned in `js/tool_binaries.py`.

Both extracted ELF files were inspected with `readelf -l -d`: neither has a
PT_INTERP segment nor a dynamic section. These specific binaries therefore need
no shared libc, loader, or minimum glibc version, and can serve musl and glibc
Linux hosts of the matching architecture. This is verified binary structure,
not an inference from a musl filename. Native execution and real HTTP transfer
tests have been run on Linux x86_64 only. ARM64 compatibility is inspected and
fixture-tested, not natively executed. amd64/x86_64 and arm64/aarch64 aliases
select the same respective assets.

No Darwin aria2 asset is pinned. Unsupported targets fail explicitly; Android
assets are not substitutes for Linux releases. No system packages, compatibility
layers, or source builds are part of this contract.

## Release selection and incomplete targets

`just install-tool-binaries` and `just ensure-tools` use the same managed
installer. `just install` invokes it; none invokes a system package manager.
System copies never satisfy provisioning. Python urllib bootstraps aria2,
then the managed aria2 performs subsequent downloads. Runtime resolution and
startup checks prefer managed executables over PATH.

`js/tool_releases.json` pins archive and executable SHA-256 digests for ripgrep
15.2.0, fd 10.3.0, bat 0.25.0, fzf 0.65.2, and ast-grep 0.45.1. Linux x86_64
and aarch64 musl binaries for the first four were inspected using readelf:
no PT_INTERP or NEEDED entries. They work without a libc compatibility layer.
Darwin ARM64 archives are pinned but not executed on Linux. Digests were
compared to GitHub release API digests where published; bat's digests were
computed from the release downloads.

Ast-grep publishes no musl release in 0.45.1. Its GNU binaries require glibc
2.34 (x86_64) or 2.18 (aarch64). They are not selected for musl or an unknown
libc. The pinned Obscura 0.2.0 x86_64 stealth launcher requires glibc 2.35;
the worker requires 2.34, and both require libgcc_s. Neither is static.
Obscura ARM64 Linux and ARM64 macOS stealth assets exist, but their executable
hashes and dependency requirements are not verified here. Darwin aria2 is not
available from the pinned aria2 release. Unsupported/missing components are
reported individually and installation exits unsuccessfully after provisioning
the verified subset. An incomplete toolkit is never reported as complete.

## Integrity, repair, and publication

Archives are retained in `tools/bin/.archives` under their SHA-256 plus asset
name, verified before reuse. This avoids downloading large releases again to
repair a worker. Executable and companion hashes and executable permissions
are checked on every install. Missing/corrupt companions trigger repair even
when the launcher is intact. Only explicitly named regular archive members
are copied; archive paths and links are never extracted into the filesystem.
All files are verified before publication; each file is replaced atomically.
Multi-file publication is not a transaction across process termination or disk
failure: rerunning detects and repairs a partial companion update.

No native ARM64 or Darwin execution is claimed. Full portable provisioning is
blocked on the missing/unverified assets above, not silently substituted with
source builds or compatibility layers.
