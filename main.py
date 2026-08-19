"""
Unified entrypoint — one codebase, three roles selected by MODE.

    MODE=owner     -> owner_bot.py     the central panel (only the owner)
    MODE=customer  -> customer_bot.py  the shared customer bot
    MODE=worker    -> worker_api.py    a headless sending node

The owner bot and the customer bot are separate processes with separate tokens
and they are started separately:

    MODE=owner    python main.py
    MODE=customer python main.py

Workers are provisioned automatically by the owner panel over SSH + Docker and
run with MODE=worker. A worker never holds a bot token, a customer id or a
database — it only executes work for a session it was handed.
"""
import config


def main() -> None:
    mode = (config.MODE or "owner").strip().lower()

    if mode == "worker":
        import worker_api
        worker_api.run()
        return

    import asyncio

    if mode == "customer":
        import customer_bot
        asyncio.run(customer_bot.amain())
        return

    if mode == "owner":
        import owner_bot
        asyncio.run(owner_bot.amain())
        return

    raise SystemExit(
        f"MODE='{config.MODE}' is not valid. Use owner, customer or worker.")


if __name__ == "__main__":
    main()
