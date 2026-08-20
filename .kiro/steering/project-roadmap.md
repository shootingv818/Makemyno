# Makemyno — Project Roadmap & Working Agreement

Multi-tenant Rubika/Telegram bulk-messaging SaaS. One shared customer bot + one
owner panel + a fleet of remote worker nodes.

- Repo: `github.com/shootingv818/Makemyno`
- Working branch: `feat/multi-tenant-foundation` (PR #1 open against `main`)
- Tests: 997 passing, 12 skipped

## وضعیت فعلی (خلاصه برای مالک)

بخش‌های ۱ تا ۶ ساخته شده. باگ چند ساعته‌ی «ساخت کانال INVALID_AUTH» ریشه‌یابی و
حل شد: مشکل از کانال نبود، از **جای فایل سشن** بود — سشن روی یک سرور بود و کار
روی سرور دیگری اجرا می‌شد، و rubpy در این حالت خطا نمی‌داد بلکه **بدون احراز
هویت** وصل می‌شد. نتیجه: مخاطبین صفر + INVALID_AUTH روی اولین دستور امضادار.

کاری که باید بکنی بعد از هر آپدیت:
1. مستر: `cd /opt/makemyno && sudo bash deploy/makemyno.sh update`
2. ورکرها: دکمه‌ی **⬆️ آپدیت** در پنل هر ورکر (ورکر کد خودش را دارد و با آپدیت
   مستر آپدیت نمی‌شود).

## THE RULE THAT MATTERS MOST

**Read the reference projects and port their code. Do not reinvent.**

Read-only references live at:
- `/projects/sandbox/Makiioo` — the primary reference. Single-tenant ancestor of
  this project. Its `project_notes/` folder documents decisions.
- `/projects/sandbox/Haopooonwkkoo` — secondary reference.

The owner has said this many times, and every time a bug was chased for hours the
answer was already implemented in `Makiioo`. Before designing anything, diff the
equivalent function/endpoint there. When behaviour differs, the reference is
right until proven otherwise.

Practical diff recipe (no `diff` binary in the sandbox):

```
cd /projects/sandbox && python - <<'PY'
import difflib
a=open('Makiioo/rubika_client.py').read().splitlines()
b=open('Makemyno/rubika_client.py').read().splitlines()
print("\n".join(difflib.unified_diff(a,b,'Makiioo','Makemyno',lineterm='',n=2)))
PY
```

## Commands

```
# tests (python is not on PATH without pyenv; -p no:cacheprovider avoids a stale cache)
cd /projects/sandbox/Makemyno && pyenv global 3.11.15 \
  && python -m pytest tests/ -q -p no:cacheprovider

# commit / push
git -c user.email=dev@makemyno -c user.name="Makemyno" commit -q -m "..."
git push origin feat/multi-tenant-foundation
```

Production deploy: `cd /opt/makemyno && sudo bash deploy/makemyno.sh update`
(master only). Workers must be rebuilt separately from the panel's ⬆️ آپدیت
button, which rebuilds the Docker image on the remote server.

## Architecture invariants — break these and production breaks

1. **A session is a FILE on ONE machine.** `data/sessions/c<customer_id>/acc_<98…>`.
   `accounts.worker_id` says which machine, and `worker.worker_for_account()`
   routes every job there. If the file and the job are on different servers,
   rubpy connects **unauthenticated** and fails silently-ish (zero contacts, then
   INVALID_AUTH on the first signed call). `session_store.place()` is what keeps
   them together; `session_store.run_with_repair()` heals it at runtime.
2. **One live connection per session.** Two connections on one session is the
   number-one cause of INVALID_AUTH / AUTH_FROM_ANOTHER. `account_conn` keeps one
   warm connection per `(customer, phone)` behind a lock. Signed operations
   (channel create, add members) use `account_conn.fresh_call`, which closes the
   warm socket and runs on a dedicated connection, then disconnects.
3. **Session identity is `(customer_id, phone)`, never phone alone.** Two
   customers may legitimately own the same number.
4. **The busy registry is in memory.** Therefore the health engine runs in the
   **customer** process (the one that owns the jobs). Running it in the owner
   process gives it no view of what is mid-send, and it kills the sessions it is
   meant to protect.
5. **Workers run their own copy of the code** (`COPY . .` in the image). A master
   update does NOT update a worker. The worker detail screen compares
   `worker_code_version` with `master_code_version` and flags a stale worker.
6. **A worker holds no secrets**: no bot token, no owner id, no customer roster,
   no database. Only session files and in-flight jobs.
7. **The process role is argv, not an env var** (`python main.py owner|customer|worker`).
   systemd and compose disagree on env precedence, and relying on that put the
   owner bot on both services.
8. **pydantic request models must be at MODULE level** in `worker_api.py`. Defined
   inside `build_app()` they are forward refs pydantic cannot resolve, and the
   worker crashes at startup with `PydanticUndefinedAnnotation`, which the master
   only sees as "Server disconnected without a response".
9. **Pinned dependency set.** `rubpy==7.3.5`, and the
   httpx/fastapi/starlette/pydantic/uvicorn block is pinned **as a set**.
   `DOCKER_BUILDKIT=0` (buildx is absent on bare servers). Keep
   `-i https://pypi.org/simple` and `--prefer-binary` in the Dockerfile.

## Next steps

1. **Verify on production** that channel creation now works, using an account
   that was logged in before this fix (it should self-heal on the first retry via
   the stored `session_blob`). An account with **no** stored blob (logged in
   before `session_blob` existed) can only be repaired by logging in again — the
   panel should eventually say that explicitly instead of showing an error code.
2. **Audit the remaining warm-connection call sites** for signed operations that
   should use `fresh_call`, and for operations that should be wrapped in
   `session_store.run_with_repair`. Currently repaired: `_channel_flow` (local +
   remote), `_collect_targets` (local + remote). Not yet: `_run_send` /
   `/send/start`, `contacts/add`, group join, tabchi/secretary paths.
3. **Telegram send speed** is still reported as slow. The owner says the reference
   solved it by not re-downloading the media for every chat — port the reference's
   media reuse for the Telegram path and confirm against `telegram_multi_send.py`.
4. **Live provisioning progress**: the owner wants a live log/percentage while a
   worker is being built, instead of SSH-ing in. Partially done
   (`test_provision_progress.py`); the Docker build step still looks hung for
   minutes with no feedback.
5. **Error cards mangle `**kwargs`** — the traceback rendering eats `**` because
   the message is parsed as Markdown. Send error cards with markdown disabled so
   source lines are readable.
6. Offer (pending owner's yes/no): make `makemyno.sh update` print a reminder that
   workers need updating separately.

## Owner's standing preferences

- Wants **live diagnostics in the bot**, not SSH. Log group cards, progress bars,
  a self-test button, worker log button.
- Wants the **real error surfaced**, never a swallowed exception or a cheerful
  "done" when nothing happened. A pool account that reached nobody must say why.
- Persian UI text, English log/card labels, the house card style in `cards.py`.
- Do not add tests unless asked — but the suite exists and must stay green.
