from __future__ import annotations

from fastapi import APIRouter, Form, Request
from sqlalchemy import select

from ...db import session
from ...models import Account, Channel, ContentItem, Donor, MyChannel, MyCreative
from ...services import content, tasks
from ..app import back, render, to_utc

router = APIRouter(prefix="/content")


@router.get("")
async def index(request: Request):
    async with session() as s:
        chans = (await s.scalars(select(MyChannel).order_by(MyChannel.id))).all()
        accounts = (await s.scalars(select(Account).where(Account.status == "active"))).all()
    return render(request, "content_index.html", chans=chans, accounts=accounts)


@router.post("")
async def create(ref: str = Form(...), language: str = Form("uz"), topic: str = Form(""), positioning: str = Form(""),
                 publisher_account_id: int = Form(0)):
    async with session() as s:
        mc = MyChannel(ref=ref.strip(), language=language, topic=topic, positioning=positioning,
                       publisher_account_id=publisher_account_id or None)
        s.add(mc)
        await s.commit()
        mc_id = mc.id
    tasks.spawn(content.refresh_my_channel(mc_id), "Проверка своего канала")
    return back(f"/content/{mc_id}", msg="Канал добавлен")


@router.get("/{mc_id}")
async def channel(request: Request, mc_id: int, days: int = 30):
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        donors = (await s.execute(select(Donor, Channel).join(Channel, Donor.channel_id == Channel.id)
                                  .where(Donor.my_channel_id == mc_id))).all()
        top = await content.top_posts(s, mc_id, days=days)
        donor_names = {c.id: c for _, c in donors}
        items = (await s.scalars(select(ContentItem).where(ContentItem.my_channel_id == mc_id,
                                                           ContentItem.status.not_in(("rejected",)))
                                 .order_by(ContentItem.status, ContentItem.scheduled_at, ContentItem.id.desc())
                                 .limit(100))).all()
        creatives = (await s.scalars(select(MyCreative).where(MyCreative.my_channel_id == mc_id)
                                     .order_by(MyCreative.id.desc()))).all()
        accounts = (await s.scalars(select(Account).where(Account.status == "active"))).all()
    return render(request, "content.html", mc=mc, donors=donors, top=top, donor_names=donor_names, items=items,
                  creatives=creatives, accounts=accounts, days=days)


@router.post("/{mc_id}/settings")
async def save_settings(mc_id: int, ref: str = Form(...), language: str = Form("uz"), topic: str = Form(""),
                        positioning: str = Form(""), post_times: str = Form(""), publisher_account_id: int = Form(0)):
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        if mc.ref != ref.strip():
            mc.tg_id = None
        mc.ref, mc.language, mc.topic, mc.positioning = ref.strip(), language, topic, positioning
        mc.post_times = post_times
        mc.publisher_account_id = publisher_account_id or None
        await s.commit()
    tasks.spawn(content.refresh_my_channel(mc_id), "Проверка своего канала")
    return back(f"/content/{mc_id}", msg="Сохранено")


@router.post("/{mc_id}/donors")
async def add_donors(mc_id: int, refs: str = Form(...)):
    n = await content.add_donors(mc_id, refs)
    return back(f"/content/{mc_id}", msg=f"Добавлено доноров: {n}. Они встали в очередь сбора.")


@router.post("/{mc_id}/donors/{donor_id}/delete")
async def del_donor(mc_id: int, donor_id: int):
    async with session() as s:
        d = await s.get(Donor, donor_id)
        if d:
            await s.delete(d)
            await s.commit()
    return back(f"/content/{mc_id}")


@router.post("/{mc_id}/refresh")
async def refresh(mc_id: int):
    tasks.spawn(content.refresh_donors(mc_id), "Обновление постов доноров")
    return back(f"/content/{mc_id}", msg="Обновление доноров запущено (см. дашборд)")


@router.post("/{mc_id}/take")
async def take(mc_id: int, post_id: int = Form(...), mode: str = Form("rewrite")):
    tasks.spawn(content.make_draft(mc_id, post_id, mode), "Черновик из поста донора")
    return back(f"/content/{mc_id}", msg="Черновик создаётся" + (" (рерайт через ИИ)" if mode == "rewrite" else ""))


@router.post("/{mc_id}/generate")
async def generate(mc_id: int, hint: str = Form("")):
    await content.generate(mc_id, hint)
    return back(f"/content/{mc_id}", msg="Генерация поставлена в очередь ИИ")


@router.post("/{mc_id}/manual")
async def manual(mc_id: int, text: str = Form(...)):
    async with session() as s:
        s.add(ContentItem(my_channel_id=mc_id, source="manual", text=text))
        await s.commit()
    return back(f"/content/{mc_id}", msg="Черновик добавлен")


@router.post("/items/{item_id}")
async def item_action(item_id: int, action: str = Form(...), text: str = Form(""), when: str = Form("")):
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        mc_id = item.my_channel_id
        if text:
            item.text = text
        if action == "reject":
            item.status = "rejected"
        elif action == "unschedule":
            item.status, item.scheduled_at = "draft", None
        await s.commit()
    if action == "schedule":
        await content.schedule(item_id, to_utc(when))
        return back(f"/content/{mc_id}", msg="В очереди публикации")
    if action == "now":
        await content.publish(item_id)
        return back(f"/content/{mc_id}", msg="Отправлено")
    return back(f"/content/{mc_id}", msg="Сохранено")


@router.post("/{mc_id}/creatives")
async def add_creative(mc_id: int, title: str = Form(""), text: str = Form(...)):
    async with session() as s:
        s.add(MyCreative(my_channel_id=mc_id, title=title, text=text))
        await s.commit()
    return back(f"/content/{mc_id}#creatives", msg="Рекламный пост сохранён")


@router.post("/creatives/{cr_id}/delete")
async def del_creative(cr_id: int):
    async with session() as s:
        cr = await s.get(MyCreative, cr_id)
        mc_id = cr.my_channel_id
        await s.delete(cr)
        await s.commit()
    return back(f"/content/{mc_id}#creatives")
