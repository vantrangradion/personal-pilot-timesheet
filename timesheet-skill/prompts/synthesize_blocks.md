# [AI] Block Topic Synthesis (US-2.2)

Input: the JSON array printed by `scripts/prepare_payload.py`. Each block has
`id`, `time`, and optionally `event`, `commits` (subjects grouped by repo) and
`prs` (`#N title (status)`).

For each block, write a `title` (2–4 word topic, e.g. "Calendar Harvester",
"Sprint Planning"; no PR numbers) and one `description` of 8–20 words:

- Group related commits into one or two business topics; never list commits one by one.
- Use only work present in the block. Do not invent tasks, repos, or PRs.
- A meeting block with no activity: describe the meeting from its `event` title.
- End with `PRs: #a, #b` using the block's PR numbers; omit it when the block has none.

Reply with JSON only, no prose or code fence, one key per block `id`:

{"<id>":{"title":"<topic>","description":"<topics>. PRs: #a, #b"}}
