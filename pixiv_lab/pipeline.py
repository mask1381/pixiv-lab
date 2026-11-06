from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pixiv_lab.blocklist import blocked_reason
from pixiv_lab.caption import build_caption, build_cover_caption, caption_parts
from pixiv_lab.delivery import caption_format, channel_split, decide
from pixiv_lab.enrich import enrich_posts
from pixiv_lab.images import has_valid_urls
from pixiv_lab.pixiv import adapt_detail_pages, fetch_illust_detail
from pixiv_lab.rules import get_rule
from pixiv_lab.state import save_posted, save_runtime
from pixiv_lab.tags import classify_tags, record_tag_stats
from pixiv_lab.title import clean_title
from pixiv_lab.userbot_zip import zip_name_base

log = logging.getLogger("pixiv_lab")


ENRICH_KEYS = (
    "character",
    "character_tags",
    "franchise_tags",
    "studio_tags",
    "source_tags",
    "general_tags",
    "generalTags",
    "inferred_franchise",
    "inferred_studio",
    "franchiseList",
    "studioList",
    "characterList",
    "all_tags",
)

MAX_FAILURES = 3

# Placeholder link used only in dry runs (no real posting). The channel supplies it
# via the batch so the machine never hardcodes a channel/bot identity.
_DRY_BOT_LINK = "https://t.me/example?start=dry"


def set_dry_bot_link(url: str) -> None:
    global _DRY_BOT_LINK
    if str(url or "").strip():
        _DRY_BOT_LINK = str(url).strip()


class ConfigError(RuntimeError):
    """Missing secrets / setup problem. Never counts toward MAX_FAILURES."""


def _enrich_album(pages: list[dict], dict_rows: list[dict]) -> list[dict]:
    if not pages:
        return []
    head = enrich_posts([pages[0]], dict_rows)[0]
    out = []
    for page in pages:
        item = {**page}
        for key in ENRICH_KEYS:
            item[key] = head[key]
        out.append(item)
    return out


def _mark_skipped(posted: dict, runtime: dict, item_id: str, reason: str) -> None:
    posted[item_id] = f"skipped:{reason}" if reason else "skipped"
    runtime.get("failures", {}).pop(item_id, None)
    log.warning("Marked %s as skipped: %s", item_id, reason)


def _failure_count(raw) -> int:
    """Accept legacy dict shape {n, reason, last} or a plain int."""
    if isinstance(raw, dict):
        try:
            return int(raw.get("n") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _record_failure(posted: dict, runtime: dict, item_id: str) -> None:
    failures = runtime.setdefault("failures", {})
    n = _failure_count(failures.get(item_id)) + 1
    failures[item_id] = n
    log.warning("Failure %s/%s for %s", n, MAX_FAILURES, item_id)
    if n >= MAX_FAILURES:
        _mark_skipped(posted, runtime, item_id, f"failed {MAX_FAILURES} times")


def _mark_done(posted: dict, runtime: dict, item_id: str, value: str = "") -> None:
    posted[item_id] = value
    runtime.get("failures", {}).pop(item_id, None)


def _hashtag_opts(rules: dict, nsfw: bool) -> dict:
    """rules['hashtags'] -> classify_tags options (per artist, per path)."""
    h = get_rule(rules, "hashtags", default={}) or {}
    pt = h.get("passthrough") or {}
    keys = ("max_category", "fallback", "max_len", "pass_max_len", "pass_max_words")
    opts = {k: h[k] for k in keys if k in h}
    opts["passthrough"] = bool(pt.get("nsfw" if nsfw else "sfw", True))
    limit = pt.get("max_nsfw") if nsfw and "max_nsfw" in pt else pt.get("max")
    if limit is not None:
        opts["passthrough_max"] = limit
    return opts


def _classify(enriched, dict_rows, content_map, adult_set, drop_set, rules, nsfw):
    """Decide what every Pixiv tag becomes; returns the full classify result."""
    return classify_tags(enriched, dict_rows, content_map, adult_set, drop_set, _hashtag_opts(rules, nsfw))


def _apply_page_rules(pages: list[dict], rules: dict) -> list[dict]:
    """pages.drop_last removes the last page of the post (ads). Single-page posts are untouched."""
    if get_rule(rules, "pages", "drop_last", default=False) and len(pages) > 1:
        return pages[:-1]
    return pages


def _unify_tags(pages: list[dict]) -> None:
    """Merged post: every page carries the union of tags of all source illusts; titles are dropped."""
    tags: list[str] = []
    translations: dict[str, str] = {}
    pixiv_tags: list[dict] = []
    seen: set[str] = set()
    for p in pages:
        for t in p.get("generalTags") or []:
            if t not in tags:
                tags.append(t)
        translations.update(p.get("tagTranslations") or {})
        for pt in p.get("pixivTags") or []:
            if pt["name"] not in seen:
                seen.add(pt["name"])
                pixiv_tags.append(pt)
    raw = list(dict.fromkeys([*tags, *translations.values()]))
    for p in pages:
        p["generalTags"], p["tagsRaw"] = tags, raw
        p["tagTranslations"], p["pixivTags"] = translations, pixiv_tags
        p["title"] = None


# ---- one prepared post (shared by the immediate "post" mode and the nightly "plan" mode) ----

@dataclass
class PreparedPost:
    chosen: dict
    nsfw: bool
    rules: dict
    valid: list
    enriched: list
    classified: dict
    method: str          # zip | post
    parts: dict
    dry: bool = False
    runtime: dict = field(default_factory=dict)
    # ugoira (animated) posts: the raw pixiv detail is carried so deliver_post can
    # convert the frame zip to a video. is_ugoira bypasses the static-URL gate.
    is_ugoira: bool = False
    token: str | None = None

    @property
    def item_id(self) -> str:
        return self.chosen["id"]

    @property
    def zip_name(self) -> str:
        art_title = next((it.get("art_title") for it in self.enriched if it.get("art_title")), None)
        return zip_name_base(self.classified, art_title)

    @property
    def fmt(self) -> str:
        return caption_format(self.method, self.nsfw, len(self.enriched), self.rules)

    def make_caption(self, m: str | None = None, page_url=None, cover=None) -> str:
        """m: zip | album | bot | hd | cover. 'cover' needs the prepared SFW cover post."""
        m = m or self.fmt
        if m == "cover" and cover is None:
            m = "bot"
        if page_url is None and self.dry:
            if m in ("bot", "cover"):
                page_url = [f"{_DRY_BOT_LINK}1"]
            elif m in ("zip", "hd"):
                page_url = _DRY_BOT_LINK
        if m == "cover":
            return build_cover_caption(cover.parts, cover.classified, self.classified, page_url, self.rules, self.runtime)
        n = len(self.enriched)
        count = (n if self.nsfw else channel_split(n)[1]) if m == "bot" else n
        return build_caption(
            self.parts, m, count, self.classified["hashtags"], self.rules, self.classified, page_url,
            more=(m == "bot" and not self.nsfw),
        )


def prepare_post(
    client, token, chosen, dict_rows, content_map, adult_set, drop_set, posted, runtime, rules, nsfw: bool, dry: bool = False,
    record: bool = True,
) -> PreparedPost | None:
    kind = "NSFW" if nsfw else "SFW"
    item_id = chosen["id"]
    merged = bool(chosen.get("_merge"))
    ids = [c["id"] for c in chosen["_merge"]] if merged else [item_id]

    if merged:
        pages = []
        for comp in chosen["_merge"]:
            detail = fetch_illust_detail(client, token, comp["id01"])
            pages += _apply_page_rules(adapt_detail_pages(detail), rules)
        _unify_tags(pages)
        detail = None
    else:
        detail = fetch_illust_detail(client, token, chosen["id01"])
        pages = _apply_page_rules(adapt_detail_pages(detail), rules)

    # ugoira = animated ilust: there is no single static original to post; the animation
    # is delivered as a video (convert_ugoira_to_video). The poster frame is kept so the
    # caption/classification machinery still has something, but the static-URL gate below
    # is bypassed so the animation is never silently dropped as "no valid image URLs".
    if not merged:
        is_ugoira = bool((detail or {}).get("type") == "ugoira" or chosen.get("category") == "ugoira")
    else:
        is_ugoira = False

    persist = record and not dry
    if is_ugoira and get_rule(rules, "ugoira", default="video") == "skip":
        log.info("%s: ugoira %s skipped by artist rule", kind, item_id)
        if persist:
            for mid in ids:
                _mark_skipped(posted, runtime, mid, "ugoira skip")
        return None

    hit = next((h for h in (blocked_reason(p) for p in pages) if h), None)
    if hit:
        log.warning("%s: %s skipped, blocked content (%s)", kind, item_id, hit)
        if persist:
            for mid in ids:
                _mark_skipped(posted, runtime, mid, f"blocked content ({hit})")
        return None

    # ugoira: the poster frame may not carry a valid static original, and that is fine —
    # the video is the actual post. Bypass the silent "no valid image URLs" drop.
    if is_ugoira:
        valid = pages or []
        if not valid:
            log.warning("%s: ugoira %s has no frame to attach poster to; skipping", kind, item_id)
            return None
    else:
        valid = [p for p in pages if has_valid_urls(p)]
        if not valid:
            log.warning("%s: no valid image URLs for %s", kind, item_id)
            if persist:
                for mid in ids:
                    _mark_skipped(posted, runtime, mid, "no valid image URLs")
            return None

    enriched = _enrich_album(valid, dict_rows)
    art_title = None if merged else clean_title(valid[0].get("title"), adult_set)
    for it in enriched:
        it["art_title"] = art_title
    classified = _classify(enriched, dict_rows, content_map, adult_set, drop_set, rules, nsfw=nsfw)
    method = "ugoira" if is_ugoira else decide(len(valid), nsfw, rules)
    parts = caption_parts(enriched, nsfw, runtime, classified)
    log.info("Prepared %s %s: %s pages, method=%s", kind, item_id, len(valid), method)
    return PreparedPost(chosen, nsfw, rules, valid, enriched, classified, method, parts, dry, runtime, is_ugoira, token)


def _persist_stats(cfg, classified, nsfw: bool) -> None:
    """Count every Pixiv tag of a posted item into data/tag_stats.csv or State sink."""
    if cfg["dry_run"]:
        return
    if "state" in cfg and cfg["state"]:
        cfg["state"].add_stats(classified["decisions"], nsfw)
        return
    if "tag_stats_path" in cfg:
        record_tag_stats(cfg["tag_stats_path"], classified["decisions"], nsfw)


def _save_state(cfg, posted, runtime) -> None:
    """Persist state files or send to State sink; a dry run never writes state."""
    if cfg["dry_run"]:
        log.info("DRY_RUN: state not saved")
        return
    if "state" in cfg and cfg["state"]:
        cfg["state"].save(posted, runtime)
        return
    if posted is not None and "posted_path" in cfg:
        save_posted(cfg["posted_path"], posted)
    if "runtime_path" in cfg:
        save_runtime(cfg["runtime_path"], runtime)