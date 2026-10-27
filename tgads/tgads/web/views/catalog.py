from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from ...analysis.tgstat import CATALOG_CATEGORIES
from ...db import session
from ...models import CatalogChannel
from ...services import catalog
from ..app import back, render

router = APIRouter(prefix="/catalog")
PAGE = 100
SORTS = {
    "subs": CatalogChannel.subscribers.desc().nullslast(),
    "reach": CatalogChannel.avg_reach.desc().nullslast(),
    "err": (CatalogChannel.avg_reach / CatalogChannel.subscribers).desc().nullslast(),
    "ci": CatalogChannel.ci.desc().nullslast(),
    "new": CatalogChannel.created_at.desc(),
}


@router.get("")
async def index(request: Request, category: int = 0, language: int = -1, kind: str = "", q: str = "",
                min_subs: int = 0, max_subs: int = 0, sort: str = "subs", page: int = 1):
    stmt = catalog.filtered(category, language, kind, q, min_subs, max_subs)
    stats = await catalog.breakdown(stmt)
    async with session() as s:
        rows = (await s.scalars(stmt.order_by(SORTS.get(sort, SORTS["subs"]), CatalogChannel.id)
                                .offset((max(page, 1) - 1) * PAGE).limit(PAGE))).all()
        lang_ids = (await s.scalars(select(CatalogChannel.language_id).distinct())).all()
        grand_total = await s.scalar(select(func.count(CatalogChannel.id)))
    languages = sorted(((i, catalog.language_name(i)) for i in lang_ids if i is not None), key=lambda x: x[1])
    params = dict(category=category, language=language, kind=kind, q=q, min_subs=min_subs, max_subs=max_subs,
                  sort=sort)
    total = stats["total"]
    return render(request, "catalog.html", rows=rows, stats=stats, page=page, pages=(total + PAGE - 1) // PAGE,
                  params=params, grand_total=grand_total,
                  categories=sorted(((k, v[0]) for k, v in CATALOG_CATEGORIES.items()), key=lambda x: x[1]),
                  languages=languages, category_name=catalog.category_name, language_name=catalog.language_name)


@router.post("/to-work")
async def to_work(ids: list[int] = Form([]), action: str = Form("work"), back_url: str = Form("/catalog")):
    if not ids:
        return back(back_url, err="Ничего не выбрано")
    n = await catalog.to_work(ids, monitor=action == "monitor")
    what = "на вступление и проверку накрутки" if action == "monitor" else "в очередь сбора"
    return back(back_url, msg=f"Отправлено {what}: {n} (закрытые без @username пропущены)")


@router.post("/top")
async def top(n: int = Form(100), category: int = Form(0), language: int = Form(-1), kind: str = Form(""),
              q: str = Form(""), min_subs: int = Form(0), max_subs: int = Form(0), back_url: str = Form("/catalog")):
    """The n biggest public channels of the current filter -> join and bot check."""
    ids = await catalog.top_ids(catalog.filtered(category, language, kind, q, min_subs, max_subs), max(1, min(n, 2000)))
    done = await catalog.to_work(ids, monitor=True)
    return back("/vetting", msg=f"На вступление поставлено каналов: {done}. Самые крупные идут первыми, "
                                f"в пределах дневного лимита вступлений на аккаунт")


@router.post("/ingest/{key}")
async def ingest(key: str, request: Request):
    """Rows from the in-browser collector. Open without the panel login: the key in the URL is the password."""
    if not await catalog.check_key(key):
        return HTMLResponse("Неверный ключ", status_code=403)
    try:
        data = catalog.parse_payload(await request.body())
        added, updated = await catalog.ingest(data)
    except catalog.IngestError as e:
        return HTMLResponse(f"<p id='result'>Ошибка: {e}</p>", status_code=400)
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><p id='result'>Принято: новых {added}, "
                        f"обновлено {updated}</p><p><a href='/catalog'>Открыть каталог</a></p>")
