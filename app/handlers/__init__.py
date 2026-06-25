from aiogram import Router

from app.handlers import menu, start, tasks


def build_router() -> Router:
    r = Router(name="owner")
    r.include_router(start.router)
    r.include_router(menu.router)
    r.include_router(tasks.router)
    return r
