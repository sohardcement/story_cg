"""Resolve a character tag and source identity descriptors from Danbooru."""

import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from storycg.state import save_json_atomic


BASE_URL = "https://danbooru.donmai.us"
USER_AGENT = "storycg/0.1 (local story-CG identity resolver)"
WIKI_LINK = re.compile(r"\[\[([^]|]+)(?:\|[^]]*)?\]\]")
APPEARANCE = re.compile(
    r"(?:^|_)(?:hair|eyes?|ears?|skin|horns?|tails?|elf|petite|bangs|"
    r"twintails?|ponytails?|braids?|ahoge|fangs?|freckles|eyebrows?|"
    r"eyelashes|mole|glasses)(?:_|$)"
)
APPAREL = re.compile(
    r"(?:^|_)(?:shirt|jacket|skirt|belt|capelet|cape|collar|pantyhose|"
    r"boots?|shoes?|earrings?|dress|coat|cloak|robe|uniform|socks?|"
    r"gloves?|hat|scarf|ribbon|gem|trim)(?:_|$)"
)


def _tag(text: str) -> str:
    return re.sub(r"\s+", "_", text.strip().lower())


class _CachedDanbooru:
    def __init__(self, cache_dir: Path, client: httpx.Client):
        self.cache_dir = cache_dir
        self.client = client
        self.last_request = 0.0

    def get(self, path: str, params: dict | None = None) -> tuple[object, str]:
        url = str(self.client.build_request("GET", path, params=params).url)
        cache = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))["response"], url
        elapsed = time.monotonic() - self.last_request
        if self.last_request and elapsed < 0.8:
            time.sleep(0.8 - elapsed)
        response = self.client.get(path, params=params, headers={"User-Agent": USER_AGENT})
        self.last_request = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        save_json_atomic(cache, {"url": url, "response": payload})
        return payload, url


def _appearance_from_wiki(body: str, wiki_url: str) -> list[dict]:
    found = []
    seen = set()
    for paragraph in re.split(r"\r?\n\s*\r?\n", body):
        if not re.search(r"\b(?:has|is a|is an|with|features)\b", paragraph, re.I):
            continue
        for match in WIKI_LINK.finditer(paragraph):
            tag = _tag(match.group(1))
            if tag not in seen and APPEARANCE.search(tag):
                found.append({"tag": tag, "source": {"url": wiki_url,
                              "method": "linked tag in character wiki appearance prose",
                              "evidence": match.group(0)}})
                seen.add(tag)
    return found


def _outfit_from_wiki(body: str, wiki_url: str) -> list[dict]:
    found = []
    seen = set()
    for paragraph in re.split(r"\r?\n\s*\r?\n", body):
        if not re.search(r"\b(?:wears?|outfit|clothing|dressed)\b", paragraph, re.I):
            continue
        for match in WIKI_LINK.finditer(paragraph):
            tag = _tag(match.group(1))
            if tag not in seen and APPAREL.search(tag):
                found.append({"tag": tag, "source": {"url": wiki_url,
                              "method": "linked tag in character wiki clothing prose",
                              "evidence": match.group(0)}})
                seen.add(tag)
    return found


def resolve_identity(name: str, cache_dir: str | Path, *, client: httpx.Client | None = None) -> dict:
    """Resolve exact tag/alias; leave multiple non-exact characters for human disambiguation."""
    if not name.strip():
        raise ValueError("character name is empty")
    if client is None:
        with httpx.Client(base_url=BASE_URL, timeout=30, trust_env=False,
                          headers={"User-Agent": USER_AGENT}) as owned:
            return resolve_identity(name, cache_dir, client=owned)
    source = _CachedDanbooru(Path(cache_dir), client)
    entries, search_url = source.get("/autocomplete.json", {
        "search[query]": name, "search[type]": "tag_query", "limit": 10,
    })
    candidates = [{"tag": item["value"], "post_count": item.get("post_count", 0),
                   "antecedent": item.get("antecedent"),
                   "source": {"url": search_url, "method": "autocomplete character category"}}
                  for item in entries if item.get("category") == 4 and not
                  item.get("tag", {}).get("is_deprecated", False)]
    exact = [item for item in candidates if _tag(item["tag"]) == _tag(name)
             or item.get("antecedent") == name]
    choices = exact if exact else candidates
    if len(choices) != 1:
        return {"input_name": name, "status": "needs_clarification",
                "reason": "no character candidate" if not choices else "ambiguous character candidates",
                "candidates": choices, "identity_tier": "pending_P6"}
    chosen = choices[0]
    character = chosen["tag"]
    try:
        wiki, wiki_url = source.get(f"/wiki_pages/{character}.json")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        wiki, wiki_url = {}, f"{BASE_URL}/wiki_pages/{character}.json"
    body = wiki.get("body", "")
    work_match = re.search(r"\b(?:of|from)\s+\[\[([^]|]+)", body, re.I)
    if work_match:
        work_tag = _tag(work_match.group(1))
        work_source = {"url": wiki_url, "method": "work link in character wiki",
                       "evidence": work_match.group(0)}
    else:
        parenthetical = re.search(r"\(([^)]+)\)$", character)
        work_tag = _tag(parenthetical.group(1)) if parenthetical else None
        work_source = ({"url": search_url, "method": "character tag parenthetical"}
                       if work_tag else None)
    appearance = _appearance_from_wiki(body, wiki_url)
    outfit = _outfit_from_wiki(body, wiki_url)
    block = ", ".join(x for x in ["1girl", character, work_tag,
                                  *(item["tag"] for item in appearance)] if x)
    return {"input_name": name, "status": "locked", "character_tag": character,
            "character_source": chosen["source"], "work_tag": work_tag,
            "work_source": work_source, "appearance_tags": appearance,
            "default_outfit_tags": outfit,
            "identity_block": block, "identity_tier": "pending_P6",
            "lora": None, "reference_image": None, "candidates": candidates}
