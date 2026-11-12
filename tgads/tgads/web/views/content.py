from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from ...db import session
from ...models import Account, AdFeedback, AiJob, Channel, ContentItem, Donor, MyChannel, MyCreative
from ...services import content, tasks
from ..app import back, render, to_utc

router = APIRouter(prefix="/content")


@router.get("")
async def index(request: Request):
    async with session() as s:
        chans = (await s.scalars(select(MyChannel).order_by(MyChannel.id))).all()
        accounts = (await s.scalars(select(Account).where(Account.status == "active"))).all()
        # starter posts that still wait for the launch, and ad posts, per channel
        waiting = dict((await s.execute(select(ContentItem.my_channel_id, func.count()).where(
            ContentItem.source == "starter", ContentItem.status.in_(("draft", "failed")))
            .group_by(ContentItem.my_channel_id))).all())
        posted = dict((await s.execute(select(ContentItem.my_channel_id, func.count()).where(
            ContentItem.status == "published").group_by(ContentItem.my_channel_id))).all())
        ads = dict((await s.execute(select(MyCreative.my_channel_id, func.count())
                                    .group_by(MyCreative.my_channel_id))).all())
    pending = [m for m in chans if not m.is_live or waiting.get(m.id)]
    return render(request, "content_index.html", chans=chans, accounts=accounts, waiting=waiting, posted=posted,
                  ads=ads, pending=pending)


@router.post("/import")
async def import_packs(file: UploadFile = File(...)):
    """Ready channels from a JSON file: one pack or a list of packs (see content.import_pack)."""
    try:
        data = json.loads((await file.read()).decode("utf-8-sig"))
        packs = data if isinstance(data, list) else [data]
        for pack in packs:
            await content.import_pack(pack)
    except Exception as e:
        return back("/content", err=f"Пакет не загружен: {e}"[:300])
    return back("/content", msg=f"Загружено каналов: {len(packs)}. Проверьте их и создайте в Telegram.")


@router.post("/launch-pending")
async def launch_pending(account_id: int = Form(0)):
    """Create every draft channel in Telegram, one after another (a background task)."""
    if account_id:
        async with session() as s:
            for m in (await s.scalars(select(MyChannel))).all():
                if not m.is_live:
                    m.publisher_account_id = account_id
            await s.commit()
    tasks.spawn(content.launch_pending(), "Создание своих каналов в Telegram")
    return back("/content", msg="Создание каналов запущено: по одному, с паузами. Ход виден на главной странице.")


@router.post("/public-channels")
async def public_channels(account_id: int = Form(...)):
    tasks.spawn(content.public_channels(account_id), "Публичные каналы аккаунта")
    return back("/content", msg="Смотрю публичные каналы аккаунта: итог на главной странице")


@router.post("/{mc_id}/go-public")
async def go_public(mc_id: int, drop: str = Form("")):
    tasks.spawn(content.go_public(mc_id, drop), "Публичный адрес канала")
    return back(f"/content/{mc_id}", msg="Ставлю публичный адрес: итог на главной странице")


@router.post("/{mc_id}/republish")
async def republish(mc_id: int):
    tasks.spawn(content.republish_starters(mc_id), "Перепубликация стартовых постов")
    return back(f"/content/{mc_id}", msg="Публикую стартовые посты заново: итог на главной странице")


@router.post("/{mc_id}/nopreview")
async def nopreview(mc_id: int):
    tasks.spawn(content.strip_previews(mc_id), "Убрать предпросмотр ссылок в постах")
    return back(f"/content/{mc_id}", msg="Убираю предпросмотр ссылок в опубликованных постах")


@router.get("/taste")
async def taste(request: Request):
    """The owner's marks of ad posts: what is remembered and goes into the prompts of new ad posts."""
    async with session() as s:
        rows = (await s.scalars(select(AdFeedback).order_by(AdFeedback.id.desc()).limit(300))).all()
        chans = {m.id: m for m in (await s.scalars(select(MyChannel))).all()}
    return render(request, "taste.html", rows=rows, chans=chans)


@router.post("/taste/{fb_id}/delete")
async def taste_delete(fb_id: int):
    async with session() as s:
        fb = await s.get(AdFeedback, fb_id)
        if fb:
            await s.delete(fb)
            await s.commit()
    return back("/content/taste", msg="Оценка удалена из памяти")


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
        # starter posts are read in the order they will be published; other drafts — newest first
        items.sort(key=lambda it: (it.status, it.source != "starter", it.id if it.source == "starter" else -it.id))
        creatives = (await s.scalars(select(MyCreative).where(MyCreative.my_channel_id == mc_id)
                                     .order_by(MyCreative.id.desc()))).all()
        accounts = (await s.scalars(select(Account).where(Account.status == "active"))).all()
        no_ru = sum(len(rows) for rows in await content.untranslated(s, mc_id))
        # replacements ordered after a dislike, by the id of the ad post
        waiting = {c.replace_job_id: c.id for c in creatives if c.replace_job_id}
        replace_jobs = {waiting[j.id]: j for j in (await s.scalars(select(AiJob).where(
            AiJob.id.in_(waiting)))).all()} if waiting else {}
        marks = await s.scalar(select(func.count()).select_from(AdFeedback))
    return render(request, "content.html", mc=mc, donors=donors, top=top, donor_names=donor_names, items=items,
                  creatives=creatives, accounts=accounts, days=days, untranslated=no_ru, replace_jobs=replace_jobs,
                  marks=marks)


@router.post("/{mc_id}/settings")
async def save_settings(mc_id: int, ref: str = Form(""), language: str = Form("uz"), topic: str = Form(""),
                        positioning: str = Form(""), post_times: str = Form(""), publisher_account_id: int = Form(0),
                        title: str = Form(""), about: str = Form(""), username_wanted: str = Form(""),
                        signature: str = Form("")):
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        was_draft = not mc.is_live
        if mc.ref != ref.strip():
            mc.tg_id = None
        mc.ref, mc.language, mc.topic, mc.positioning = ref.strip(), language, topic, positioning
        mc.post_times = post_times
        mc.publisher_account_id = publisher_account_id or None
        mc.signature = signature.strip() or None
        if was_draft:  # what the channel will be created with
            mc.title = title.strip() or mc.title
            mc.about = about.replace("\r\n", "\n").strip() or None
            mc.username_wanted = username_wanted.strip() or None
        live = mc.is_live
        await s.commit()
    if live:
        tasks.spawn(content.refresh_my_channel(mc_id), "Проверка своего канала")
    return back(f"/content/{mc_id}", msg="Сохранено")


@router.post("/{mc_id}/launch")
async def launch(mc_id: int):
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        title = mc.title
    tasks.spawn(content.launch(mc_id), f"Создание канала «{title}» в Telegram")
    return back(f"/content/{mc_id}", msg="Создание запущено: канал, адрес, аватар и стартовые посты. "
                                         "Ход виден на главной странице, это займёт несколько минут.")


@router.post("/{mc_id}/translate")
async def translate(mc_id: int):
    """Russian translations for the owner to read (they are never published): order the missing ones from the AI."""
    n = await content.translate_missing(mc_id)
    if not n:
        return back(f"/content/{mc_id}", msg="Заказывать нечего: переводы уже есть или ждут в очереди ИИ.")
    return back(f"/content/{mc_id}", msg=f"В очередь ИИ поставлено переводов: {n}. Готовые появятся на этой "
                                         "странице, ход виден на странице «ИИ».")


@router.get("/{mc_id}/avatar")
async def avatar(mc_id: int):
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
    if mc is None or not mc.avatar_path or not Path(mc.avatar_path).is_file():
        raise HTTPException(404)
    return FileResponse(mc.avatar_path)


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
        s.add(ContentItem(my_channel_id=mc_id, source="manual", text=text.replace("\r\n", "\n")))
        await s.commit()
    return back(f"/content/{mc_id}", msg="Черновик добавлен")


@router.post("/items/{item_id}")
async def item_action(item_id: int, action: str = Form(...), text: str = Form(""), when: str = Form("")):
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        mc_id = item.my_channel_id
        text = text.replace("\r\n", "\n")  # a browser sends the lines of a textarea with CRLF
        if text and text != item.text:
            item.text, item.text_ru = text, None  # the translation was of the old text
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
        s.add(MyCreative(my_channel_id=mc_id, title=title, text=text.replace("\r\n", "\n")))
        await s.commit()
    return back(f"/content/{mc_id}#creatives", msg="Рекламный пост сохранён")


@router.post("/creatives/{cr_id}/rate")
async def rate_creative(cr_id: int, rating: str = Form(...), comment: str = Form("")):
    """The owner's like or dislike with a comment; a dislike orders a new text instead of this one."""
    async with session() as s:
        cr = await s.get(MyCreative, cr_id)
        mc_id = cr.my_channel_id
    job_id = await content.rate_creative(cr_id, rating == "like", comment)
    msg = ("Принято: пишется замена с учётом вашего комментария. Она появится на месте этого поста."
           if job_id else "Оценка сохранена: она учтётся в следующих рекламных постах.")
    return back(f"/content/{mc_id}#creatives", msg=msg)


@router.post("/creatives/{cr_id}/delete")
async def del_creative(cr_id: int):
    async with session() as s:
        cr = await s.get(MyCreative, cr_id)
        mc_id = cr.my_channel_id
        await s.delete(cr)
        await s.commit()
    return back(f"/content/{mc_id}#creatives")
