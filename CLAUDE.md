# Working rules

## Tests
- Do not write unit tests for small functions or individual changes. They waste tokens and add noise.
- Only add a test when it is a broad, well-scoped integration test covering a whole flow end to end
  (e.g. Telegram message → router → agent → store → reply). Keep these few.
- Don't add tests unless asked; verify changes by running the real thing (CLI / bot) instead.
- Don't expand the existing test suite as a side effect of other work.
