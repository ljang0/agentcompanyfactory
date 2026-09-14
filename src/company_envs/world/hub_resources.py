"""Serve only manifest-bound files referenced by a worker's current app view."""

import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

from company_envs.storage import digest, read

ROUTE = "/__company_resource/"
LINK = re.compile(r'https?://[^\s<>"\'()\[\]]+|/__company_resource/[^\s<>"\'()\[\]]+')


class ResourceStore:
    def __init__(self, folder, states):
        folder = Path(folder).resolve()
        self.resources, self.aliases = {}, {}
        for name in ("ASSETS.json", "CAMPAIGNS.json", "transactions/RESOURCES.json", "files/RESOURCES.json"):
            path = folder / "world/population" / name
            if not path.exists():
                continue
            value = read(path)
            rows = value if isinstance(value, list) else value.get("resources", []) + value.get("exports", [])
            for row in rows:
                variants = [
                    (
                        row.get("url"),
                        row.get("path") or row.get("local_path"),
                        row.get("sha256"),
                        row.get("id"),
                    )
                ]
                if row.get("image_url"):
                    variants.append((row["image_url"], row["image_path"], row["image_sha256"], None))
                for url, relative, expected, rid in variants:
                    target = (folder / relative).resolve()
                    if (
                        not target.is_relative_to(folder)
                        or not target.is_file()
                        or digest(target.read_bytes()) != expected
                    ):
                        raise ValueError(f"invalid resource bytes: {relative}")

                    def register(alias, expected=expected, target=target, rid=rid, url=url):
                        route = ROUTE + expected + "/" + digest(alias)[:12] + "/" + quote(target.name)
                        self.resources[route] = {"path": target, "id": rid, "url": url, "sha256": expected}
                        self.aliases[alias] = route

                    if url:
                        register(url)
                    if rid:
                        item = states.get("google_drive_mock", {}).get("items", {}).get(rid, {})
                        binary = item.get("thumbnailUrl", "")
                        if binary.startswith("data:"):
                            register(binary)
        self.reverse = {v: k for k, v in self.aliases.items()}

    def rewrite(self, value, *, reverse=False):
        mapping = self.reverse if reverse else self.aliases
        if isinstance(value, str):
            if value in mapping:
                return mapping[value]

            def substitute(match):
                token = match[0].rstrip(".,;")
                return mapping.get(token, token) + match[0][len(token) :]

            return LINK.sub(substitute, value)
        if isinstance(value, dict):
            return {k: self.rewrite(v, reverse=reverse) for k, v in value.items()}
        if isinstance(value, list):
            return [self.rewrite(v, reverse=reverse) for v in value]
        return value

    def permitted(self, route, state):
        resource = self.resources.get(route)
        if not resource:
            return False
        rid, url = resource["id"], resource["url"]

        def contains(value):
            if isinstance(value, dict):
                return bool(rid and value.get("id") == rid) or any(contains(v) for v in value.values())
            if isinstance(value, list):
                return any(contains(v) for v in value)
            return isinstance(value, str) and bool(url and url in value)

        return contains(state)

    def response(self, route, state):
        if not self.permitted(route, state):
            return 404, b"Resource unavailable", [("Content-Type", "text/plain")]
        resource = self.resources[route]
        data = resource["path"].read_bytes()
        if digest(data) != resource["sha256"]:
            return 409, b"Resource changed", [("Content-Type", "text/plain")]
        mime = mimetypes.guess_type(resource["path"].name)[0] or "application/octet-stream"
        return (
            200,
            data,
            [
                ("Content-Type", mime),
                ("Content-Disposition", f"inline; filename*=UTF-8''{quote(resource['path'].name)}"),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
