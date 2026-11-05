"""pixiv-lab — a channel-agnostic pixiv post factory.

The machine turns raw material (a batch from the bot) into finished posts:
  - fetches pixiv illusts (its own token)
  - generates captions with the layout/labels the bot supplied
  - downloads, resizes, zips
  - posts to Telegram via the userbot or schedules
  - reports everything back as acks

No channel's identity, name, or rules are hardcoded here — every rule arrives
per run as material from the bot, so this repo stays safe to share/public.
"""