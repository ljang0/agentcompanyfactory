"""Same-origin stand-ins for images a worker VM can never fetch.

A worker VM has no route off the host (QEMU ``restrict=on``), so every absolute ``http(s)``
image reference an app clone ships -- avatar services, CDN logos -- renders as a broken box:
one live check found 80 of them in a single Slack clone, plus a Gmail logo, a Drive logo and
several empty ``src=""`` attributes. They come from the clones' own demo data and markup, not
from our seeded state.

Rather than patch a URL in every clone, the worker proxy rewrites image references in the
HTML, JavaScript and CSS it serves to ``/__image/<token>`` and answers that route itself with
a small deterministic SVG. Nothing here ever touches the network.

Only references that look like images or stylesheets move: a path ending in an image extension,
a known avatar/logo host, or an off-host stylesheet. API endpoints, link targets and every other
URL are left exactly as the app wrote them, and the proxy's own routes (``/state``, ``/post``,
``/go``, ``/__image``) are never rewritten.

A stylesheet counts because a webfont is not decoration to a gate that judges looks: 31 of the 98
clones pull ``https://fonts.googleapis.com/css2?family=...``, which a guest cannot fetch, so the
host rendered the page in the intended face and every guest rendered it in a fallback -- the
visual gate was judging typography no worker would ever see. The link is answered here with an
empty same-origin stylesheet, so both sides render the fallback and neither waits for a request
that will not arrive.
"""

import colorsys
import hashlib
import re
from urllib.parse import parse_qsl, urlsplit

ROUTE = "/__image/"

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico", ".bmp")

# Hosts that only ever serve pictures: an avatar service, a stock-photo CDN, a logo bucket.
# A subdomain counts (``ssl.gstatic.com`` for ``gstatic.com``).
IMAGE_HOSTS = (
    "picsum.photos",
    "ui-avatars.com",
    "gravatar.com",
    "images.unsplash.com",
    "gstatic.com",
    "upload.wikimedia.org",
    "placehold.co",
    "placeholder.com",
    "placekitten.com",
    "dummyimage.com",
    "loremflickr.com",
    "robohash.org",
    "avatars.githubusercontent.com",
    "avatars.dicebear.com",
    "api.dicebear.com",
    # 97 references across 10 clones, and the one that shows: instagram's guest screenshot has a
    # broken-image icon where an avatar belongs. The path is /150?u=..., with no extension, so
    # only the host puts it here.
    "pravatar.cc",
    "fbcdn.net",
)

# Stylesheets a worker VM can never fetch. A webfont link is the whole of this in practice, and
# an empty same-origin stylesheet is what both sides then render.
STYLE_HOSTS = ("fonts.googleapis.com",)
STYLE_EXTENSIONS = (".css",)
STYLE_MARK = "_c"  # a token ending this way is answered as an empty stylesheet, not a picture

# Our own routes, and anything already rewritten, stay as they are.
RESERVED_PATHS = (ROUTE, "/state", "/post", "/go")

_URL = re.compile(rb"""https?://[^\s'"`<>()\[\]{}\\|^$]+""")
_TRAILING = b".,;:!?"
_IMG_TAG = re.compile(rb"<img\b[^>]*>", re.IGNORECASE)
_SRC = re.compile(rb"""(?P<lead>\ssrc\s*=\s*)(?P<quote>["'])(?P<value>[^"']*)(?P=quote)""", re.IGNORECASE)
_ALT = re.compile(rb"""\salt\s*=\s*(?P<quote>["'])(?P<value>[^"']*)(?P=quote)""", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NAME_PARAMS = ("name", "initials", "text", "seed")

SVG_SIZE = 200
CACHE_CONTROL = "public, max-age=31536000, immutable"
CONTENT_TYPE = "image/svg+xml; charset=utf-8"
FONT = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def initials(text):
    """One or two uppercase letters for a name; empty when a name cannot be read from it."""
    words = [w for w in re.split(r"[^A-Za-z]+", text or "") if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:1].upper()
    return (words[0][:1] + words[-1][:1]).upper()


LOGO_FIELDS = ("logo", "logourl", "icon", "iconurl", "image", "imageurl", "image_url", "thumbnail")


def _token(seed, hint="", person=True):
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:10]
    letters = initials(hint)
    token = f"{digest}-{letters}" if letters else digest
    return token if person else token + LOGO_MARK


def url_token(url):
    """A short stable token for one image URL, carrying initials when the URL names a person."""
    hint = ""
    try:
        query = dict(parse_qsl(urlsplit(url).query))
    except ValueError:
        query = {}
    for key in _NAME_PARAMS:
        if query.get(key):
            hint = query[key]
            break
    return _token(url, hint)


def alt_token(alt):
    """A token for an empty ``src``: the alt text keeps an avatar's initials."""
    return _token("alt:" + (alt or ""), alt or "")


def is_token(value):
    """Only a plain short token reaches the SVG: no traversal, no markup, no network."""
    return bool(_TOKEN.fullmatch(value or ""))


def is_image_url(url):
    """True when this absolute URL points at a picture the worker VM could never load."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    path = parts.path.lower()
    reserved = [route.rstrip("/") for route in RESERVED_PATHS]
    if any(path == route or path.startswith(route + "/") for route in reserved):
        return False
    if path.endswith(IMAGE_EXTENSIONS):
        return True
    try:
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return any(host == known or host.endswith("." + known) for known in IMAGE_HOSTS)


def is_style_url(url):
    """True when this absolute URL points at a stylesheet the worker VM could never load."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if parts.path.lower().endswith(STYLE_EXTENSIONS):
        return True
    try:
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return any(host == known or host.endswith("." + known) for known in STYLE_HOSTS)


def style_token(url):
    """A token for one stylesheet URL; the mark is what makes the route answer as CSS."""
    return _token(url, "", person=False)[: -len(LOGO_MARK)] + STYLE_MARK


def _replace_url(match):
    url = match.group(0)
    trailing = b""
    while url and url[-1:] in _TRAILING:
        url, trailing = url[:-1], url[-1:] + trailing
    text = url.decode("ascii", "replace")
    if is_image_url(text):
        return (ROUTE + url_token(text)).encode() + trailing
    if is_style_url(text):
        return (ROUTE + style_token(text)).encode() + trailing
    return match.group(0)


def rewrite_urls(data):
    """Point every absolute image or stylesheet URL in a served body at our own route."""
    return _URL.sub(_replace_url, data)


def _replace_img(match):
    """``<img src="">`` is a broken box too, and an alt text names the person in the picture."""
    tag = match.group(0)
    src = _SRC.search(tag)
    if not src:
        return tag
    alt = _ALT.search(tag)
    text = alt.group("value").decode("utf-8", "replace") if alt else ""
    value = src.group("value").strip()
    if not value:
        token = alt_token(text)
    elif value.startswith(ROUTE.encode()) and b"-" not in value[len(ROUTE) :] and initials(text):
        # A URL we just replaced, on an element that says whose face it was: keep the initials.
        token = value[len(ROUTE) :].decode() + "-" + initials(text)
    else:
        return tag
    quote = src.group("quote")
    replaced = src.group("lead") + quote + (ROUTE + token).encode() + quote
    return tag[: src.start()] + replaced + tag[src.end() :]


def rewrite_img_tags(data):
    """Fill empty ``<img>`` sources, and give a placeholder the initials its alt text carries."""
    return _IMG_TAG.sub(_replace_img, data)


def rewrite_html(data):
    return rewrite_img_tags(rewrite_urls(data))


def rewrite_text(data):
    """JavaScript and CSS carry the demo avatars and logos; only their URLs move."""
    return rewrite_urls(data)


def rewrite_kind(content_type):
    """``html``, ``text`` or None: which rewrite a response of this content type takes."""
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    if kind in ("text/html", "application/xhtml+xml"):
        return "html"
    if "javascript" in kind or "ecmascript" in kind or kind == "text/css":
        return "text"
    return None


def _colour(byte):
    """A readable flat background for white text, as hex: SVG colour parsing takes no chances."""
    red, green, blue = colorsys.hls_to_rgb(byte / 256, 0.42, 0.44)
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in (red, green, blue))


LOGO_MARK = "_l"  # a token ending this way stands for a workspace or an app, not a person


def placeholder_svg(token):
    """A flat 200x200 SVG for one token: initials on a colour of their own, or a plain tile."""
    # Initials ride in the token's suffix; a token without one is a picture of nobody. A logo
    # says so in its own suffix, so an unnamed one is a tile rather than somebody's face.
    person = not token.endswith(LOGO_MARK)
    body_token = token[: -len(LOGO_MARK)] if not person else token
    suffix = body_token.rpartition("-")[2] if "-" in body_token else ""
    letters = "".join(c for c in suffix if c.isascii() and c.isalpha()).upper()[:2]
    digest = hashlib.sha256(token.encode()).digest()
    if letters:
        body = (
            f'<rect width="{SVG_SIZE}" height="{SVG_SIZE}" rx="28" fill="{_colour(digest[0])}"/>'
            f'<text x="100" y="104" fill="#ffffff" font-family="{FONT}" font-size="82" '
            f'font-weight="600" text-anchor="middle" dominant-baseline="central">{letters}</text>'
        )
    else:
        # A grey box inside a grey box reads as a broken image, which is the very thing this
        # placeholder exists to prevent. A person silhouette reads as the default avatar every
        # real product shows, but only where a person belongs: on a workspace or app logo it
        # reads as the wrong picture, so an unnamed one gets a plain tile of its own colour.
        body = f'<rect width="{SVG_SIZE}" height="{SVG_SIZE}" rx="28" fill="{_colour(digest[0])}"/>'
        if person:
            body += (
                '<circle cx="100" cy="80" r="30" fill="#ffffff" fill-opacity="0.92"/>'
                '<path d="M44 168a56 56 0 0 1 112 0z" fill="#ffffff" fill-opacity="0.92"/>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_SIZE}" height="{SVG_SIZE}" '
        f'viewBox="0 0 {SVG_SIZE} {SVG_SIZE}" role="img" aria-label="image">{body}</svg>'
    ).encode()


# Fields whose value is a picture. A record that carries one of these with an empty string makes
# the app render <img src=""> at runtime, which no rewrite of the served HTML can reach: the tag
# does not exist until the page hydrates. Filling them in the state the app is handed does.
IMAGE_FIELDS = (
    "avatar",
    "avatarurl",
    "avatar_url",
    "icon",
    "iconurl",
    "image",
    "imageurl",
    "image_url",
    "logo",
    "logourl",
    "photo",
    "photourl",
    "photo_url",
    "picture",
    "profileimage",
    "thumbnail",
)
NAME_FIELDS = ("name", "fullname", "full_name", "username", "displayname", "display_name", "title")
# The plural of every picture field. Only string values under a singular key ever moved, so a
# record's ``avatar`` was rewritten while the listing photos beside it at ``images[0]`` were not:
# 149 off-host picture URLs sit in lists like this across the seeded worlds on disk (amazon 130,
# xiaohongshu 11, weibo 5, wechat 3), and every one of them is a broken box in a guest.
IMAGE_LIST_FIELDS = tuple(field + "s" for field in IMAGE_FIELDS)


def _flat(fields):
    return {field.lower().replace("_", "") for field in fields}


def _person_hint(record):
    """The name beside a picture field, so the placeholder can wear that person's initials."""
    wanted = {f.replace("_", "") for f in NAME_FIELDS}
    for key, value in record.items():
        if key.lower().replace("_", "") in wanted and isinstance(value, str) and value.strip():
            return value
    return ""


def fill_image_fields(state):
    """Give every empty or unreachable picture field in a state a placeholder it can draw.

    Walks the whole state, so it covers user records, workspace logos and channel icons alike.
    Values that already point somewhere the browser can reach (a relative path, a data URI) are
    left exactly as they are. Only fields the state already carries are filled: adding one a
    record never had would change the shape every comparison in the proxy is built on. Returns a
    new object; the caller's state is untouched.

    **Two pieces of evidence, and only one of them is the field's name.** A value that is an
    absolute URL at a picture is a picture whatever the field is called, and a list of names
    could never keep up: measured across every seeded state on disk, 232 of 441 off-host image
    URLs sat under names this module did not have -- ``src`` (147), ``imageSrc`` (44),
    ``thumbnailUrl`` (21), ``mediaUrl`` (18), ``url`` (2), across 5 app states in 5 companies --
    and every one is a broken box in a guest with ``restrict=on``. Adding those five would have
    been the third round of the same gap; the note under ``IMAGE_LIST_FIELDS`` records the
    second. So the value decides, and the name is kept for the one question a value cannot
    answer: an **empty** string, which is a picture only because its field says so.
    """
    if isinstance(state, dict):
        hint = _person_hint(state)
        filled = {}
        for key, value in state.items():
            flat = key.lower().replace("_", "")
            named = flat in _flat(IMAGE_FIELDS) or flat in _flat(IMAGE_LIST_FIELDS)
            # A silhouette only where a person belongs. A named avatar earns one; a picture found
            # by its value, under a name nobody vouched for, is a tile -- a face on a product row
            # is the wrong picture, which is the thing this module exists to avoid.
            person = flat in _flat(IMAGE_FIELDS) and flat not in _flat(LOGO_FIELDS)
            if isinstance(value, str):
                replaced = _picture(value, key, hint, person, named=named)
                if replaced is not None:
                    filled[key] = replaced
                    continue
            elif isinstance(value, list):
                # A list of pictures is a gallery, not a face: every entry gets a plain tile.
                filled[key] = [
                    (
                        _picture(item, f"{key}[{index}]", hint, person=False, named=named) or item
                        if isinstance(item, str)
                        else fill_image_fields(item)
                    )
                    for index, item in enumerate(value)
                ]
                continue
            filled[key] = fill_image_fields(value)
        return filled
    if isinstance(state, list):
        return [fill_image_fields(item) for item in state]
    return state


def _picture(value, key, hint, person, *, named=False):
    """The placeholder this value earns, or None to leave the value exactly as it is.

    ``named`` says the field's own name vouches for it being a picture, which is what decides an
    empty string. A non-empty value speaks for itself: ``is_image_url`` is the whole test, and it
    already refuses a relative path, a data URI, a non-picture URL and this proxy's own routes.
    """
    text = value.strip()
    if is_image_url(text):
        # The record names the person; prefer that over anything in the URL.
        return ROUTE + (_token(text, hint, person) if hint else _token(text, "", person))
    if not text and named:
        return ROUTE + _token(f"{hint}|{key}", hint, person)
    return None


def strip_placeholders(state, original):
    """Undo :func:`fill_image_fields` on a state coming back from an app.

    The app is served pictures it can draw; the store keeps what the world actually says. Without
    this, the first save writes ``/__image/...`` into the shared records and the seeded value is
    gone. A field the app genuinely changed to something else is left alone.
    """
    if isinstance(state, dict):
        source = original if isinstance(original, dict) else {}
        cleaned = {}
        for key, value in state.items():
            if isinstance(value, str) and value.startswith(ROUTE):
                if key in source:
                    cleaned[key] = source[key]
                # A key we added to a record that never had one is dropped entirely.
                continue
            cleaned[key] = strip_placeholders(value, source.get(key))
        return cleaned
    if isinstance(state, list):
        source = original if isinstance(original, list) else []
        cleaned = []
        for index, item in enumerate(state):
            prior = source[index] if index < len(source) else None
            if isinstance(item, str) and item.startswith(ROUTE):
                # A gallery entry we replaced on the way out: the store keeps what the world says.
                cleaned.append(prior if isinstance(prior, str) else item)
            else:
                cleaned.append(strip_placeholders(item, prior))
        return cleaned
    return state


# Interface state, not world state: what a person had selected, dragged or was uploading. A seed
# that carries these opens the app mid-gesture -- Drive greets a worker with "1 selected" and an
# upload toast nobody started -- which reads as a machine left it that way.
TRANSIENT = {
    "selecteditems": [],
    "selectedids": [],
    "selectedfiles": [],
    "selection": [],
    "uploadqueue": [],
    "clipboard": None,
    "isdragging": False,
    "draggingid": None,
    "undostack": [],
    "redostack": [],
    "openmodal": None,
    "activemodal": None,
    "contextmenu": None,
    "toasts": [],
}


def reset_transient(state):
    """Hand the app a workspace at rest: nothing selected, nothing dragging, no half-done upload.

    Only top-level keys are touched, and only ones whose current value is the same shape as the
    resting value, so a collection that happens to share a name is never emptied.
    """
    if not isinstance(state, dict):
        return state
    reset = {}
    for key, value in state.items():
        want = TRANSIENT.get(key.lower().replace("_", ""), Ellipsis)
        if want is Ellipsis or value == want:
            reset[key] = value
        elif isinstance(want, list) and isinstance(value, list):
            reset[key] = []
        elif want is None or isinstance(want, bool):
            reset[key] = want
        else:
            reset[key] = value
    return reset


STYLE_CONTENT_TYPE = "text/css; charset=utf-8"
STYLE_BODY = b"/* off-host stylesheet: the guest cannot fetch it, so neither does the host */\n"


def response(token):
    """The served placeholder: status, body and headers, or a 404 for a token we never issued."""
    if not is_token(token):
        return 404, b"Not found", [("Content-Type", "text/plain; charset=utf-8")]
    if token.endswith(STYLE_MARK):
        return 200, STYLE_BODY, [("Content-Type", STYLE_CONTENT_TYPE), ("Cache-Control", CACHE_CONTROL)]
    return (
        200,
        placeholder_svg(token),
        [("Content-Type", CONTENT_TYPE), ("Cache-Control", CACHE_CONTROL)],
    )
