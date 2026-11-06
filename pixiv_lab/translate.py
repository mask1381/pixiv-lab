"""English text for Pixiv title, caption, and tags.

Prefers Pixiv's own translations (app-api title_caption_translation +
translated_name). If Japanese is still left, fills from the public ajax
endpoint with lang=en. Every consumer should read through this module so
channel captions, zip names, and personal dumps stay on the same English.
"""
from __future__ import annotations

import html as htmlmod
import logging
import re

from pixiv_lab.config import PIXIV_USER_AGENT, REQUEST_TIMEOUT

log = logging.getLogger(__name__)

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
SKIP_TAGS = {"r-18", "r-18g", "r18", "original", "オリジナル"}


def is_cjk(s: str) -> bool:
    return bool(CJK_RE.search(str(s or "")))


def plain_caption(raw) -> str:
    text = str(raw or "")
    text = text.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    return htmlmod.unescape(text).strip()


def pick_en(en, jp) -> str:
    en = str(en or "").strip()
    jp = str(jp or "").strip()
    if en:
        return en
    return jp


def _tr_blob(illust: dict) -> dict:
    tr = illust.get("title_caption_translation")
    if not isinstance(tr, dict):
        tr = illust.get("titleCaptionTranslation") or {}
    return tr if isinstance(tr, dict) else {}


def _tag_en(tag: dict) -> str:
    en = str(tag.get("translated_name") or "").strip()
    trans = tag.get("translation")
    if isinstance(trans, dict):
        en = en or str(trans.get("en") or "").strip()
    return en


def from_illust(illust: dict) -> dict:
    """English-first title/caption/tags from one app-api illust object."""
    tr = _tr_blob(illust)
    title_jp = str(illust.get("title") or "").strip()
    cap_jp = plain_caption(illust.get("caption") or "")
    title_en = str(tr.get("work_title") or tr.get("workTitle") or "").strip()
    cap_en = plain_caption(tr.get("work_caption") or tr.get("workCaption") or "")
    tags = []
    seen: set[str] = set()
    for t in illust.get("tags") or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or t.get("tag") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        tags.append({"name": name, "en": _tag_en(t)})
    return {
        "title": pick_en(title_en, title_jp),
        "caption": pick_en(cap_en, cap_jp),
        "title_jp": title_jp,
        "caption_jp": cap_jp,
        "tags": tags,
    }


def needs_ajax(loc: dict) -> bool:
    if is_cjk(loc.get("title") or "") or is_cjk(loc.get("caption") or ""):
        return True
    return any(is_cjk(t["name"]) and not t["en"] for t in loc.get("tags") or [])


def _apply_ajax(loc: dict, body: dict) -> dict:
    tr = body.get("titleCaptionTranslation") or {}
    if not isinstance(tr, dict):
        tr = {}
    title_en = str(tr.get("workTitle") or tr.get("work_title") or "").strip()
    cap_en = plain_caption(tr.get("workCaption") or tr.get("work_caption") or "")
    if title_en:
        loc["title"] = title_en
    if cap_en:
        loc["caption"] = cap_en
    by_name = {t["name"]: t for t in loc["tags"]}
    raw_tags = body.get("tags") or {}
    for t in (raw_tags.get("tags") if isinstance(raw_tags, dict) else raw_tags) or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("tag") or t.get("name") or "").strip()
        if not name:
            continue
        trans = t.get("translation") if isinstance(t.get("translation"), dict) else {}
        en = str(trans.get("en") or t.get("translated_name") or "").strip()
        if name in by_name:
            if en and not by_name[name]["en"]:
                by_name[name]["en"] = en
        else:
            loc["tags"].append({"name": name, "en": en})
            by_name[name] = loc["tags"][-1]
    return loc


def fill_from_ajax(client, illust_id, loc: dict) -> dict:
    """Public lang=en ajax. Best-effort; never raises into the send path."""
    if not illust_id or not needs_ajax(loc):
        return loc
    try:
        r = client.get(
            f"https://www.pixiv.net/ajax/illust/{illust_id}",
            params={"lang": "en"},
            headers={
                "User-Agent": PIXIV_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.pixiv.net/",
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        body = (r.json() or {}).get("body") or {}
        if not isinstance(body, dict):
            return loc
        return _apply_ajax(loc, body)
    except Exception as exc:
        log.info("ajax en fill skipped for %s: %s", illust_id, exc)
        return loc


def enrich_illust(client, illust: dict) -> dict:
    """Mutate the illust so title, caption, and tag translated_name are English-first."""
    loc = from_illust(illust)
    loc = fill_from_ajax(client, illust.get("id"), loc)
    illust["title"] = loc["title"] or illust.get("title")
    if loc["caption"]:
        # keep HTML-ish caption as plain English for every later consumer
        illust["caption"] = loc["caption"]
    tr = dict(_tr_blob(illust))
    tr["work_title"] = loc["title"]
    tr["work_caption"] = loc["caption"]
    illust["title_caption_translation"] = tr
    by_en = {t["name"]: t["en"] for t in loc["tags"]}
    tags = illust.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").strip()
            en = by_en.get(name) or _tag_en(t)
            if en:
                t["translated_name"] = en
    illust["_locale"] = loc
    return illust


def tag_pairs(illust: dict) -> list[tuple[str, str]]:
    loc = illust.get("_locale") or from_illust(illust)
    return [(t["name"], t["en"]) for t in loc["tags"]]


def display_tags(illust: dict) -> list[str]:
    out = []
    seen: set[str] = set()
    for name, en in tag_pairs(illust):
        label = en or (name if not is_cjk(name) else "")
        key = label.lower().replace(" ", "_")
        if not label or key in SKIP_TAGS or key in seen:
            continue
        seen.add(key)
        out.append("#" + re.sub(r"\s+", "_", label))
    return out


def raw_caption(illust: dict) -> str:
    """Personal dump: translated title + caption + English tags + source URL."""
    loc = illust.get("_locale") or from_illust(illust)
    iid = illust.get("id")
    url = f"https://www.pixiv.net/artworks/{iid}" if iid else ""
    text = "\n".join(p for p in (loc["title"], loc["caption"], " ".join(display_tags(illust)), url) if p)
    return text[:1024]
