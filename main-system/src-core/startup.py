import asyncio


def run_cli() -> None:
    """Run the main CLI entrypoint from `main.py` inside an asyncio loop.

    This function imports `main` lazily to avoid circular imports when
    `main` imports this module for other refactor tasks.
    """
    import faulthandler

    # Native-extension faults (e.g. 0xc0000374 heap corruption) otherwise die
    # silently; faulthandler dumps all thread stacks to stderr, which the
    # boot-core relay persists into the backend log.
    faulthandler.enable()
    try:
        # Import locally to avoid circular import at module import time
        import main as main_mod

        asyncio.run(main_mod.main())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"Startup failed: {exc}")
        raise
