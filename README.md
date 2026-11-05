# pixiv-lab

A channel-agnostic **pixiv post factory**. It receives raw material (a batch)
from the bot, does the work, and reports back. It knows nothing about any single
channel — no bot username, no brand name, no CTA wording, no rule is hardcoded
here. Every rule and every word arrives per-run as material.

```
Bot (operator) ── batch.json ──▶ private group ──▶ GitHub Actions
Machine ── reads batch, fetches pixiv, builds captions, posts/schedules ──▶ done
Machine ── /ack ... ──▶ private group ──▶ Bot updates its DB
```

## What it does per batch

1. Reads the batch file from the private group (`BATCH_CHAT_ID`)
2. For each requested artist: fetch illusts, pick by the law, plan the post,
   build the caption (layout + labels from the batch), download/resize/zip,
   post to `target_chat_id` (or schedule), store zips via the storage bot
3. Reports `posted` ids and state back as `/ack`s

## Structure

```
scripts/process_batch.py   entry point; kind="pixiv", ack verb "px"
pixiv_lab/                 the machine (pixiv fetch, caption, rules, plan,
                           schedule, delivery, userbot zip, tags, state, …)
docs/BATCH.md              ← THE contract: everything the bot must supply
config/schedule.json       default plan schedule
data/*.csv                 shared dictionaries (character, tags)
```

**Everything that varies per channel is material, not code.** The one file that
defines how a caption is assembled (block order, labels, teasers) is
`pixiv_lab/caption.py` — but it reads its configuration from `set_layout()` /
`set_cta()` / `set_footer()` / `set_brand_tag()`, which the bot fills from the
batch. If you want a new caption block or new channel-rule, that is a code
change here; if you want to rearrange/relabel existing ones, that is a batch
change from the bot.

## Run

```
python scripts/process_batch.py --batch-id <id> --message-id <msg_id>
```

Secrets come from env / GitHub Actions secrets. See `docs/BATCH.md` for the full
batch contract and the ack protocol.
