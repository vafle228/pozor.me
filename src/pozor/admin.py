"""Compatibility entry point. Prefer `pozor admin` or `pozor serve`."""
from pozor.infra.sqlite.admin_repo import AdminData
from pozor.presentation.admin.server import create_app

__all__ = ['AdminData','create_app']


def main() -> None:
    import asyncio

    from pozor.app.runtime import serve
    from pozor.config import Settings
    asyncio.run(serve(Settings(),admin_only=True))


if __name__ == '__main__':
    main()
