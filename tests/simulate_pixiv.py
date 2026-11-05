#!/usr/bin/env python3
"""Simulation harness for pixiv-lab. No network, no Telegram.

Synthesizes every illust shape the real Pixiv API produces, then drives the machine
through its full delivery matrix with downloads and sends mocked, asserting the
behavior a real run must show.

Run:  python tests/simulate_pixiv.py
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---- recorders (replace real I/O) -------------------------------------------------
CALLS: list[dict] = []          # everything the machine tried to send / upload
FAILS: list[str] = []           # exceptions raised by mocked delivery
_LINK = "https://t.me/storagebot/formocked"

def _png(w=10, h=10) -> bytes:
    """A tiny valid PNG (1x1-ish) so PIL-based code paths run for real."""
    import struct, zlib
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00\xff" * w for _ in range(h))
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

def _claim(what):
    def wrap(fn):
        def inner(*a, **k):
            CALLS.append({"what": what, "args": a, "kwargs": k})
            log: list[int] = [1]
            return log
        return inner
    return wrap

# ---- source synthesis (the real Pixiv API shapes) ---------------------------------
def _page_urls(pid, idx) -> dict:
    """The real app-api meta_pages.img_urls shape: all four sizes present."""
    base = f"https://i.pximg.net/c/540x540/img-master/1/2/3/{pid}_p{idx}_master1200.jpg"
    return {
        "square_medium": base,
        "medium": base,
        "large": base,
        "original": f"https://i.pximg.net/img-original/img/2024/01/01/00/00/00/{pid}_p{idx}.png",
    }


def illust(**kw) -> dict:
    """A raw pixiv illust/list item in the app-api shape.

    Real app-api semantics kept faithful:
      * single illust  -> meta_single_page set, NO meta_pages key
      * multi illust   -> meta_pages (one entry per page), no meta_single_page
    (The machine keys strictly off meta_pages presence for page count.)
    """
    base = {
        "id": 124, "title": "Test Art", "type": "illust", "x_restrict": 0,
        "sanity_level": 2, "page_count": 1, "width": 1200, "height": 1600,
        "create_date": "2024-01-01T00:00:00+00:00",
        "total_view": 5000, "total_bookmarks": 120,
        "tags": [{"name": "girl"}],
        "caption": "",
        "user": {"id": 999, "name": "Test Artist", "account": "test_artist"},
        "image_urls": _page_urls(124, 0),
        "meta_single_page": {"original_image_url": f"https://i.pximg.net/img-original/img/2024/01/01/00/00/00/124_p0.png"},
    }
    base.update({k: v for k, v in kw.items() if v is not None})
    return base

NSFW = {"x_restrict": 1, "sanity_level": 6}
UGOIRA = {
    "type": "ugoira", "page_count": 1,
    # no meta_pages, no meta_single_page.original — the "video" shape
    "image_urls": {"square_medium": "https://i.pximg.net/c/540x540/img-master/1/2/3/124_ugoira0_master1200.jpg"},
}

def pages(n, base=None):
    """A consistent n-page manga: page_count == len(meta_pages), each page with all 4 sizes."""
    base = base or illust()
    out = {**base, "page_count": n}
    if n == 1:
        # single keeps meta_single_page, no meta_pages key
        out["meta_pages"] = None
    else:
        # multi: meta_pages only, strip meta_single_page (real API never sends both)
        out["meta_single_page"] = None
        out["meta_pages"] = [
            {"image_urls": _page_urls(base["id"], i)}
            for i in range(n)
        ]
    return out

# ---- patching ----------------------------------------------------------------------
import pixiv_lab.config as cfg_mod
import pixiv_lab.images as img_mod
import pixiv_lab.plan as plan_mod
import pixiv_lab.pipeline as pipe_mod
import pixiv_lab.userbot_schedule as usched_mod
import pixiv_lab.userbot_zip as uzip_mod
import pixiv_lab.pixiv as pix_mod

# fake raw illust fetches (listited + detail)
DETAIL_STORE: dict[str, dict] = {}
def _fake_fetch_user_illusts(client, token, uid, posted=None, **kw):
    # build_pools does the real dedup (posted_ids); this fake just mimics the source list.
    return [raw for key, raw in DETAIL_STORE.items() if key.startswith(f"{uid}:")]
def _fake_fetch_illust_detail(client, token, iid):
    key = str(iid)
    for skey, raw in DETAIL_STORE.items():
        if skey.endswith(":" + key):
            return raw
    raise RuntimeError(f"no detail for {iid}")
def _fake_download_image(client, item):
    return _png()
def _fake_download_original(client, page):
    return f"page_{page.get('id')}.png", _png()

# record schedule / storage calls
def _rec(fn_name):
    def inner(*a, **k):
        CALLS.append({"what": fn_name, "args": [_fmt(x) for x in list(a)], "kwargs": k})
        return [1]
    return inner

def _fmt(x):
    if isinstance(x, (bytes, io.BytesIO)):
        return "<bytes>"
    if isinstance(x, Path):
        return str(x.name)
    return x

def _install(cfg=None):
    # Every consumer module binds these names at import time — patch where they're used.
    for mod in (pix_mod, plan_mod, pipe_mod):
        mod.fetch_user_illusts = _fake_fetch_user_illusts
        mod.fetch_illust_detail = _fake_fetch_illust_detail
        mod.refresh_access_token = lambda client, token: "mock-token"
    for mod in (img_mod, plan_mod):
        mod.download_image = _fake_download_image
    for mod in (uzip_mod,):
        mod.download_original = _fake_download_original
    plan_mod.edit_image = lambda d, r: d
    # schedule_album/schedule_text are consumed by *both* plan.py (asserted at import)
    # and userbot_schedule itself (for direct calls) — patch every binding.
    for mod in (usched_mod, plan_mod):
        mod.schedule_album = _rec("schedule_album")
        mod.schedule_text = _rec("schedule_text")
        mod.schedule_video = _rec("schedule_video")
    # ugoira conversion bypass: harness verifies delivery routing, not video encoding
    plan_mod.convert_ugoira_to_video = lambda c, t, iid, wdir, f=None: (f"{iid}.mp4", b"fake-video")
    for mod in (usched_mod, plan_mod):
        mod.schedule_video = _rec("schedule_video")
    def send_gallery_to_storage(client, pages, workdir, base_name, rules=None):
        # positional order mirrors real call: (client, pages, workdir, base_name, rules)
        CALLS.append({"what": "send_gallery_to_storage", "args": (client, pages, workdir, base_name, rules), "kwargs": {}})
        # real shape: list[str] deep links, one per uploaded part (<= GALLERY_PART_SIZE pages each)
        return [f"{_LINK}part{i}" for i in range(1, (len(pages) + 9) // 10 + 1)]
    def send_hd_to_storage(client, page, workdir, base_name):
        CALLS.append({"what": "send_hd_to_storage", "args": (client, page, workdir, base_name), "kwargs": {}})
        return f"{_LINK}hd{page.get('id')}"
    def send_zip_to_storage(zip_path):
        CALLS.append({"what": "send_zip_to_storage", "args": (zip_path,), "kwargs": {}})
        return SimpleNamespace(link=_LINK)
    for mod in (uzip_mod, plan_mod):
        mod.send_zip_to_storage = send_zip_to_storage
        mod.send_gallery_to_storage = send_gallery_to_storage
        mod.send_hd_to_storage = send_hd_to_storage
    for mod in (uzip_mod, plan_mod):
        mod.userbot_configured = lambda: True
        mod.storage_configured = lambda: True
    for mod in (plan_mod,):
        mod.prepare_zip = lambda client, pages, iid, workdir, rules=None, name_hint=None: (
            workdir / f"{name_hint or 'zip'}_{iid}.zip"
        )
        mod.list_scheduled = lambda chat: []
        mod.upcoming_slots = lambda conf, now: [
            plan_mod.Slot(now.replace(hour=12, minute=0), "post"),
            plan_mod.Slot(now.replace(hour=22, minute=0), "zip"),
        ]

def _batch(**kw) -> dict:
    b = {
        "batch_id": "sim-1", "kind": "pixiv",
        "target_chat_id": "100000",
        "artists": [],
        "dry": False,
    }
    b.update(kw)
    return b

RESULT = {"ok": 0, "fail": 0}
def check(name, cond, detail=""):
    if cond:
        RESULT["ok"] += 1
        print(f"  PASS {name}")
    else:
        RESULT["fail"] += 1
        print(f"  FAIL {name}: {detail}")

def _make_cfg(batch):
    from pixiv_lab.config import env_config
    from pixiv_lab.state import State
    cfg = env_config(batch)
    cfg["dry_run"] = bool(batch.get("dry"))
    # mirrors process_batch.run(): state is seeded from the batch, not the filesystem.
    cfg["state"] = State(batch.get("posted"), batch.get("runtime"), None, dry=cfg["dry_run"])
    cfg.pop("posted_path", None)
    cfg.pop("runtime_path", None)
    return cfg

def reset():
    CALLS.clear(); FAILS.clear(); DETAIL_STORE.clear()

# -----------------------------------------------------------------------------------
# 1. SFW single with hd_link -> one channel photo + hd storage link, no spoiler
def t_single_sfw_hd():
    print("[1] SFW single with hd_link")
    reset()
    _install()
    DETAIL_STORE["999:777"] = illust(id=777, user={"id": 999, "name": "A", "account": "a"})
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}]))
    cfg["channel_law"] = {}
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    hds = [c for c in CALLS if c["what"] == "send_hd_to_storage"]
    check("hd single: 1 album, 1 media", len(albs) == 1 and len(albs[0]["args"][1]) == 1)
    check("hd single: sends hd to storage", len(hds) == 1)
    check("no spoiler on SFW", albs and not albs[0]["kwargs"].get("spoiler"))

# 2. SFW single with hd_link=false (law) -> album only, no storage
def t_single_sfw_no_hd():
    print("[2] SFW single, hd_link=false via channel_law")
    reset()
    _install()
    DETAIL_STORE["999:778"] = illust(id=778, user={"id": 999, "name": "A", "account": "a"})
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}],
                           channel_law={"hd_link": False}))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    hds = [c for c in CALLS if c["what"] == "send_hd_to_storage"]
    check("no hd when hd_link=false", len(hds) == 0)
    check("album still sent", len(albs) == 1)

# 3. NSFW cover_only default -> ALL pages to bot storage, channel gets own cover
def t_nsfw_cover_only():
    print("[3] NSFW cover_only (default)")
    reset()
    _install()
    DETAIL_STORE["999:790"] = pages(4, illust(id=790, user={"id": 999, "name": "A", "account": "a"}, **NSFW))
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}]))
    plan_mod.run_plan(cfg, dry=False)
    all_c = [c for c in CALLS if c["what"] == "schedule_album"]
    gals = [c for c in CALLS if c["what"] == "send_gallery_to_storage"]
    check("NSFW cover_only: full album to storage", len(gals) == 1 and len(gals[0]["args"][1]) == 4)
    check("channel gets own cover (1 media)", len(all_c) >= 1 and len(all_c[0]["args"][1]) == 1)

# 4. NSFW all_to_channel -> whole album in channel
def t_nsfw_all_to_channel():
    print("[4] NSFW all_to_channel")
    reset()
    _install()
    DETAIL_STORE["999:791"] = pages(3, illust(id=791, user={"id": 999, "name": "A", "account": "a"}, **NSFW))
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}],
                           channel_law={"law": {"nsfw": {"channel_mode": "all_to_channel"}}}))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    gals = [c for c in CALLS if c["what"] == "send_gallery_to_storage"]
    check("all_to_channel: album to channel of 3", len(albs) == 1 and len(albs[0]["args"][1]) == 3)
    check("all_to_channel: no gallery to storage", len(gals) == 0)

# 5. NSFW single_to_channel -> first page(s) to channel, rest to bot storage
def t_nsfw_single_to_channel():
    print("[5] NSFW single_to_channel (single_in_channel=2)")
    reset()
    _install()
    # 4 pages (below the NSFW zip threshold nsfw_after=5) so the law reaches single_to_channel,
    # not the zip branch.
    DETAIL_STORE["999:792"] = pages(4, illust(id=792, user={"id": 999, "name": "A", "account": "a"}, **NSFW))
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}],
                           channel_law={"law": {"nsfw": {"channel_mode": "single_to_channel", "single_in_channel": 2}}}))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    gals = [c for c in CALLS if c["what"] == "send_gallery_to_storage"]
    check("single_to_channel: channel gets 2", len(albs) == 1 and len(albs[0]["args"][1]) == 2)
    # full album in bot storage exactly once (no redundant preview re-upload), and the
    # channel caption must deep-link the FULL album, not just the visible pages.
    check("single_to_channel: full album to storage once", len(gals) == 1 and len(gals[0]["args"][1]) == 4)

# 6. spoiler => NSFW channel posts carry spoiler=True
def t_spoiler_rule():
    print("[6] spoiler rule on NSFW")
    reset()
    _install()
    DETAIL_STORE["999:793"] = pages(3, illust(id=793, user={"id": 999, "name": "A", "account": "a"}, **NSFW))
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}],
                           channel_law={"spoiler": True, "law": {"nsfw": {"channel_mode": "all_to_channel"}}}))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    check("spoiler=True passed to album", len(albs) == 1 and albs[0]["kwargs"].get("spoiler") is True)

# 7. SFW zip via law threshold -> zip to storage, cover to channel, no spoiler
def t_zip_law():
    print("[7] SFW zip (law.zip.sfw_after=3)")
    reset()
    _install()
    DETAIL_STORE["999:794"] = pages(3, illust(id=794, user={"id": 999, "name": "A", "account": "a"}))
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}],
                           channel_law={"law": {"zip": {"sfw_after": 3}}}))
    plan_mod.run_plan(cfg, dry=False)
    zips = [c for c in CALLS if c["what"] == "send_zip_to_storage"]
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    check("zip built + sent to storage", len(zips) == 1)
    check("cover to channel", len(albs) == 1 and len(albs[0]["args"][1]) == 1)

# 8. SFW >10 pages -> split: ch in channel (law), rest to bot gallery
def t_sfw_split():
    print("[8] SFW 20 pages with split law")
    reset()
    _install()
    DETAIL_STORE["999:795"] = pages(20, illust(id=795, user={"id": 999, "name": "A", "account": "a"}))
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}],
                           channel_law={"law": {"split": {"full_to_channel": 10, "share_div": 3, "min_channel": 2, "max_channel": 6}}}))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    gals = [c for c in CALLS if c["what"] == "send_gallery_to_storage"]
    ch = albs[0]["args"][1] if albs else []
    rest = gals[0]["args"][1] if gals else []
    check("split: channel gets a slice (<=6)", 0 < len(ch) <= 6)
    check("split: rest to storage", len(ch) + len(rest) == 20)

# 9. ugoira/video — THE GAP: source sends animated, machine must not silently drop
def t_ugoira():
    print("[9] ugoira (animated) — must not be silently skipped")
    reset()
    _install()
    raw = illust(**{**UGOIRA, "id": 796})
    raw["meta_pages"] = None
    raw["meta_single_page"] = {"original_image_url": "https://i.pximg.net/img-original/img/x/796_ugoira0.png"}
    DETAIL_STORE["999:796"] = raw
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}],
                           channel_law={"law": {"zip": {"sfw_after": 3}}}))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    gals = [c for c in CALLS if c["what"] == "send_gallery_to_storage"]
    zips = [c for c in CALLS if c["what"] == "send_zip_to_storage"]
    vids = [c for c in CALLS if c["what"] == "schedule_video"]
    check("ugoira yields at least one attempt (not silent zero)", len(albs) + len(gals) + len(zips) + len(vids) > 0,
          f"calls={CALLS}")

# 10. SFW manga 2 pages default -> album of 2, no storage
def t_small_sfw_album():
    print("[10] SFW 2-page manga -> album of 2 in channel")
    reset()
    _install()
    DETAIL_STORE["999:797"] = pages(2, illust(id=797, user={"id": 999, "name": "A", "account": "a"}))
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}]))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    check("2-page album of 2", len(albs) == 1 and len(albs[0]["args"][1]) == 2)

# 11. artist blocked content -> post skipped, no sends
def t_blocked():
    print("[11] artist with only blocked content")
    reset()
    _install()
    DETAIL_STORE["999:798"] = illust(id=798, user={"id": 999, "name": "A", "account": "a"},
                                     tags=[{"name": "gore"}])
    import pixiv_lab.blocklist as bl
    saved = (plan_mod.blocked_reason, pipe_mod.blocked_reason)
    blocked_reason = lambda it: "gore tag banned"
    try:
        for mod in (plan_mod, pipe_mod):
            mod.blocked_reason = blocked_reason
        cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}]))
        plan_mod.run_plan(cfg, dry=False)
        check("blocked post never sent", len(CALLS) == 0, str(CALLS))
    finally:
        # restore real function so later tests are not silently blocked
        plan_mod.blocked_reason, pipe_mod.blocked_reason = saved

# 12. empty artist list -> clear error, not silent
def t_empty_artists():
    print("[12] empty artist list")
    reset()
    _install()
    cfg = _make_cfg(_batch(artists=[]))
    try:
        plan_mod.run_plan(cfg, dry=False)
        check("empty artists -> raised", False, "no error")
    except Exception:
        check("empty artists -> raised", True, "")

# 13. all posted (already seen) -> nothing scheduled
def t_all_posted():
    print("[13] everything already posted (dedup)")
    reset()
    _install()
    DETAIL_STORE["999:799"] = illust(id=799, user={"id": 999, "name": "A", "account": "a"})
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {}}],
                           posted={"pixiv:799": "sent:post:2024-01-01T12:00"}))
    plan_mod.run_plan(cfg, dry=False)
    check("no sends when everything posted", len(CALLS) == 0, str(CALLS))

# 14. artist rules override channel_law (per-key deep merge, artist wins)
def t_artist_overrides_channel_law():
    print("[14] artist rules override channel_law")
    reset()
    _install()
    DETAIL_STORE["999:770"] = illust(id=770, user={"id": 999, "name": "A", "account": "a"})
    # channel_law says hd_link True (the default); the artist forces it off.
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {"hd_link": False}}],
                           channel_law={"hd_link": True}))
    plan_mod.run_plan(cfg, dry=False)
    hds = [c for c in CALLS if c["what"] == "send_hd_to_storage"]
    check("artist hd_link=False wins over channel_law", len(hds) == 0)

# 15. artist zip rule forces a zip on a small post the law would not zip
def t_artist_forces_zip():
    print("[15] artist zip rule forces zip (zip_enabled wins)")
    reset()
    _install()
    DETAIL_STORE["999:771"] = pages(2, illust(id=771, user={"id": 999, "name": "A", "account": "a"}))
    # channel law has no zip; artist adds one -> all posts become zips.
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {"zip": {"pages": "all"}}}],
                           channel_law={}))
    plan_mod.run_plan(cfg, dry=False)
    zips = [c for c in CALLS if c["what"] == "send_zip_to_storage"]
    check("zip enforced by artist rule", len(zips) == 1)

# 16. SFW merge_singles: 4 single-page posts -> one album (BOT_MIN-style), set via number
def t_merge_singles():
    print("[16] SFW merge_singles -> bundles singles into an album")
    reset()
    _install()
    for i in range(4):
        DETAIL_STORE[f"999:{800+i}"] = illust(id=800 + i, user={"id": 999, "name": "A", "account": "a"},
                                             total_view=1000 + i)
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {"pages": {"merge_singles": {"min": 4, "max": 8, "max_wait_days": 3}}}}]))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    zips = [c for c in CALLS if c["what"] == "send_zip_to_storage"]
    check("merge_singles: one album (4 singles bundled)", len(albs) >= 1)
    merged_media = sum(len(a["args"][1]) for a in albs if isinstance(a["args"][1], list))
    check("merge_singles: 4 pages delivered total", merged_media == 4, f"albs={len(albs)} media={merged_media}")

# 17. artist rule drop_last=true drops the last page of a multi-page post
def t_drop_last():
    print("[17] pages.drop_last -> drops final page")
    reset()
    _install()
    DETAIL_STORE["999:772"] = pages(3, illust(id=772, user={"id": 999, "name": "A", "account": "a"}))
    cfg = _make_cfg(_batch(artists=[{"user_id": "999", "name": "A", "rules": {"pages": {"drop_last": True}}}]))
    plan_mod.run_plan(cfg, dry=False)
    albs = [c for c in CALLS if c["what"] == "schedule_album"]
    check("drop_last: 3-page becomes 2-page album", len(albs) == 1 and len(albs[0]["args"][1]) == 2)

if __name__ == "__main__":
    for fn in sorted((v for v in list(globals().values()) if callable(v) and v.__name__.startswith("t_")), key=lambda f: f.__name__):
        fn()
    print()
    print(f"RESULT {RESULT['ok']} passed, {RESULT['fail']} failed")
    sys.exit(1 if RESULT["fail"] else 0)
