# Desktop setup check

The [documented image build](../../docs/RUNTIME.md) was run on September 14, 2026,
using the pinned Ubuntu cloud image and committed cloud-init recipe.

- [BUILD.json](BUILD.json) records the input, recipe and resulting image hashes,
  plus the checks performed after provisioning.
- [guest-check.txt](guest-check.txt) contains the guest check output and installed
  Debian package versions.
- [DESKTOP-CHECK.json](DESKTOP-CHECK.json) records a subsequent boot of an owned
  overlay: X desktop, Chromium startup, SSH browser connection, page reading,
  text input and screenshot capture. No model calls were used.

These checks establish a working desktop/browser setup. They do not certify a
company world, worker app access or a successful team trial. The image itself is
not included in Git. Rebuild with the runtime guide and keep the generated build
report with any future measured run.
