# Managed toolkit provisioning

Toolkit executables belong to the checkout's `js/tools` directory. Runtime
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

The remaining toolkit still needs platform-specific release verification; the
current top-level installer retains its legacy platform restriction until that
selection is implemented. This aria2 repair alone does not establish portable
installation of the entire toolkit.