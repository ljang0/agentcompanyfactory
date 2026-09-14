"""Capture a guest desktop over SSH, or a browser page over its existing CDP tunnel.

The default returns desktop pixels, matching xdotool coordinates. CDP is an
explicit alternative: its image excludes browser controls and other windows.
The CDP helper uses the Node WebSocket API already available on the hub host;
it does not start a browser or install packages.
"""

import base64
import io
import subprocess

from PIL import Image

# Each attempt has its own deadline. Copy the file through SSH stdout, then
# remove it even on failure. ImageGrab is not needed in the guest.
GUEST_CAPTURE = r"""
set -eu
unset DISPLAY
for socket in /tmp/.X11-unix/X*; do
    test -S "$socket" || continue
    export DISPLAY=":${socket##*/X}"
    break
done
test -n "${DISPLAY:-}" || { echo 'No X desktop' >&2; exit 1; }
if test -r /run/user/1000/gdm/Xauthority; then
    export XAUTHORITY=/run/user/1000/gdm/Xauthority
fi
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
capture_dir=$(mktemp -d)
trap 'rm -rf -- "$capture_dir"' EXIT
for tool in gnome-screenshot import; do
    command -v "$tool" >/dev/null 2>&1 || continue
    rm -f -- "$capture_dir/screen.png"
    if test "$tool" = gnome-screenshot; then
        timeout 8s gnome-screenshot -f "$capture_dir/screen.png" >&2 || continue
    else
        timeout 8s import -window root "$capture_dir/screen.png" >&2 || continue
    fi
    test -s "$capture_dir/screen.png" || continue
    cat -- "$capture_dir/screen.png"
    exit 0
done
echo 'Guest screenshot failed: gnome-screenshot and import were unavailable or failed' >&2
exit 1
"""

# A separate Node process gives discovery, WebSocket reads and capture one host
# deadline. Rebuild the WebSocket address using the forwarded port: Chrome may
# advertise its guest port instead. Never follow a host from the target listing.
_CDP_CAPTURE = r"""
const port = process.argv[1];
const base = `http://127.0.0.1:${port}`;
async function connect(target) {
    const path = new URL(target.webSocketDebuggerUrl).pathname;
    if (!path.startsWith('/devtools/page/')) throw Error('Invalid CDP page address');
    const ws = new WebSocket(`ws://127.0.0.1:${port}${path}`);
    await new Promise((resolve, reject) => {
        ws.onopen = resolve;
        ws.onerror = () => reject(Error('CDP connection failed'));
    });
    let id = 0;
    return {
        close: () => ws.close(),
        call: (method, params = {}) => new Promise((resolve, reject) => {
            const request = ++id;
            ws.onerror = () => reject(Error('CDP connection failed'));
            ws.onclose = () => reject(Error('CDP connection closed'));
            ws.onmessage = event => {
                try {
                    const reply = JSON.parse(event.data);
                    if (reply.id !== request) return;
                    if (reply.error) reject(Error(JSON.stringify(reply.error)));
                    else resolve(reply.result);
                } catch (error) { reject(error); }
            };
            ws.send(JSON.stringify({id: request, method, params}));
        })
    };
}
async function capture() {
    const response = await fetch(`${base}/json/list`);
    if (!response.ok) throw Error(`CDP discovery failed: ${response.status}`);
    const pages = (await response.json()).filter(page => page.type === 'page');
    for (const page of pages) {
        const client = await connect(page);
        try {
            const focus = await client.call('Runtime.evaluate', {
                expression: 'document.hasFocus()', returnByValue: true
            });
            if (!focus.result?.value && pages.length !== 1) continue;
            const result = await client.call('Page.captureScreenshot', {
                format: 'png', fromSurface: true, captureBeyondViewport: false
            });
            process.stdout.write(Buffer.from(result.data, 'base64'));
            return;
        } finally { client.close(); }
    }
    throw Error('No focused browser page; use the guest desktop capture');
}
capture().catch(error => { console.error(error.message); process.exitCode = 1; });
"""


def validate_png(png):
    """Reject empty, truncated or non-PNG output before it becomes an observation."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG":
                raise ValueError("not PNG")
            image.verify()
        with Image.open(io.BytesIO(png)) as image:
            image.load()
    except (OSError, ValueError, SyntaxError) as exc:
        raise RuntimeError("Guest screenshot did not return a complete PNG") from exc
    return png


def _capture(argv, *, env=None):
    try:
        result = subprocess.run(argv, env=env, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Guest screenshot exceeded 30 seconds") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not start screenshot command: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Guest screenshot failed ({result.returncode}): {detail}")
    return validate_png(result.stdout)


def screenshot(vm):
    """Return full desktop PNG bytes from a WorkerVM without changing its screen."""
    return _capture([*vm.ssh_base, GUEST_CAPTURE], env=vm.env)


async def screenshot_transport(transport, *, deadline):
    """Capture through the backend's SSH supervisor, keeping its deadline and cleanup."""
    result = await transport.run(GUEST_CAPTURE, deadline=deadline, limit=16 * 1024 * 1024)
    if result["exit_code"] or result["truncated"]["stdout"]:
        detail = base64.b64decode(result["stderr"]).decode(errors="replace").strip()
        raise RuntimeError(f"Guest screenshot failed or was truncated: {detail}")
    try:
        png = base64.b64decode(result["stdout"], validate=True)
    except ValueError as exc:
        raise RuntimeError("Guest screenshot was not valid base64") from exc
    return validate_png(png)


def cdp_screenshot(vm):
    """Return the focused browser page as PNG bytes through the launcher's tunnel.

    Requires Node with --experimental-websocket (available in Node 20.20.2).
    With one page, capture it even when the browser controls hold keyboard focus.
    Page coordinates differ from desktop coordinates; do not silently substitute
    this for a desktop observation when using xdotool.
    """
    port = vm.cdp_port
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError("VM needs a valid forwarded CDP port")
    return _capture(["node", "--experimental-websocket", "-e", _CDP_CAPTURE, str(port)])
