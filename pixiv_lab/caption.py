from __future__ import annotations

"""One caption builder for every delivery format.

    <b>title</b>
    <i>teaser / hook</i>

    <blockquote>bot link line(s)</blockquote>   (zip / bot / hd / cover)

    #hashtags

    footer

Formats: zip | album | bot | hd | cover
  bot   : SFW post with the rest in the bot, or NSFW with its own cover / text only
  cover : NSFW bundle whose channel image is an SFW album of ANOTHER post. Title and hashtags
          come from the SFW post, the hook sentence depends on how the two posts relate
          (same character / same franchise / unrelated), so it never lies.
"""

import html
import logging
import re

from pixiv_lab.caption_nsfw import nsfw_parts, set_nouns as set_nsfw_nouns
from pixiv_lab.caption_sfw import _pick_fresh, sfw_parts, set_nouns as set_sfw_nouns
from pixiv_lab.names import relation
from pixiv_lab.rules import get_rule
from pixiv_lab.tags import FORMAT_TAGS, get_brand_tag

log = logging.getLogger(__name__)

FORMATS = ("zip", "album", "bot", "hd", "ugoira")
CAPTION_LIMIT = 1024

_DYNAMIC_FOOTER = ""
_STRIP: list[str] = []
_TPL: dict = {}
_EXTRAS: dict = {}
_JOINER = "\n\n"


def set_footer(footer: str) -> None:
    """Set dynamic footer received from batch/bot."""
    global _DYNAMIC_FOOTER
    _DYNAMIC_FOOTER = (footer or "").strip()


def set_strip(patterns) -> None:
    global _STRIP
    _STRIP = [str(p) for p in (patterns or []) if p]


def set_templates(tpl: dict | None) -> None:
    global _TPL
    _TPL = dict(tpl or {})


def set_extras(extras: dict | None) -> None:
    global _EXTRAS
    _EXTRAS = dict(extras or {})


def set_joiner(joiner: str | None) -> None:
    global _JOINER
    _JOINER = "\n" if joiner == "nl" else "\n\n"


def set_caption_cfg(cfg: dict | None) -> None:
    cfg = cfg or {}
    set_layout(cfg.get("layout"))
    set_strip(cfg.get("strip"))
    set_templates(cfg.get("templates"))
    set_extras(cfg.get("extras"))
    set_joiner(cfg.get("joiner"))


def apply_strip(text: str) -> str:
    out = text or ""
    for pat in _STRIP:
        try:
            out = re.sub(pat, "", out, flags=re.I | re.M)
        except re.error:
            out = out.replace(pat, "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# CTA words belong to the channel (batch). Machine only has structural fallbacks.
_CTA_STRUCT = {
    "zip_link": "ZIP",
    "zip_plain": "ZIP",
    "hd_link": "HD",
    "hd_plain": "HD",
    "gallery_word": "{count}",
    "gallery_word_more": "{count}",
    "gallery_one": "open",
    "gallery_part": "{i}/{n}",
    "sep": " · ",
    "ugoira": "",
}
_COVER_HOOK_KEYS = ("char", "franchise", "none")
_COVER_BTN_KEYS = ("btn_char", "btn_franchise", "btn_none")
_CTA = dict(_CTA_STRUCT)
_HOOK_LISTS: dict[str, list[str]] = {k: [] for k in _COVER_HOOK_KEYS}
_BTN_LISTS: dict[str, list[str]] = {k: [] for k in _COVER_BTN_KEYS}


def _lines(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value or "")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def set_cta(cta: dict | None) -> None:
    """Replace this run's CTA from the batch. Never keeps another channel's voice."""
    global _CTA, _HOOK_LISTS, _BTN_LISTS
    _CTA = dict(_CTA_STRUCT)
    _HOOK_LISTS = {k: [] for k in _COVER_HOOK_KEYS}
    _BTN_LISTS = {k: [] for k in _COVER_BTN_KEYS}
    noun = None
    noun_plural = None
    for key, value in (cta or {}).items():
        if key == "noun" and isinstance(value, str) and value.strip():
            noun = value.strip()
            continue
        if key == "noun_plural" and isinstance(value, str) and value.strip():
            noun_plural = value.strip()
            continue
        if key in _CTA and isinstance(value, str):
            _CTA[key] = value
        elif key in _COVER_HOOK_KEYS and isinstance(value, (str, list, tuple)):
            _HOOK_LISTS[key] = _lines(value)
        elif key in _COVER_BTN_KEYS and isinstance(value, (str, list, tuple)):
            _BTN_LISTS[key] = _lines(value)
    set_sfw_nouns(noun, noun_plural)
    set_nsfw_nouns(noun, noun_plural)


# ---- caption layout (the arrangement comes from the bot, not the machine) ----
# A caption is an ordered list of blocks. The BOT picks the order — including
# odd layouts like "hashtags before the title" — by sending a layout in the batch.
# The machine only renders the blocks it is asked to. Default = current order
# (title, teaser, CTA, hashtags, footer) so an un-configured machine still works.
TITLE = "title"
TEASER = "teaser"
CTA = "cta"
TAGS = "hashtags"
FOOTER = "footer"
PREFIX = "prefix"
EXTRA = "extra"
VALID_LAYOUT = {TITLE, TEASER, CTA, TAGS, FOOTER, PREFIX, EXTRA}
DEFAULT_LAYOUT = (TITLE, TEASER, CTA, TAGS, FOOTER)

_LAYOUT = DEFAULT_LAYOUT


def set_layout(layout) -> None:
    """Set the caption block order from the batch (string or list of ids).

    Example: "title hashtags teaser cta footer" — hashtags between title and teaser.
    Unknown ids are dropped; an empty/invalid layout keeps whatever was last set
    (which is the default in a fresh run).
    """
    global _LAYOUT
    if isinstance(layout, str):
        layout = [w for w in layout.replace(",", " ").split() if w]
    if isinstance(layout, (list, tuple)) and layout:
        cleaned = [b for b in layout if b in VALID_LAYOUT]
        if cleaned:
            _LAYOUT = tuple(cleaned)


def _block_text(b: str, parts: dict, cta: str, tags: list[str], footer: str) -> str:
    if b == TITLE:
        val = apply_strip(parts.get("title") or "")
        if not val:
            return ""
        tpl = _TPL.get("title") or "<b>{title}</b>"
        if "{title}" in tpl or "{value}" in tpl:
            return tpl.replace("{title}", _esc(val)).replace("{value}", _esc(val))
        return f"<b>{_esc(val)}</b>"
    if b == TEASER:
        val = apply_strip(parts.get("teaser") or "")
        if not val:
            return ""
        tpl = _TPL.get("teaser") or "<i>{teaser}</i>"
        if "{teaser}" in tpl or "{value}" in tpl:
            return tpl.replace("{teaser}", _esc(val)).replace("{value}", _esc(val))
        return f"<i>{_esc(val)}</i>"
    if b == CTA:
        return f"<blockquote>{cta}</blockquote>" if cta else ""
    if b == TAGS:
        joined = " ".join(tags) if tags else ""
        if not joined:
            return ""
        tpl = _TPL.get("hashtags") or "{hashtags}"
        if "{hashtags}" in tpl:
            return tpl.replace("{hashtags}", joined)
        return joined
    if b == FOOTER:
        return footer or ""
    if b == PREFIX:
        return str(_EXTRAS.get("prefix") or "")
    if b == EXTRA:
        return str(_EXTRAS.get("extra") or "")
    return ""


TAG_RE = re.compile(r"<[^>]+>")


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def _visible_len(text: str) -> int:
    return len(html.unescape(TAG_RE.sub("", text)))


def _link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def caption_parts(items: list[dict], nsfw: bool, runtime: dict, info: dict | None = None) -> dict:
    """Pick title + teaser once per post."""
    if nsfw:
        title, teaser = nsfw_parts(items, info)
    else:
        title, teaser = sfw_parts(items, runtime, info)
    return {"title": title, "teaser": teaser}


def _gallery_cta(count: int, links: list[str], more: bool) -> str:
    n = len(links)
    tpl = _CTA["gallery_word_more"] if more else _CTA["gallery_word"]
    word = tpl.format(count=count)
    sep = _CTA["sep"]
    if n > 1:
        part_tpl = _CTA["gallery_part"].format(i="{i}", n=n)  # "Part {i} of {n}"
        return "\n\n".join(
            _link(link, f"📖 {word}{sep}{_CTA['gallery_one']}, {part_tpl}".replace("{i}", str(i)))
            for i, link in enumerate(links, start=1)
        )
    if n == 1:
        return _link(links[0], f"📖 {word}{sep}{_CTA['gallery_one']}")
    return f"📖 {word}"


def _cta(fmt: str, count: int, page_url, more: bool = False) -> str:
    if fmt == "zip":
        if isinstance(page_url, str) and page_url:
            return _link(page_url, _CTA["zip_link"])
        return _CTA["zip_plain"]
    if fmt == "hd":
        if isinstance(page_url, str) and page_url:
            return _link(page_url, _CTA["hd_link"])
        return _CTA["hd_plain"]
    if fmt == "bot":
        links = [page_url] if isinstance(page_url, str) else list(page_url or [])
        return _gallery_cta(count, [x for x in links if x], more)
    if fmt == "ugoira":
        # the animated video itself is the post; the caption just labels it.
        return f"🎞 {_CTA['ugoira']}" if _CTA["ugoira"] else "🎞"
    return ""


def _footer(rules: dict | None) -> str:
    return "" if get_rule(rules or {}, "caption", "footer", default=True) is False else _DYNAMIC_FOOTER


def _assemble(parts: dict, cta: str, tags: list[str], footer: str) -> str:
    """Render the caption blocks in the order the bot chose (_LAYOUT).

    Title and teaser stay tight (one header) when adjacent in the layout; every
    other block is separated by a blank line. So "hashtag before title" works:
      #tag1 #tag2
      <b>Title</b>
      <i>Teaser</i>
    """
    out: list[str] = []
    for i, block in enumerate(_LAYOUT):
        text = _block_text(block, parts, cta, tags, footer)
        if not text:
            continue
        tight = block == TEASER and i > 0 and _LAYOUT[i - 1] == TITLE
        gap = "\n" if tight else _JOINER
        out.append((gap + text) if out else text)
    return "".join(out)


def _fit(parts: dict, cta: str, tags: list[str], footer: str, info: dict | None) -> str:
    """Trim to 1024 visible chars: least important hashtags first, then the teaser."""
    protected = {get_brand_tag().lower(), *(h.lower() for h in FORMAT_TAGS.values())}
    protected.discard("")
    if info:
        protected |= {h.lower() for h in info.get("character_hashtags") or []}
        if info.get("franchise_hashtag"):
            protected.add(info["franchise_hashtag"].lower())
    text = _assemble(parts, cta, tags, footer)
    while _visible_len(text) > CAPTION_LIMIT:
        idx = next((i for i in range(len(tags) - 1, -1, -1) if tags[i].lower() not in protected), None)
        if idx is None:
            break
        log.warning("Caption too long (%s), dropping hashtag %s", _visible_len(text), tags[idx])
        tags.pop(idx)
        text = _assemble(parts, cta, tags, footer)
    if _visible_len(text) > CAPTION_LIMIT and parts.get("teaser"):
        log.warning("Caption still too long (%s), dropping teaser", _visible_len(text))
        text = _assemble({**parts, "teaser": ""}, cta, tags, footer)
    if _visible_len(text) > CAPTION_LIMIT:
        log.warning("Caption is still %s chars after trimming", _visible_len(text))
    return text


def _with_format_tag(hashtags, fmt: str, rules) -> list[str]:
    tags = [t for t in (hashtags or []) if t]
    ft = FORMAT_TAGS.get(fmt)
    if ft and get_rule(rules or {}, "hashtags", "format_tag", default=True) is not False:
        if ft.lower() not in {t.lower() for t in tags}:
            tags.insert(0, ft)
    return tags


def build_caption(
    parts: dict,
    fmt: str,
    count: int,
    hashtags: list[str] | None,
    rules: dict | None = None,
    info: dict | None = None,
    page_url=None,
    more: bool = False,
) -> str:
    """count: pages shown in the link line (for 'bot' = pages that are inside the bot).
    more: True for SFW posts ('N more pics'), False for NSFW ('N pics')."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown caption format {fmt!r}")
    return _fit(parts, _cta(fmt, count, page_url, more), _with_format_tag(hashtags, fmt, rules), _footer(rules), info)


def build_cover_caption(
    cover_parts: dict, cover_info: dict, nsfw_info: dict, links, rules: dict | None, runtime: dict
) -> str:
    """Channel image = SFW album of another post, bot link = the NSFW bundle.
    Title + hashtags belong to the SFW post (they match the image); the hook and the button
    text follow the relation of the two posts, so they never claim something untrue."""
    rel, name = relation(cover_info, nsfw_info)
    mem = runtime.setdefault("recentCaptions", {})
    hooks = _HOOK_LISTS.get(rel) or []
    btns = _BTN_LISTS.get(f"btn_{rel}") or []
    hook_pool = [h.format(n=name, f=name) for h in hooks]
    btn_pool = [b.format(n=name, f=name) for b in btns] or ["open"]
    hook = _pick_fresh(hook_pool, f"h:{rel}", mem) if hook_pool else ""
    btn = _pick_fresh(btn_pool, f"b:{rel}", mem) if btn_pool else "open"

    links = [x for x in ([links] if isinstance(links, str) else list(links or [])) if x]
    if not links:
        cta = btn
    elif len(links) == 1:
        cta = _link(links[0], btn)
    else:
        cta = "\n\n".join(_link(link, f"{btn} · Part {i}") for i, link in enumerate(links, start=1))

    parts = {"title": cover_parts.get("title"), "teaser": hook}
    tags = _with_format_tag(cover_info.get("hashtags"), "bot", rules)
    log.info("Cover caption: relation=%s name=%r", rel, name)
    return _fit(parts, cta, tags, _footer(rules), cover_info)