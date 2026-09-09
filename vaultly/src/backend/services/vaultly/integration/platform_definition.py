from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformDefinition:
    id: str
    name: str
    home_url: str
    match_hosts: tuple[str, ...]
    media_hosts: tuple[str, ...]


PLATFORMS: dict[str, PlatformDefinition] = {
    "instagram": PlatformDefinition(
        id="instagram",
        name="Instagram",
        home_url="https://www.instagram.com/",
        match_hosts=("instagram.com",),
        media_hosts=("cdninstagram.com", "fbcdn.net"),
    ),
    "x": PlatformDefinition(
        id="x",
        name="X",
        home_url="https://x.com/home",
        match_hosts=("x.com", "twitter.com"),
        media_hosts=("twimg.com",),
    ),
}
