"""Token-free verification of the bot-controlled caption surface.

Exercises set_layout / set_cta / set_footer / set_brand_tag and the layout engine
in pixiv_lab.caption — no network, no pixiv token, no Telegram.
Note: the brand tag and format tags are appended by pixiv_lab.tags.classify_tags
(a higher layer); this test hands build_caption its already-built tag list, so it
verifies only what caption.py itself owns: ordering, spacing, labels, overrides.
Run:  python test_layout.py
"""
import logging
import sys

# Windows consoles default to cp1252; emoji in the CTA defaults crash print().
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pixiv_lab.caption as C
import pixiv_lab.tags as T

logging.basicConfig(level=logging.WARNING, format="%(message)s")

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# fresh state
C.set_layout("")
C.set_cta({})
C.set_footer("")
T.set_brand_tag("")

fake_parts = {"title": "Title & <b>bold</b>", "teaser": "Teaser line"}
fake_hashtags = ["#Character", "#Franchise", "#Art"]

# 1) hashtags BEFORE title; order + spacing must match the batched layout exactly
C.set_layout("hashtags title teaser cta footer")
out = C._assemble(fake_parts, "CTA TEXT", fake_hashtags, "<a href='x'>Join us</a>")
expected = "#Character #Franchise #Art\n\n<b>Title &amp; &lt;b&gt;bold&lt;/b&gt;</b>\n<i>Teaser line</i>\n\n<blockquote>CTA TEXT</blockquote>\n\n<a href='x'>Join us</a>"
check("layout hashtag-first exact", out == expected, repr(out))

# 1b) title/teaser stay TIGHT (one newline) only when adjacent; other blocks get blank lines
check("tight title/teaser (decoded)", "\n<i>Teaser" in out and "\n\n<i>Teaser" not in out, repr(out))
check("blank line before CTA block", "\n\n<blockquote>" in out, repr(out))

# 2) CTA override via set_cta flows into build_caption (zip link label + plain fallback)
C.set_layout("title teaser cta hashtags footer")
C.set_cta({"zip_link": "MY ZIP LINK LABEL", "zip_plain": "MY PLAIN"})
link_out = C.build_caption(fake_parts, "zip", 5, ["#ow"], None, page_url="https://t.me/x")
plain_out = C.build_caption(fake_parts, "zip", 5, ["#ow"], None, page_url=None)
check("zip_link override", "MY ZIP LINK LABEL" in link_out and "Tap to get" not in link_out, link_out)
check("zip_plain override", "MY PLAIN" in plain_out and "below" not in plain_out, plain_out)

# 2b) gallery word override for album/bot format; layout has no hashtags block -> tags not shown
C.set_cta({"gallery_word": "{count} shots"})
C.set_layout("title teaser cta footer")
gout = C.build_caption(fake_parts, "bot", 4, ["#a"], None, page_url="https://t.me/x")
check("gallery_word override+link", "4 shots" in gout and "https://t.me/x" in gout and "#a" not in gout, gout[:200])
C.set_cta({})

# 3) footer flows in from set_footer
C.set_footer("<a href='https://t.me/link'>Join us</a>")
out = C.build_caption(fake_parts, "bot", 6, ["#waifu"], None)
check("footer rendered", "Join us" in out, out[-120:])

# 3b) caption.footer=false in rules suppresses the footer
out = C.build_caption(fake_parts, "bot", 6, ["#waifu"], {"caption": {"footer": False}})
check("footer suppressed by rule", "Join us" not in out, repr(out))
C.set_footer("")

# 4) no teaser: layout without the teaser block drops it
C.set_layout("title cta footer")
out = C._assemble(fake_parts, "CTA", ["#a"], "<footer>")
check("no teaser", "Teaser" not in out and out.startswith("<b>Title"), out[:100])

# 5) invalid layout keeps the last valid one; garbage never breaks a run
C.set_layout("bogus nothing")
check("invalid layout ignored", C._LAYOUT == ("title", "cta", "footer"), C._LAYOUT)

# 6) cta: unknown keys and non-string values are ignored, valid ones applied
C.set_cta({"bogus": "x", "btn_none": 3, "sep": " | ", "zip_link": "new"})
check("unknown/non-string cta ignored, valid applied",
      C._CTA["sep"] == " | " and C._CTA["zip_link"] == "new" and C._CTA["gallery_one"] == "open",
      {k: C._CTA[k] for k in ("sep", "zip_link", "gallery_one")})

# 7) set_brand_tag normalizes and stores (used by classify_tags, verified here)
T.set_brand_tag("#MyChannel")
check("brand tag normalized", T.get_brand_tag() == "#MyChannel", T.get_brand_tag())
T.set_brand_tag("")
check("empty brand tag clears", T.get_brand_tag() == "", T.get_brand_tag())

print()
print("all checks passed") if not failures else print(f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
