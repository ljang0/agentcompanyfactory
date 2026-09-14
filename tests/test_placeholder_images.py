"""Image URLs a worker VM cannot reach become same-origin SVG placeholders at the proxy.

No VM, no network: the stub upstream serves the bodies an app clone ships, and the proxy is
the only thing under test.
"""

import json
import re
from urllib.error import HTTPError
from urllib.request import urlopen
from xml.etree import ElementTree

import pytest
from test_hub_concurrency import SID, STATE, USERS, response_proxy

from company_envs.world import placeholder_images as placeholders
from company_envs.world.hub_world import placeholder_images_enabled

PAGE = (
    b"<!doctype html><html><head></head><body>"
    b'<img src="https://picsum.photos/200/200?random=17" alt="Dana Ortiz">'
    b'<img src="" alt="Luis Prado">'
    b'<img src=" ">'
    b'<a href="https://example.test/handbook">Handbook</a>'
    b'<script>var api = "https://api.example.test/v1/tickets";</script>'
    b"</body></html>"
)
BUNDLE = (
    b'const logo = "https://ssl.gstatic.com/images/branding/googlelogo/logo.png";\n'
    b'const avatar = "https://ui-avatars.com/api/?name=Dana+Ortiz&size=36";\n'
    b'const save = "https://api.example.test/v1/state";\n'
)


def sources(page):
    return re.findall(rb'src="([^"]*)"', page)


@pytest.mark.parametrize("generic", [True, False])
def test_served_html_swaps_image_urls_and_leaves_every_other_url_alone(tmp_path, generic):
    with (
        response_proxy(tmp_path, body=PAGE, generic=generic) as (origin, _),
        urlopen(origin, timeout=5) as response,
    ):
        page = response.read()
    assert b"picsum.photos" not in page
    assert all(src.startswith(b"/__image/") for src in sources(page))
    assert b'href="https://example.test/handbook"' in page
    assert b'"https://api.example.test/v1/tickets"' in page
    if not generic:
        assert b'data-company-envs="identity"' in page


def test_empty_src_becomes_a_placeholder_that_keeps_the_alt_initials(tmp_path):
    with response_proxy(tmp_path, body=PAGE) as (origin, _):
        with urlopen(origin, timeout=5) as response:
            page = response.read()
        avatar, empty, blank = sources(page)
        assert avatar.endswith(b"-DO")  # the alt text names the face the URL would have shown
        assert empty.endswith(b"-LP")
        assert b"-" not in blank.removeprefix(b"/__image/")  # nothing to take initials from
        with urlopen(origin + empty.decode(), timeout=5) as response:
            assert b">LP<" in response.read()


@pytest.mark.parametrize(
    "content_type,body,gone,kept",
    [
        ("application/javascript", BUNDLE, b"ssl.gstatic.com", b"https://api.example.test/v1/state"),
        ("text/javascript;charset=utf-8", BUNDLE, b"ui-avatars.com", b"https://api.example.test/v1/state"),
        (
            "text/css",
            (
                b".hero{background-image:url(https://images.unsplash.com/photo-1.jpg);}"
                b'@import url("https://api.example.test/v1/state");'
            ),
            b"images.unsplash.com",
            b"https://api.example.test/v1/state",
        ),
    ],
)
def test_scripts_and_stylesheets_lose_only_their_image_urls(tmp_path, content_type, body, gone, kept):
    """An off-host webfont used to be kept here; it now moves too (see the stylesheet test)."""
    with (
        response_proxy(tmp_path, body=body, content_type=content_type) as (origin, _),
        urlopen(origin + "/assets/app.js", timeout=5) as response,
    ):
        served = response.read()
    assert gone not in served
    assert kept in served
    assert b"/__image/" in served


def test_placeholder_route_is_deterministic_svg_and_never_reaches_the_app(tmp_path):
    token = placeholders.url_token("https://picsum.photos/200/200?random=17")
    with response_proxy(tmp_path) as (origin, calls):
        with urlopen(f"{origin}/__image/{token}", timeout=5) as response:
            first, headers = response.read(), response.headers
        with urlopen(f"{origin}/__image/{token}", timeout=5) as response:
            assert response.read() == first
        named = placeholders.alt_token("Dana Ortiz")
        with urlopen(f"{origin}/__image/{named}", timeout=5) as response:
            other = response.read()
        with pytest.raises(HTTPError) as refused:
            urlopen(f"{origin}/__image/..%2Fetc", timeout=5)
    assert refused.value.code == 404
    assert calls == []  # the app was never asked for a picture
    assert headers["Content-Type"] == "image/svg+xml; charset=utf-8"
    assert "max-age=31536000" in headers["Cache-Control"]
    assert first.startswith(b"<svg") and b'width="200"' in first
    assert other != first  # a token carrying initials draws its own colour and letters


def test_a_token_with_no_initials_draws_a_neutral_square_with_no_text():
    plain = placeholders.placeholder_svg(placeholders.url_token("https://picsum.photos/200/200?random=1"))
    named = placeholders.placeholder_svg(placeholders.alt_token("Dana Ortiz"))
    assert b"<text" not in plain  # nothing to spell: no stray letters from the hash
    assert b">DO<" in named
    assert ElementTree.fromstring(plain) is not None and ElementTree.fromstring(named) is not None


def test_two_urls_get_two_tokens_and_the_same_url_gets_the_same_one():
    one = placeholders.url_token("https://picsum.photos/200/200?random=1")
    assert one == placeholders.url_token("https://picsum.photos/200/200?random=1")
    assert one != placeholders.url_token("https://picsum.photos/200/200?random=2")
    assert placeholders.url_token("https://ui-avatars.com/api/?name=Dana+Ortiz").endswith("-DO")
    named = [placeholders.alt_token(name) for name in ("Dana Ortiz", "Luis Prado")]
    assert named[0] != named[1]
    assert placeholders.placeholder_svg(named[0]) != placeholders.placeholder_svg(named[1])


@pytest.mark.parametrize(
    "url,image",
    [
        ("https://picsum.photos/200/200?random=4", True),
        ("http://ssl.gstatic.com/images/branding/logo.png", True),
        ("https://upload.wikimedia.org/wikipedia/commons/a/b/Logo.svg", True),
        ("https://cdn.example.test/assets/hero.webp", True),
        ("https://www.gravatar.com/avatar/abc?d=mp", True),
        ("https://api.example.test/v1/tickets", False),
        ("https://example.test/handbook", False),
        ("https://fonts.googleapis.com/css?family=Inter", False),
        ("http://www.w3.org/2000/svg", False),
        ("https://app.internal/__image/abc123", False),
        ("ftp://example.test/logo.png", False),
    ],
)
def test_only_image_references_are_rewritten(url, image):
    assert placeholders.is_image_url(url) is image


def test_the_served_state_carries_pictures_the_browser_can_draw(tmp_path):
    """An app builds its own <img> tags at runtime, so rewriting the HTML cannot reach them; the
    state it is handed has to carry something drawable in the first place."""
    stored = {**STATE, "settings": {"logo": "https://picsum.photos/200/200?random=9"}}
    body = json.dumps({"stored_state": stored}).encode()
    with (
        response_proxy(tmp_path, body=body, content_type="application/json") as (origin, _),
        urlopen(f"{origin}/state?sid={SID}", timeout=5) as response,
    ):
        served = json.load(response)
    assert served["stored_state"]["settings"]["logo"].startswith("/__image/")
    assert served["stored_state"]["currentUser"]["id"] == USERS[1]["id"]


@pytest.mark.parametrize("generic", [True, False])
def test_flag_off_restores_the_clone_urls_and_the_route(tmp_path, generic):
    with response_proxy(tmp_path, body=PAGE, generic=generic, placeholder_images=False) as (origin, calls):
        with urlopen(origin, timeout=5) as response:
            page = response.read()
        with urlopen(f"{origin}/__image/abc123", timeout=5) as response:
            response.read()
    assert b'src="https://picsum.photos/200/200?random=17"' in page
    assert b'src=""' in page
    # No placeholder route with the flag off: the request goes to the app, as it used to.
    assert any(call.startswith("/__image/abc123") for call in calls)


def test_flag_defaults_to_on_and_a_config_can_turn_it_off(tmp_path):
    assert placeholder_images_enabled(tmp_path) is True  # no config.toml
    (tmp_path / "config.toml").write_text("[design]\nidentity = 'shared_session'\n")
    assert placeholder_images_enabled(tmp_path) is True
    (tmp_path / "config.toml").write_text("[design]\nplaceholder_images = false\n")
    assert placeholder_images_enabled(tmp_path) is False


def test_empty_picture_fields_in_served_state_get_something_to_draw():
    """A React app renders <img src=""> from an empty avatar; no rewrite of the HTML can reach it."""
    from company_envs.world.placeholder_images import ROUTE, fill_image_fields

    state = {
        "users": [
            {"name": "Tessa Whitfield", "avatar": ""},
            {"name": "Celia Renshaw", "avatar": "https://picsum.photos/200/200?random=4"},
            {"name": "Ann Reyes", "avatar": "/assets/ann.png"},
        ],
        "workspace": {"title": "Harborlight", "logo": "   "},
        "messages": [{"body": "see https://example.test/report"}],
    }
    filled = fill_image_fields(state)
    tessa, celia, ann = filled["users"]
    assert tessa["avatar"].startswith(ROUTE) and tessa["avatar"].endswith("-TW")
    assert celia["avatar"].startswith(ROUTE) and celia["avatar"].endswith("-CR")
    assert ann["avatar"] == "/assets/ann.png"  # already reachable, left alone
    assert filled["workspace"]["logo"].startswith(ROUTE)
    assert filled["messages"] == state["messages"]  # not a picture field, untouched
    assert state["users"][0]["avatar"] == ""  # the caller's state is not modified
    assert fill_image_fields(filled) == filled  # applying it twice changes nothing


def test_a_picture_field_a_record_never_had_is_not_invented():
    """Adding a key changes the shape every comparison in the proxy is built on; only fill what is
    already there. An app drawing an empty avatar from its own defaults is the clone's defect."""
    from company_envs.world.placeholder_images import fill_image_fields

    original = {"users": [{"id": "u1", "name": "Tessa Whitfield"}]}
    assert fill_image_fields(original) == original


def test_a_logo_is_a_tile_and_a_person_is_a_silhouette():
    """The judge read a person's face on a Drive logo as the wrong picture, and it was right."""
    from company_envs.world.placeholder_images import fill_image_fields, is_token, placeholder_svg

    filled = fill_image_fields(
        {
            "workspace": {"title": "Harborlight", "logo": ""},
            "users": [{"id": "u1", "name": "", "avatar": ""}],
        }
    )
    logo = filled["workspace"]["logo"].rsplit("/", 1)[1]
    face = filled["users"][0]["avatar"].rsplit("/", 1)[1]
    assert is_token(logo) and is_token(face)
    assert b"circle" not in placeholder_svg(logo), "a workspace has no face"
    assert b"circle" in placeholder_svg(face), "a nameless person still gets the default avatar"


def test_the_app_opens_at_rest_not_mid_gesture():
    """Drive greeted a worker with '1 selected' and an upload toast nobody started."""
    from company_envs.world.placeholder_images import reset_transient

    at_rest = reset_transient(
        {
            "items": [{"id": "f1"}, {"id": "f2"}],
            "selectedItems": ["f1"],
            "uploadQueue": [{"name": "scan.pdf"}],
            "isDragging": True,
            "viewMode": "list",
        }
    )
    assert at_rest["selectedItems"] == [] and at_rest["uploadQueue"] == []
    assert at_rest["isDragging"] is False
    assert at_rest["items"] == [{"id": "f1"}, {"id": "f2"}] and at_rest["viewMode"] == "list"
    assert reset_transient({"selection": "a string, not a selection"})["selection"] == (
        "a string, not a selection"
    )


STYLED = (
    b"<!doctype html><html><head>"
    b'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    b'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap">'
    b'<link rel="stylesheet" href="https://cdn.example.test/lib/grid.css">'
    b"</head><body><h1>Bamboo</h1></body></html>"
)


def test_an_off_host_stylesheet_is_answered_as_an_empty_same_origin_one(tmp_path):
    """Only images moved, so 31 of the 98 clones kept a fonts.googleapis.com link a guest cannot
    fetch: the host rendered the intended face and every guest a fallback, and the visual gate
    judged typography no worker would ever see."""
    with response_proxy(tmp_path, body=STYLED) as (origin, _):
        with urlopen(origin, timeout=5) as response:
            page = response.read()
        assert b"fonts.googleapis.com" not in page and b"cdn.example.test" not in page
        hrefs = re.findall(rb'href="([^"]*)"', page)
        sheets = [href for href in hrefs if href.startswith(b"/__image/")]
        assert len(sheets) == 3  # the two stylesheets and the gstatic preconnect, all local now
        css = next(href for href in sheets if href.endswith(placeholders.STYLE_MARK.encode()))
        with urlopen(origin + css.decode(), timeout=5) as response:
            assert response.headers["Content-Type"].startswith("text/css")
            assert b"off-host stylesheet" in response.read()


def test_an_avatar_service_without_an_extension_still_moves():
    """i.pravatar.cc: 97 references across 10 clones, and a broken-image icon in instagram's
    guest screenshot. The path is /150?u=..., so only the host list can catch it."""
    assert placeholders.is_image_url("https://i.pravatar.cc/150?u=amelia")
    assert placeholders.is_image_url("https://static.xx.fbcdn.net/rsrc.php/v3/y1/l.png")
    assert not placeholders.is_style_url("https://i.pravatar.cc/150?u=amelia")
    assert placeholders.is_style_url("https://fonts.googleapis.com/css2?family=Inter")
    assert placeholders.is_style_url("https://cdn.example.test/lib/grid.css")
    assert not placeholders.is_style_url("https://api.example.test/v1/state")


def test_a_gallery_of_pictures_is_filled_and_given_back_untouched():
    """fill_image_fields moved only string values under singular keys, so a listing's photos at
    images[0] were never rewritten while the same record's avatar was: 149 off-host picture URLs
    sit in lists like this across the seeded worlds on disk (amazon 130, xiaohongshu 11)."""
    seed = {
        "listings": [
            {
                "id": "item_1",
                "title": "Vintage Game Boy Color",
                "avatar": "https://i.pravatar.cc/80?u=seller",
                "images": ["https://picsum.photos/400?random=a", "", "/local/photo.png"],
            }
        ]
    }
    served = placeholders.fill_image_fields(seed)
    photos = served["listings"][0]["images"]
    assert [p.startswith(placeholders.ROUTE) for p in photos] == [True, True, False]
    assert photos[2] == "/local/photo.png"  # already reachable, left exactly as it was
    assert served["listings"][0]["avatar"].startswith(placeholders.ROUTE)
    # A gallery entry is a picture of a thing, not a face: no person silhouette.
    assert all(p.endswith(placeholders.LOGO_MARK) or "-" in p for p in photos[:2])
    assert placeholders.strip_placeholders(served, seed) == seed


def test_a_picture_is_recognised_by_its_value_and_not_only_by_its_field_name():
    """The sixth instance of one class tonight, and the vocabulary was the problem every time.
    Measured over every seeded state on disk: 232 of 441 off-host image URLs sat under names this
    module did not have -- src (147), imageSrc (44), thumbnailUrl (21), mediaUrl (18), url (2),
    across 5 app states in 5 companies -- and each is a broken box in a guest with restrict=on.
    Listing those five would have been the third round of the same gap, so the value decides."""
    from company_envs.world.placeholder_images import ROUTE, fill_image_fields

    state = {
        "products": [
            {
                "title": "Quarry air shop hose 25ft",
                "src": "https://canalridgeindustrial.com/media/products/hose.jpg",
                "imageSrc": "https://footwear.prism-lane.example/assets/nova-black.jpg",
                "thumbnailUrl": "https://assets.lakebridgeaudience.com/riverlight/panorama.jpg",
                "mediaUrl": "https://cedarlineapparel.com/media/ads/range-photo.jpg",
                "sku": "CAP-BOS-3300",
                "detailUrl": "/products/hose",
                "spec": "https://canalridgeindustrial.com/specs/hose.pdf",
            }
        ]
    }
    filled = fill_image_fields(state)["products"][0]
    for field in ("src", "imageSrc", "thumbnailUrl", "mediaUrl"):
        assert filled[field].startswith(ROUTE), field
    # Values the browser can reach, and URLs that are not pictures, are left exactly as they were.
    assert filled["detailUrl"] == "/products/hose"
    assert filled["spec"] == state["products"][0]["spec"]
    assert filled["sku"] == "CAP-BOS-3300"


def test_an_empty_string_is_still_decided_by_its_field_name():
    """A value cannot say whether it is a picture when there is no value. The name list is kept
    for exactly that, so an app rendering <img src=""> at runtime still gets something to draw."""
    from company_envs.world.placeholder_images import ROUTE, fill_image_fields

    filled = fill_image_fields({"name": "Ada Iyer", "avatar": "", "nickname": "", "note": ""})
    assert filled["avatar"].startswith(ROUTE)
    assert filled["nickname"] == "" and filled["note"] == ""


def test_a_picture_found_by_its_value_is_a_tile_and_not_somebody_s_face():
    """A silhouette on a product row is the wrong picture, which is the thing this module exists
    to avoid. A named avatar earns a face; a URL under a name nobody vouched for earns a tile."""
    from company_envs.world.placeholder_images import LOGO_MARK, ROUTE, fill_image_fields

    filled = fill_image_fields(
        {"avatar": "https://gravatar.com/x.jpg", "src": "https://example.test/product.jpg"}
    )
    assert not filled["avatar"][len(ROUTE) :].endswith(LOGO_MARK)
    assert filled["src"][len(ROUTE) :].endswith(LOGO_MARK)


def test_the_world_keeps_its_own_values_however_they_were_found():
    """fill and strip have to stay inverses, or the first save writes /__image/ into the shared
    records. Checked over all 406 seeded states on disk: every one round-trips identically."""
    from company_envs.world.placeholder_images import fill_image_fields, strip_placeholders

    state = {
        "rows": [{"src": "https://example.test/a.jpg", "images": ["https://example.test/b.png"]}],
        "user": {"name": "Ada Iyer", "avatar": ""},
    }
    assert strip_placeholders(fill_image_fields(state), state) == state
