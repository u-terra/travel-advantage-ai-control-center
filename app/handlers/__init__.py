from aiogram import Router

from app.handlers import material_generation, menu, source_analysis, start, tasks


def build_router() -> Router:
    r = Router(name="owner")
    r.include_router(start.router)
    r.include_router(menu.router)
    r.include_router(source_analysis.router)
    r.include_router(material_generation.router)
    r.include_router(tasks.router)
    return r
