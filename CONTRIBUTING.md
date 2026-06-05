# Contributing

Thanks for taking the time — this is a **beta**, and feedback is exactly what it
needs right now. You do not have to write code to help; the most valuable
contributions at this stage are clear reports of where the tool falls short.

## The easiest, most useful thing: open an issue

If something is confusing, broken, missing, or just doesn't fit your field,
[open an issue](../../issues). **Concrete beats polite.** A good report says:

- **What you were trying to do** (your topic / what you expected).
- **What happened** (and what you expected instead) — paste the command and the
  output or error, if any.
- **Whether it's a config issue or a tool limitation**, as best you can tell. The
  README's "[Help us improve it](README.md#help-us-improve-it--finding-limitations-with-your-assistant)"
  section has prompts you can give your AI assistant to help you figure this out
  and draft the report.

Feature requests, "this assumption doesn't hold for my field" reports, and
documentation gaps are all welcome — not just bugs.

When pasting output, **don't include anything private**: API keys, institutional
proxy URLs/tokens, or large dumps of copyrighted abstracts. A few lines is enough.

## If you want to contribute code

1. **Open an issue first** to discuss the change, so effort isn't wasted on
   something that doesn't fit the project's direction.
2. Fork, branch, and keep pull requests **small and focused** — one concern per PR
   is much easier to review than a sweeping rewrite.
3. **Don't commit generated data or secrets.** The `.gitignore` already excludes
   the corpus files (`citation_graph.json`, `engagement_matrix.json`, caches,
   `reading/`, `txt/`, etc.) and `.env`-style files. Please keep it that way.
4. If your change touches the LLM calls, route them through
   [`llm_client.py`](llm_client.py) (don't add new direct `anthropic` SDK calls in
   the pipeline scripts) so the provider stays swappable in one place.
5. Match the surrounding style. The scripts favor self-contained files with a
   docstring at the top explaining what the stage does and why.

## Expectations (it's a beta)

This is a personal research tool released early for feedback. Responses may be
slow, and not every suggestion will be adopted — but every clear report helps
shape where it goes. Thank you.
