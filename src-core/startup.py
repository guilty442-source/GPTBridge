import asyncio


def run_cli() -> None:
    """Run the main CLI entrypoint from `main.py` inside an asyncio loop.

    This function imports `main` lazily to avoid circular imports when
    `main` imports this module for other refactor tasks.
    """
    try:
        # Import locally to avoid circular import at module import time
        import main as main_mod

        asyncio.run(main_mod.main())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"Startup failed: {exc}")
        raise
