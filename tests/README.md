# Tests

```bash
./venv/bin/python -m pytest
```

## What's covered, and why these parts

The suite targets the code that fails *silently* — where a bug produces a
plausible-looking number or a request that quietly succeeds, rather than a
crash someone would notice:

- **`test_engine.py`** — fills land on the correct side of the spread, PnL
  direction and magnitude for both longs and shorts, realised PnL hitting the
  balance exactly once, partial closes, margin rejection, stop-out, and SL/TP
  triggers *including the exact boundary* (a stop must fire when price touches
  it, and must not fire one tick short).
- **`test_permissions.py`** — the investor password genuinely cannot move the
  fund, checked at the API rather than by trusting the UI to hide buttons, plus
  the idempotency guard that stops a double-tapped Buy opening two positions.

Prices come from a fake feed the test drives directly (`fakes.py`), so every
assertion is an exact expected value instead of "some plausible number came
out". Supabase is replaced with a small in-memory store — real enough to
exercise the engine's actual query patterns, no network, suite runs offline.

## Verifying the tests actually catch things

These tests were checked by mutation: deliberately breaking one thing in
`engine.py` and confirming a test fails. That found two real gaps that were
then filled — closing a *short* had no realised-PnL coverage at all, and the
SL boundary test was derived from a mid price, so float rounding put the quote
just off the trigger and a `<=` → `<` bug slipped straight through.

Worth knowing if you repeat that exercise: several mutations
(`+ pnl` → `- pnl`, swapping `bid`/`ask`) are *byte-identical in length*, and
CPython invalidates `.pyc` files on mtime **and size**. Rewriting the file
quickly can leave stale bytecode running, so tests pass against code that is
no longer on disk. Clear the cache between mutations:

```bash
find . -name __pycache__ -type d -not -path './venv/*' -exec rm -rf {} +
```
