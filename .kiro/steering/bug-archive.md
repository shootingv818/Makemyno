# Bug Archive — symptom → root cause → fix

Every entry here cost real production time. Read the symptom column before
debugging anything that looks familiar: several of these presented as a broken
*feature* and were actually a broken *invariant*.

## The big one: "channel creation fails with INVALID_AUTH"

**Symptom.** Creating a channel failed with
`rubpy.exceptions.InvalidAuth: {'status_det': 'INVALID_AUTH'}` on the master AND
on every worker, on healthy accounts, right after a successful login, and
sometimes right after the same account had sent hundreds of messages. In the same
period, reading contacts returned **zero** on accounts with thousands.

**Two wrong theories that cost hours.**
1. "The warm connection is being reused for a signed call." Partly true and worth
   fixing (see below), but it was not the cause — the fix changed nothing.
2. "The client code differs from the reference." It does not. `open_client`,
   `connect_ready`, `_import_key_from_private`, `create_channel`,
   `add_channel_members` are byte-identical to `Makiioo`, and `rubpy` is pinned to
   the same 7.3.5 in both.

**Actual root cause.** The session FILE was on a different server than the job ran
on. rubpy does not fail loudly in that state — it connects **unauthenticated**.
An unauthenticated client reads zero contacts and gets INVALID_AUTH on the first
*signed* call (`addChannel`), which is why the two symptoms always travelled
together and why plain sends could still appear to work.

Three independent ways the file and the job ended up apart:

| Cause | Why |
| --- | --- |
| `db.add_account` never `UPDATE`d `worker_id` | `INSERT OR IGNORE` kept the value from the *first* login forever, so a re-login that landed on another server still routed every job to the old one. |
| Token login wrote no session file | `_step_token` stored the five values in `session_blob` and stopped, while the card said "Session Saved: YES". No server had a session at all. |
| A rebuilt / freshly provisioned worker | Its session store is empty for accounts the master still believes live there. |

**Fix.** Ported the reference's portable-session subsystem, which Makemyno was
missing entirely:
- `rb.import_session()` — write-only `session.insert` of the five values
  (auth, private_key, guid, phone, user_agent). **Never connects**, so it cannot
  cause AUTH_FROM_ANOTHER.
- `worker_api` `POST /session/import` — the same thing on a worker.
- `worker.push_session()` — master → worker placement.
- `session_store.place()` — put the stored session on the account's server.
- `session_store.run_with_repair()` — run an operation; on an **auth-shaped**
  error only, place the session and retry **exactly once**.
- `db.add_account` now updates `worker_id` when the caller names a server (and
  leaves it alone when the caller passes `None`).

**Lesson.** `INVALID_AUTH` is not proof of a dead account. Ask *where is the
session file, and where is this code running* before touching auth logic.

## Related: signed calls over a reused warm socket

Rubika rejects `addChannel` / `addChannelMembers` issued over a socket the
account has been sending on. `account_conn.fresh_connection` / `fresh_call` close
the warm connection, run the signed work on a dedicated client and disconnect it —
the pattern `Makiioo` uses for every signed operation. Correct and kept, but on
its own it did not fix the INVALID_AUTH above.

## Other resolved bugs

| Symptom | Root cause | Fix |
| --- | --- | --- |
| Worker: "no contacts to send to" on an account with hundreds, for a plain-text send | `/prepare` refused to return the recipient list unless a marked post existed. Text mode needs no marker. | `/prepare` always returns recipients; marker reported separately via `marker_found` / `message_id`; master passes `mode`. |
| Pool report: two accounts "done, sent 0", no reason | The per-recipient loop swallowed every non-auth exception with `except Exception: pass`, then marked the account `done`. | Failures counted, last reason recorded, account marked `failed` with the reason when it reached nobody. |
| `Targets: 2` on an account with hundreds of contacts | `get_ordered_recipients` returned `(ordered, stats)` and callers used the whole tuple as the list. | Returns a plain list. `_guids_only()` normalises dicts → guid strings on both paths. |
| "marker not found" never fired; forwards sent with a `None` id | `find_marked_message` returned `(guid, message_id)`, and a 2-tuple is truthy even as `(guid, None)`. | Returns the message id or `None`. |
| Every Rubika login died with `'NoneType' object is not callable` at `db.add_account` | `finish_login` returned the raw rubpy object, which carries a field literally named `get` whose value is `None`, so `info.get("name")` was `None("name")`. | Returns a normalised dict; identity read from `get_me()`. |
| Worker container up but not listening on 8765; master saw "Server disconnected without a response" | pydantic v2 models defined **inside** `build_app()` are forward refs it cannot resolve → `PydanticUndefinedAnnotation: name 'StartLogin' is not defined` at import. | Models moved to module level. The pydantic stub in `tests/stubs.py` now reproduces this. |
| Worker error surfaced only as `400 Bad Request` | `api_call` called `raise_for_status()`, discarding the body that held the real reason. | `WorkerAPIError` carries the worker's own detail, status code and tag. |
| Owner bot answered `/start` twice; customer bot never started | Role came from an env var; systemd and compose disagree on precedence. | Role is argv (`main.py owner|customer|worker`). |
| Contact export always returned zero numbers | Export read `get_contacts_full()`, whose dicts carry no phone, so `item.get("phone")` was always `None`. | Ported `get_contact_phones()` from the reference. |
| INVALID_AUTH on every operation right after a login | The login client stayed connected, so the next call opened a second connection on the same session; rubpy also commits its session store on `disconnect()`. | `finish_login` disconnects before returning. |
| Docker build failed / looked hung, compiling telethon from source | A server's `pip.conf` pointed at a mirror serving the sdist instead of the wheel. | `-i https://pypi.org/simple` + `--prefer-binary` in the Dockerfile. `DOCKER_BUILDKIT=0`. |
| Provisioning failed on a wall of apt output | One flaky apt mirror failed the whole image, although the packages are only a fallback. | `|| true` on the apt step. |
| Stale behaviour after a deploy | Leftover `.pyc` files. | Deploy clears them. |

## Testing notes

- `tests/stubs.py` stubs `rubpy`, `telethon`, `httpx` and `pydantic` — none are
  installed in the sandbox. The pydantic stub deliberately reproduces the
  forward-ref crash; the httpx stub is steerable via `NEXT_ERROR` / `NEXT_JSON`.
- **Never assert against a fixed byte window of source.** Windows silently stop
  covering a function as soon as the code grows and then report a present fix as
  missing. Use the `_function_body()` helper (in `test_contract_audit.py` and
  `test_channel_fresh_connection.py`), which slices to the next top-level `def`.
- Mutation-check important fixes: revert the fix, confirm a test fails, restore.
  Several "passing" tests in this repo were found to assert nothing that way.
