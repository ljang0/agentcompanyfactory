# Runtime assets

Reading the [worked example](../examples/sanmar/walkthrough/README.md) requires no
installation. `make smoke` uses the bundled Node fixture. Native company replay
uses the external hub; desktop teams additionally use a Linux VM image and a guest
browser. Acquire those assets separately as described below.

## Host requirements

The desktop launcher targets Linux on x86-64 with QEMU/KVM. Each worker gets two
virtual CPUs and 6,144 MiB of RAM by default. Four simultaneous workers therefore
allocate 24 GiB to guests, plus host memory for app servers, builds and model
clients. Image provisioning uses four virtual CPUs, 8 GiB RAM and a 32 GiB virtual
disk. Allow disk space for the downloaded cloud image, build overlay, standalone
image and trial overlays. Virtual disk capacity is not the same as physical usage.

On an Ubuntu host, the system dependencies are:

```sh
sudo apt-get update
sudo apt-get install -y git curl nodejs npm bubblewrap fonts-dejavu-core \
  qemu-system-x86 qemu-utils cloud-image-utils sshpass openssh-client netcat-openbsd
```

Use Node 22 and Python 3.13 to match CI; a distribution's default Node package may
be older. The trial user must have read/write access to `/dev/kvm`. The process
also needs local TCP sockets and the Linux namespaces used by bubblewrap.

## Public hub checkout

The published company uses [CUA-Gym](https://github.com/xlang-ai/CUA-Gym) at commit
`1e50b797200f8afd6f11ca8e3ee04412de97b0f2`. From this repository:

```sh
git clone --filter=blob:none --no-checkout https://github.com/xlang-ai/CUA-Gym.git \
  workspace/runtime/CUA-Gym
git -C workspace/runtime/CUA-Gym checkout --detach 1e50b797200f8afd6f11ca8e3ee04412de97b0f2
git -C workspace/runtime/CUA-Gym submodule update --init --recursive hub
git -C workspace/runtime/CUA-Gym rev-parse HEAD
git -C workspace/runtime/CUA-Gym/hub rev-parse HEAD
```

The second revision must be `b8207bfc1838f69b971986e9c489c8aa5d0460c1`.
The apps live in the [hub submodule](https://github.com/xlang-ai/CUA-Gym-Hub), so a
parent checkout without submodule initialization is insufficient.

Set these values in `configs/company.local.toml`:

```toml
[design]
hub_root = "../workspace/runtime/CUA-Gym/hub/websites"
hub_revision = "1e50b797200f8afd6f11ca8e3ee04412de97b0f2"
```

Update the existing `[design]` section rather than adding a duplicate. The runtime
copies and builds each selected app into its work directory; it does not modify
the upstream checkout. App builds may download npm dependencies. The first build
therefore needs network access even when the subsequent replay uses no models.
The upstream repository has its own licensing and notices.

## Build a desktop image

The committed [cloud-init recipe](../configs/desktop-cloud-init.yaml) provisions
Ubuntu GNOME on X11, SSH, desktop document viewers, xdotool and Python GTK 3. The
small [builder](../scripts/build_desktop.py) uses native QEMU and verifies the guest
before creating the requested output. It refuses an existing output path.

Download the pinned [Ubuntu 22.04 image release](https://cloud-images.ubuntu.com/releases/jammy/release-20260913/).
Its [published SHA-256 list](https://cloud-images.ubuntu.com/releases/jammy/release-20260913/SHA256SUMS)
contains the checksum used here:

```sh
mkdir -p workspace/runtime
curl --fail --location \
  https://cloud-images.ubuntu.com/releases/jammy/release-20260913/ubuntu-22.04-server-cloudimg-amd64.img \
  --output workspace/runtime/ubuntu-22.04-server-cloudimg-amd64.img
uv run python scripts/build_desktop.py \
  --cloud-image workspace/runtime/ubuntu-22.04-server-cloudimg-amd64.img \
  --sha256 9144540e8af7637d258b50dbabe82ce1aa6752c9574fedfb048270da0e087899 \
  --output workspace/runtime/company-desktop.qcow2
```

Provisioning needs Internet access for Ubuntu packages and can take tens of
minutes. The default timeout is two hours. The builder prints its work directory;
`provision.log` records package installation, `guest-check.txt` records installed
versions, and `BUILD.json` records the cloud image, recipe and output hashes.
An unsuccessful build retains diagnostics and does not create the requested image.

Ubuntu package repositories change, so the recipe reproduces the guest contract,
not identical disk bytes on every date. Preserve a successful image and its build
report for a measured run. Set `vm_base_image` to
`"../workspace/runtime/company-desktop.qcow2"` in the local config.

The guest account is `ga` (UID 1000), with the public evaluation password
`password123`. The trial launcher creates owned overlays, forwards SSH on loopback,
and restricts guest networking to its configured app services. Keep the base image
free of company data and account credentials.

## Select host and guest browsers

For host browser checks:

```sh
make browser
export COMPANY_ENVS_BROWSER="$(uv run python scripts/browser_path.py)"
```

For guest desktops, install the full browser and print its directory:

```sh
uv run playwright install chromium
uv run python scripts/browser_path.py --guest
```

Use the printed absolute directory for `design.vm_browser_dir`. The launcher
expects an executable named `chrome` in that directory and copies the complete
distribution into the guest. The dependency lock selects the Playwright version;
do not guess a browser cache revision. Host `--dump-dom` checks use the separate
headless shell because the measured full Chromium 151 build timed out on that
interface. A guest desktop needs the full graphical browser.

## Verify before running a company

```sh
uv run company-envs --config configs/company.local.toml doctor --profile desktop
uv run company-envs --config configs/company.local.toml preflight
make smoke
```

Doctor and preflight check prerequisites without inference. The image builder's
guest checks establish SSH, completed provisioning, UID and GTK/xdotool availability.
They do not establish company app access or a successful team trial. The pipeline's
world acceptance and trial gates measure those separately.

The [recorded setup check](../examples/runtime/README.md) contains the successful
build's hashes and package versions, followed by a separate guest browser check
covering page reads, text input and screenshots with zero model calls.

## Reproducing the historical SanMar snapshot

The September 14 inspection archive uses the pinned hub above. Its original
desktop image was built locally through the Gym-Anything workflow and amended
during development; it is not distributed with this repository. Its recorded
image identity is:

```text
file: base_ubuntu_gnome.qcow2
bytes: 6954614784
sha256: dbfa3aba3ce6dcb076c731a01c57977c57656e08b8a572ab1a3ce6da722b4230
```

The new recipe does not claim to reproduce that historical image byte for byte.
Native reference replay needs only the hub, not a VM image or model account.
Use [snapshot replay](COLLABORATOR.md) for that measured path. New desktop images
and browser versions need their own runtime checks before new trial results are
compared with the archived episode.
