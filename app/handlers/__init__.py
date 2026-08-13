from aiogram import Router

from app.handlers import (
    material_generation,
    materials,
    consent,
    menu,
    profile,
    source_analysis,
    sources,
    start,
    tasks,
    text_review,
)


def build_router() -> Router:
    r = Router(name="owner")
    r.include_router(start.router)
    r.include_router(consent.router)
    r.include_router(menu.router)
    r.include_router(sources.router)
    r.include_router(source_analysis.router)
    r.include_router(material_generation.router)
    r.include_router(text_review.router)
    r.include_router(profile.router)
    r.include_router(materials.router)
    r.include_router(tasks.router)
    return r
