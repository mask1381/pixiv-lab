# Batch contract — what the bot must supply to the machine

This file is the single source of truth for the **material** (موادِ خام) the bot
puts inside each batch JSON and drops in the private group. The machine is
channel-agnostic: **if a value is not listed here, the machine does not use it.**

---

## The transport

```
bot ──(sendDocument: batch.json)──▶ private group BATCH_CHAT_ID
bot ──(dispatch pixiv-lab workflow: batch_id + message_id)──▶ GitHub Actions
machine ──(Telethon reads batch.json + posts/schedules to target_chat_id)──▶ done
machine ──(/ack <batch_id> ...)──▶ private group ──▶ bot updates its DB
```

The machine validates `batch.kind == "pixiv"` and refuses anything else.

## Required fields (no default — a missing one aborts the run)

| field | type | what it is |
|---|---|---|
| `batch_id` | string | id of this run (bot-generated, echoed in every ack) |
| `kind` | string | must be `"pixiv"` |
| `target_chat_id` | int/string | the channel to post to for this run |
| `artists` | array | `[{ "user_id", "name", "rules" }]` — which pixiv artists to feed on |
| `posted` | object | `{ id: status }` seen ids (state from the previous runs) |

## Rules: `channel_law` (the channel's law — لایحه)

All decisions about *what a post looks like* come from here. The machine merges
it over each artist's own `rules` (`rules.py` deep-merge; artist rules win).

```jsonc
"channel_law": {
  "law": {
    "zip": { "sfw_after": 0, "nsfw_after": 5 },   // 0 = never zip by page count
    "split": { "full_to_channel": 10, "share_div": 3, "min_channel": 2, "max_channel": 6 },
    "nsfw": { "channel_mode": "cover_only" }       // cover_only | single_to_channel | all_to_channel
  },
  "caption": {
    "footer": true,                                // render the footer or not
    "layout": "title teaser cta hashtags footer"   // ← block order, see below
  },
  "pages": { "drop_last": false },                 // drop the trailing ad page
  "spoiler": true,                                 // NSFW photos → Telegram spoiler
  "hd_link": true,                                 // single page → original-quality link
  "hashtags": { "format_tag": true, "max_category": 8, "fallback": ["#Art"], "passthrough": { "max": 6 } }
}
```

`spoiler` (`true`/`false`, default `false`): when `true`, every **NSFW** photo in
the run is sent as a Telegram spoiler (blurred until tapped). SFW pages and
non-photo media are never spoilerred. An artist's own `rules.spoiler` overrides
the channel default.

### `caption.layout` — the arrangement is a string the bot composes

The order of caption blocks is **not** hardcoded in the machine. The bot sends
space/comma-separated block ids; the machine renders exactly that order.
Block ids: `title` `teaser` `cta` `hashtags` `footer`.
Default (no law): `title teaser cta hashtags footer`.

Examples:
- `title teaser cta hashtags footer` → normal
- `hashtags title teaser footer cta` → hashtags **before** the title
- `title hashtags teaser footer cta` → hashtags between title and teaser
- `title hashtags footer` → no teaser, no CTA at all

`title`/`teaser` stay tight (one header) only when they are adjacent; every
other block is separated by a blank line.

## Text the channel owns: `cta`, `footer_html`, `brand_tag`, `dry_bot_link`

These are the words that identify the channel. The machine renders them; it
never writes them down anywhere.

```jsonc
"footer_html": "<a href=\"https://t.me/link\">Join us</a>",   // rendered last (by layout)
"brand_tag":   "MyChannel",                                    // #MyChannel appended to hashtags
"cta": {                                                       // per-key override; missing = default
  "zip_link": "📦 Full quality ZIP 👉 Tap to get",
  "hd_link":  "🖼 Original quality 👉 Tap to get",
  "gallery_word": "{count} pics",
  "gallery_word_more": "{count} more pics",
  "gallery_one": "Get the album",
  "gallery_part": "Part {i} of {n}",
  "char": "See {n} like never before 🙈",       // cover hook (n = character)
  "franchise": "More {f} girls are waiting",    // cover hook (f = franchise)
  "none": "Want more like this? 😈",
  "btn_char": "🔥 More {n} · Tap to open",       // cover button label
  "btn_franchise": "🔥 More {f} · Tap to open",
  "btn_none": "🔥 More · Tap to open"
},
"dry_bot_link": "https://t.me/example?start=dry"  // placeholder link for dry-run captions
```

## Storage (where zips / galleries go + which bot hands out deep links)

```jsonc
"storage_bot_username":   "mystoragebot",       // @handle that replies with deep links
"storage_bot_link_label": "My Channel",         // the label line under which the machine picks the right link
```
The machine has **no default** for these; a run that needs storage must get them.

## Mode & dry-run

| field | meaning |
|---|---|
| `mode` | `"plan"` (auto-pick by schedule) or `"manual"` (post these artists now) |
| `dry` | `true` → plan + captions, no posting, no state saved; acks still sent |

## Secrets (env/GitHub secrets — the machine's own, never in a batch)

`PIXIV_REFRESH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TG_API_ID`, `TG_API_HASH`,
`TG_SESSION_STRING`, `BATCH_CHAT_ID`, `STORAGE_CHAT_ID` (fallback only).

## Acks the machine sends back (verb `px`)

```
/ack <batch_id> px <id>=<value> ...          every posted id → bot marks it posted
/ack <batch_id> blob rt <i> <n> <chunk>      runtime state (artists seen, failures)
/ack <batch_id> blob st0 <i> <n> <chunk>     tag statistics
/ack <batch_id> done
```

The bot's lab registry must map `kind == "pixiv"` → ack verb `px`
(the two constants at the top of `scripts/process_batch.py`).
