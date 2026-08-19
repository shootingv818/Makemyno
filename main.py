"""
Unified entrypoint — one codebase, three roles.

    python main.py owner      the central panel (only the owner talks to it)
    python main.py customer   the shared customer bot
    python main.py worker     a headless sending node

The role may also come from MODE in the environment, but THE ARGUMENT WINS.

WHY THE ARGUMENT WINS
---------------------
This mattered in production. Both bots were started by systemd units that set
`Environment=MODE=owner` and `Environment=MODE=customer`, with an
`EnvironmentFile=` pointing at the shared .env — which happened to contain
`MODE=owner`.

systemd documents `EnvironmentFile=` as overriding `Environment=`. So the file
won, both units started the OWNER bot, and the symptoms were baffling in three
different directions at once:

  * two processes polled the same owner token, so /start on the owner bot
    answered with TWO dashboards
  * nothing was running the customer role, so /start on the customer bot did
    nothing at all
  * the log group still filled up, because the owner bot was perfectly healthy

One misplaced precedence rule, three unrelated-looking bugs. An argv value cannot
be overridden by a file that somebody edits six months from now, so the role is
passed explicitly and the environment is only a fallback.

A worker never holds a bot token, a customer id, or a database. It only executes
work for a session it was handed.
"""
import sys

import config

ROLES = ("owner", "customer", "worker")


def resolve_role(argv=None) -> str:
    """The role to run: first positional argument, else MODE, else owner."""
    args = [a for a in (argv if argv is not None else sys.argv[1:])
            if not a.startswith("-")]
    if args:
        return args[0].strip().lower()
    return (config.MODE or "owner").strip().lower()


def main(argv=None) -> None:
    role = resolve_role(argv)

    if role not in ROLES:
        source = "آرگومان" if (argv or sys.argv[1:]) else "MODE در محیط"
        raise SystemExit(
            f"نقش «{role}» معتبر نیست (از {source} خوانده شد).\n"
            f"یکی از اینها را بده: {', '.join(ROLES)}\n"
            f"مثال:  python main.py customer")

    # Before anything connects: one process per role. Two processes on one token
    # answer every update twice, and the second connection on a session is what
    # revokes it.
    import single_instance
    try:
        single_instance.claim(role)
    except single_instance.AlreadyRunning as exc:
        raise SystemExit(str(exc)) from None

    print(f"starting role: {role}")

    if role == "worker":
        import worker_api
        worker_api.run()
        return

    import asyncio

    if role == "customer":
        import customer_bot
        asyncio.run(customer_bot.amain())
        return

    import owner_bot
    asyncio.run(owner_bot.amain())


if __name__ == "__main__":
    main()
