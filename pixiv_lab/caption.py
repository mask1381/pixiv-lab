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

from pixiv_lab.caption_nsfw import nsfw_parts
from pixiv_lab.caption_sfw import _pick_fresh, sfw_parts
from pixiv_lab.names import relation
from pixiv_lab.rules import get_rule
from pixiv_lab.tags import FORMAT_TAGS, get_brand_tag

log = logging.getLogger(__name__)

FORMATS = ("zip", "album", "bot", "hd", "ugoira")
CAPTION_LIMIT = 1024

_DYNAMIC_FOOTER = ""
_STRIP: list[str] = []
_TPL: dict = {}


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


def apply_strip(text: str) -> str:
    out = text or ""
    for pat in _STRIP:
        try:
            out = re.sub(pat, "", out, flags=re.I | re.M)
        except re.error:
            out = out.replace(pat, "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# ---- call-to-action labels (the "مواد خام" the bot sends) ----
# The machine's job is to build and lay out the caption; the WORDS that invite the
# reader to act (CTA) belong to the channel, not the machine. Each run may override
# these via set_cta(); the defaults are generic so a machine without a law still works.
_CTA = {
    "zip_link": "📦 Full quality ZIP 👉 Tap to get",
    "zip_plain": "📦 Full quality ZIP below 👇",
    "hd_link": "🖼 Original quality 👉 Tap to get",
    "hd_plain": "🖼 Original quality below 👇",
    "gallery_word": "{count} pics",
    "gallery_word_more": "{count} more pics",
    "gallery_one": "Get the album",
    "gallery_part": "Part {i} of {n}",
    "sep": " · ",
    "ugoira": "🎞 Animation",
}
# cover (the NSFW bundle whose channel image is another SFW post): {f}/{n} filled by code
_CTA_COVER = {
    "char": "See {n} like never before 🙈🔥",
    "franchise": "More {f} girls are waiting 😳",
    "none": "Want more like this? 😈 Check the bot",
    "btn_char": "🔥 More {n} · Tap to open",
    "btn_franchise": "🔥 More {f} · Tap to open",
    "btn_none": "🔥 More waifus · Tap to open",
}


def set_cta(cta: dict | None) -> None:
    """Feed this run's CTA labels from the batch. Missing keys keep the defaults."""
    global _CTA, _CTA_COVER
    for key, value in (cta or {}).items():
        if not isinstance(value, str):
            continue
        if key in _CTA:
            _CTA[key] = value
        elif key in _CTA_COVER:
            _CTA_COVER[key] = value


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
VALID_LAYOUT = {TITLE, TEASER, CTA, TAGS, FOOTER}
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
        return " ".join(tags) if tags else ""
    if b == FOOTER:
        return footer or ""
    return ""


TAG_RE = re.compile(r"<[^>]+>")

# ---------- hook sentences for "cover" posts (chosen by the relation of the two posts) ----------
HOOK_CHAR = [  # {n} = the shared character
    "Another side of {n} 😏", "{n} has a secret side 🤫💕", "You haven't seen all of {n} yet 👀",
    "More {n} is waiting for you 😳", "{n} has more to show 🔥", "This is not all of {n} 😈",
    "Want more {n}? Check the bot 💋", "The best {n} pics are in the bot 🔥",
    "There is a hotter {n} behind the link 😳", "{n} is waiting in the bot 💕",
    "See {n} like never before 🙈🔥", "{n} saved the best for the bot 😏",
    "Ready for more {n}? 😍", "{n} has a bold side too 😈", "Tap to meet the other {n} 🤭",
]
HOOK_FRANCHISE = [  # {f} = the shared franchise
    "More {f} girls are waiting 😳", "The rest of {f} is in the bot 🔥", "Love {f}? There is more 😏",
    "{f} has a bolder side 🙈", "More {f} waifus inside the bot 💋", "Not all {f} girls are this shy 😈",
    "Want more {f}? Tap below 👇🔥", "Another {f} girl is waiting 😍", "{f} fans, this one is for you 😳",
    "The bot has more {f} 💕", "Look who else is from {f} 👀", "{f} gets a little hotter in the bot 🔥",
    "More {f} art, just one tap away 🤭", "Your {f} collection is not done yet 😏",
    "Come see the other {f} girls 💖",
]
HOOK_NONE = [  # no name at all: safe when the two posts have nothing in common
    "Want more like this? 😈 Check the bot", "Something hotter is waiting for you 🔥",
    "Tap for more waifus 💋", "Your next waifu is one tap away 😳", "There is a bolder pack in the bot 🙈",
    "More waifus are hiding in the bot 👀", "Feeling curious? Tap below 😏", "The good stuff is in the bot 🔥",
    "Ready for something more? 😈", "Your next favorite is waiting 💕", "This was only the warm up 😳",
    "A secret pack is waiting for you 🤫", "Open the bot for a surprise 🎁🔥", "Not shy anymore, tap below 🙈💋",
    "Bored? Here is something spicy 🌶️😏", "Hot picks are one tap away 🔥", "More waifus, more fun 😍",
    "Don't miss the rest of the pack 👀", "The best pics are not here 😈", "A little more? Just tap 💖",
]
BTN_CHAR = [
    "🔥 More {n} · Tap to open", "💋 {n} is waiting · Tap to get", "😈 Secret {n} pics · Tap here",
    "🙈 See more {n} · Tap to see", "😳 {n} unlocked · Tap to open", "💖 The full {n} set · Tap to get",
    "🤫 Hidden {n} pics · Tap to open", "👀 More of {n} · Tap here",
]
BTN_FRANCHISE = [
    "🔥 More {f} · Tap to open", "💋 More {f} girls · Tap to get", "😈 Secret {f} pics · Tap here",
    "🙈 See more {f} · Tap to see", "😳 {f} unlocked · Tap to open", "💖 The {f} pack · Tap to get",
    "🤫 Hidden {f} pics · Tap to open", "👀 More of {f} · Tap here",
]
BTN_NONE = [
    "🔥 More waifus · Tap to open", "💋 The full pack · Tap to get", "😈 Secret pics · Tap here",
    "🙈 See more · Tap to see", "😳 Unlock the pack · Tap to open", "💖 More for you · Tap to get",
    "🤫 Hidden pics · Tap to open", "👀 Take a look · Tap here",
]
HOOKS = {"char": (HOOK_CHAR, BTN_CHAR), "franchise": (HOOK_FRANCHISE, BTN_FRANCHISE), "none": (HOOK_NONE, BTN_NONE)}


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
        out.append(("\n" if tight else "\n\n") + text if out else text)
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
    hooks, btns = HOOKS[rel]
    mem = runtime.setdefault("recentCaptions", {})
    # a channel-supplied label (set_cta) wins; otherwise rotate the generic defaults
    if _CTA_COVER.get(rel):
        hook = _CTA_COVER[rel].format(n=name, f=name)
    else:
        hook = _pick_fresh([h.format(n=name, f=name) for h in hooks], f"h:{rel}", mem)
    if _CTA_COVER.get(f"btn_{rel}"):
        btn = _CTA_COVER[f"btn_{rel}"].format(n=name, f=name)
    else:
        btn = _pick_fresh([b.format(n=name, f=name) for b in btns], f"b:{rel}", mem)

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