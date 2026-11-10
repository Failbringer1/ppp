"""Module 2: donor posts, best-post selection, AI rewrite/generation, scheduled publishing."""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, errors, functions, types

from ..ai import prompts
from ..ai.jobs import enqueue, handler
from ..analysis.posts import ScoredPost, score_posts
from ..config import get_config
from ..db import session
from ..models import AiJob, Channel, ContentItem, Donor, MyChannel, MyCreative, Post, utcnow
from ..tg.pool import TASHKENT, FloodWait, call, pool
from .research import add_channel_ref, collect_channel, resolve_entity

log = logging.getLogger(__name__)


# ----- my channels -----

async def resolve_my_channel(client: TelegramClient, mc: MyChannel):
    if mc.tg_id:
        try:
            return await client.get_input_entity(types.PeerChannel(mc.tg_id))
        except (ValueError, TypeError):
            pass
    ref = mc.ref.strip()
    if ref.lstrip("-").isdigit():
        return await client.get_input_entity(int(ref))
    return await call(lambda: client.get_input_entity(ref))


async def refresh_my_channel(mc_id: int) -> str:
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
    if not mc.is_live:
        return "Черновик: канал ещё не создан в Telegram"
    acc_id, client = await pool.pick("publisher", prefer=mc.publisher_account_id)
    entity = await client.get_entity(await resolve_my_channel(client, mc))
    full = await call(lambda: client(functions.channels.GetFullChannelRequest(channel=entity)))
    subs = full.full_chat.participants_count
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        mc.tg_id, mc.title, mc.subscribers = entity.id, entity.title, subs
        mc.publisher_account_id = mc.publisher_account_id or acc_id
        await s.commit()
    return f"{entity.title}: {subs} подписчиков"


def channel_info(mc: MyChannel) -> dict:
    return {"title": mc.title, "ref": mc.ref, "language": mc.language, "topic": mc.topic,
            "positioning": mc.positioning}


# ----- donors -----

async def add_donors(mc_id: int, refs: str) -> int:
    n = 0
    async with session() as s:
        for r in refs.replace(",", "\n").split():
            ch = await add_channel_ref(s, r, "donor", status="queued")
            if ch is None:
                continue
            exists = (await s.scalars(select(Donor).where(Donor.my_channel_id == mc_id,
                                                          Donor.channel_id == ch.id))).first()
            if not exists:
                s.add(Donor(my_channel_id=mc_id, channel_id=ch.id))
                n += 1
        await s.commit()
    return n


async def refresh_donors(mc_id: int, limit: int = 100) -> str:
    async with session() as s:
        ids = (await s.scalars(select(Donor.channel_id).where(Donor.my_channel_id == mc_id))).all()
    ok = 0
    for cid in ids:
        try:
            await collect_channel(cid, limit=limit)
            ok += 1
        except FloodWait as e:
            return f"Обновлено {ok}/{len(ids)}, остановлено: FloodWait {e.seconds} с"
        await asyncio.sleep(3)
    return f"Обновлено доноров: {ok}/{len(ids)}"


async def top_posts(s: AsyncSession, mc_id: int, days: int = 30, limit: int = 60) -> list[tuple[Post, float]]:
    donor_ids = (await s.scalars(select(Donor.channel_id).where(Donor.my_channel_id == mc_id))).all()
    used = set((await s.scalars(select(ContentItem.source_post_id).where(
        ContentItem.my_channel_id == mc_id, ContentItem.source_post_id.is_not(None)))).all())
    used_hashes = set((await s.scalars(select(Post.text_hash).where(Post.id.in_(used)))).all()) if used else set()
    since = utcnow() - timedelta(days=days)
    out: list[tuple[Post, float]] = []
    for cid in donor_ids:
        posts = (await s.scalars(select(Post).where(Post.channel_id == cid, Post.date >= since,
                                                    Post.is_ad.is_(False)))).all()
        now = utcnow()
        scored = score_posts([ScoredPost(p, p.views, p.forwards, p.reactions,
                                         (now - p.date).total_seconds() / 3600) for p in posts])
        out += [(sp.key, sp.score) for sp in scored
                if sp.key.id not in used and (not sp.key.text_hash or sp.key.text_hash not in used_hashes)]
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:limit]


# ----- drafts -----

def strip_source(text: str, source_username: str | None) -> str:
    """Remove the donor's own mentions/links (signature) from a copied post."""
    if not text:
        return ""
    if source_username:
        text = re.sub(r"(?im)^.*(@" + re.escape(source_username) + r"|t\.me/" + re.escape(source_username) +
                      r")\b.*$", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def _download_media(post: Post) -> list[str]:
    if not post.media_type or post.media_type in ("poll",):
        return []
    async with session() as s:
        ch = await s.get(Channel, post.channel_id)
    acc_id, client = await pool.pick("monitor", prefer=ch.monitor_account_id)
    entity = await resolve_entity(client, ch)
    if post.grouped_id:
        ids = list(range(post.msg_id - 9, post.msg_id + 10))
        msgs = [m for m in await call(lambda: client.get_messages(entity, ids=ids))
                if m is not None and m.grouped_id == post.grouped_id]
    else:
        msgs = [await call(lambda: client.get_messages(entity, ids=post.msg_id))]
    folder = get_config().media_dir
    paths = []
    for m in sorted((m for m in msgs if m and m.media), key=lambda m: m.id):
        p = await client.download_media(m, file=str(folder / f"p{post.id}_{m.id}"))
        if p:
            paths.append(p)
    return paths


async def make_draft(mc_id: int, post_id: int, mode: str) -> int:
    """mode: copy (as is, minus donor signature) | rewrite (AI)."""
    async with session() as s:
        post = await s.get(Post, post_id)
        ch = await s.get(Channel, post.channel_id)
        mc = await s.get(MyChannel, mc_id)
        uname = ch.username
    try:
        paths, err = await _download_media(post), None
    except Exception as e:
        paths, err = [], f"Медиа не скачалось: {e}"[:500]
    async with session() as s:
        item = ContentItem(my_channel_id=mc_id, source=mode, source_post_id=post_id, media_paths=paths,
                           text=strip_source(post.text, uname), error=err,
                           status="generating" if mode == "rewrite" else "draft")
        s.add(item)
        await s.flush()
        if mode == "rewrite":
            await enqueue(s, "rewrite_post", f"Рерайт поста {ch.ref}/{post.msg_id}",
                          prompts.rewrite_post(post.text, channel_info(mc)), ref_id=item.id)
        await s.commit()
        return item.id


async def generate(mc_id: int, hint: str) -> int:
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        examples = [p.text for p, _ in (await top_posts(s, mc_id, limit=5)) if p.text]
        item = ContentItem(my_channel_id=mc_id, source="ai", status="generating", text="")
        s.add(item)
        await s.flush()
        await enqueue(s, "generate_post", f"Пост для {mc.title or mc.ref}: {hint[:60]}",
                      prompts.generate_post(channel_info(mc), hint, examples), ref_id=item.id)
        await s.commit()
        return item.id


@handler("rewrite_post")
@handler("generate_post")
async def _apply_text(s: AsyncSession, job: AiJob, text: str) -> None:
    item = await s.get(ContentItem, job.ref_id)
    if item:
        item.text = text.strip()
        if item.status == "generating":
            item.status = "draft"


# ----- ready channels: a pack with starter content, creating the channel in Telegram -----

STARTER_PAUSE = (20, 40)     # seconds between starter posts
CHANNEL_PAUSE = (180, 300)   # seconds between channels when several are created in a row


def post_text(mc: MyChannel, text: str) -> str:
    """The post as it goes out: with the channel signature ({channel} = @username or invite link)."""
    sig = (mc.signature or "").strip()
    if not sig or ("{channel}" in sig and not mc.handle):
        return text
    sig = sig.replace("{channel}", mc.handle)
    return text if sig in text else f"{text.rstrip()}\n\n{sig}"


async def import_pack(data: dict) -> int:
    """A ready channel from a JSON pack: the draft, its starter posts and its ad posts.

    Importing a pack with the same title again replaces the starter posts while none of them is published,
    so the texts can be corrected before the launch; ad posts are updated by their titles.
    The avatar comes as a path on this machine (avatar_path) or inside the pack (avatar_b64)."""
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("В пакете нет названия канала")
    async with session() as s:
        mc = (await s.scalars(select(MyChannel).where(MyChannel.title == title))).first()
        if mc is None:
            mc = MyChannel(ref="", title=title)
            s.add(mc)
        for k in ("language", "topic", "positioning", "about", "signature", "post_times", "avatar_path"):
            if data.get(k) is not None:
                setattr(mc, k, data[k])
        if data.get("usernames"):
            mc.username_wanted = ",".join(u.strip().lstrip("@") for u in data["usernames"] if u.strip())
        await s.flush()
        if data.get("avatar_b64"):
            folder = get_config().media_dir / "avatars"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"my_{mc.id}.jpg"
            path.write_bytes(base64.b64decode(data["avatar_b64"]))
            mc.avatar_path = str(path.resolve())
        starters = (await s.scalars(select(ContentItem).where(ContentItem.my_channel_id == mc.id,
                                                              ContentItem.source == "starter"))).all()
        if not any(it.status == "published" for it in starters):
            await s.execute(delete(ContentItem).where(ContentItem.id.in_([it.id for it in starters])))
            for p in data.get("posts") or []:
                s.add(ContentItem(my_channel_id=mc.id, source="starter", text=p["text"].strip(), note=p.get("ru"),
                                  pin=True if p.get("pin") else None))
                await s.flush()  # keep the order of the pack
        old = {c.title: c for c in (await s.scalars(select(MyCreative).where(
            MyCreative.my_channel_id == mc.id, MyCreative.source == "pack"))).all()}
        for a in data.get("ads") or []:
            cr = old.get(a.get("title", ""))
            if cr is None:
                cr = MyCreative(my_channel_id=mc.id, title=a.get("title", ""), text="", source="pack")
                s.add(cr)
            cr.text, cr.note = a["text"].strip(), a.get("ru")
            await s.flush()
        await s.commit()
        return mc.id


def _usernames(mc: MyChannel) -> list[str]:
    return [u.strip().lstrip("@") for u in (mc.username_wanted or "").split(",") if u.strip()]


async def _take_username(client: TelegramClient, chat, names: list[str]) -> tuple[str | None, str]:
    """Try the wanted public @usernames in order. Returns (username or None, why there is none)."""
    why = "адрес не задан"
    for name in names:
        try:
            free = await call(lambda: client(functions.channels.CheckUsernameRequest(channel=chat, username=name)))
            if not free:
                why = f"@{name} занят"
                continue
            await call(lambda: client(functions.channels.UpdateUsernameRequest(channel=chat, username=name)))
            return name, ""
        except FloodWait:
            raise
        except errors.ChannelsAdminPublicTooMuchError:
            return None, "у аккаунта слишком много публичных каналов"
        except Exception as e:
            why = f"@{name}: {e}"[:200]
    return None, why


async def _drop_service_messages(client: TelegramClient, entity) -> None:
    """A visitor must see posts, not the lines "channel created", "photo updated", "pinned a message"."""
    try:
        msgs = await client.get_messages(entity, limit=20)
        ids = [m.id for m in msgs if isinstance(m, types.MessageService)]
        if ids:
            await client.delete_messages(entity, ids)
    except Exception as e:
        log.debug("service messages not removed: %s", e)


async def _publish_starters(mc_id: int) -> int:
    async with session() as s:
        ids = (await s.scalars(select(ContentItem.id).where(
            ContentItem.my_channel_id == mc_id, ContentItem.source == "starter",
            ContentItem.status.in_(("draft", "failed"))).order_by(ContentItem.id))).all()
    for i, item_id in enumerate(ids):
        if i:
            await asyncio.sleep(random.uniform(*STARTER_PAUSE))
        await publish(item_id)
        async with session() as s:
            item = await s.get(ContentItem, item_id)
            if item.status != "published":
                raise RuntimeError(f"Пост #{item_id} не опубликован: {item.error}")
    return len(ids)


async def launch(mc_id: int) -> str:
    """Create a draft channel in Telegram (title, description, @username, avatar) and publish its starter posts.

    Safe to run again after a failure: what is already done is not repeated."""
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        title, about, names, avatar = mc.title, mc.about or "", _usernames(mc), mc.avatar_path
        live, prefer = mc.is_live, mc.publisher_account_id
    acc_id, client = await pool.pick("publisher", prefer=prefer)
    notes: list[str] = []
    try:
        if not live:
            upd = await call(lambda: client(functions.channels.CreateChannelRequest(
                title=title[:128], about=about[:255], broadcast=True)))
            chat = upd.chats[0]
            async with session() as s:  # remembered at once: a failure below must not create the channel twice
                mc = await s.get(MyChannel, mc_id)
                mc.tg_id, mc.ref, mc.publisher_account_id = chat.id, f"-100{chat.id}", acc_id
                await s.commit()
            username, why = await _take_username(client, chat, names)
            link = None
            if username is None:
                inv = await call(lambda: client(functions.messages.ExportChatInviteRequest(peer=chat)))
                link = inv.link
                notes.append(f"канал закрытый: {why}")
            if avatar and Path(avatar).is_file():
                try:
                    photo = await client.upload_file(avatar)
                    await call(lambda: client(functions.channels.EditPhotoRequest(
                        channel=chat, photo=types.InputChatUploadedPhoto(file=photo))))
                except FloodWait:
                    raise
                except Exception as e:
                    notes.append(f"аватар не поставлен: {e}"[:150])
            async with session() as s:
                mc = await s.get(MyChannel, mc_id)
                if username:
                    mc.ref = "@" + username
                mc.invite_link = link
                await s.commit()
            await _drop_service_messages(client, chat)
        published = await _publish_starters(mc_id)
    except FloodWait as e:
        await pool.set_cooldown(acc_id, e.seconds)
        raise RuntimeError(f"Telegram просит подождать {e.seconds // 60 + 1} мин. Запустите создание ещё раз "
                           "позже: уже сделанное не повторится.") from e
    async with session() as s:
        mc = await s.get(MyChannel, mc_id)
        where = mc.handle or mc.ref
    return f"{title}: {where}, опубликовано постов: {published}" + "".join(f"; {n}" for n in notes)


async def launch_pending() -> str:
    """Create every draft channel one after another, with pauses; stop at the first failure (most likely a
    flood wait) so Telegram is not hammered with the rest."""
    async with session() as s:
        chans = (await s.scalars(select(MyChannel).order_by(MyChannel.id))).all()
        waiting = set((await s.scalars(select(ContentItem.my_channel_id).where(
            ContentItem.source == "starter", ContentItem.status.in_(("draft", "failed"))))).all())
        ids = [m.id for m in chans if not m.is_live or m.id in waiting]
    out: list[str] = []
    done = 0
    for i, mc_id in enumerate(ids):
        if i:
            await asyncio.sleep(random.uniform(*CHANNEL_PAUSE))
        try:
            out.append(await launch(mc_id))
            done += 1
        except Exception as e:
            log.exception("launch of my channel %s", mc_id)
            out.append(f"канал #{mc_id}: {e}")
            break
    return f"Готово каналов: {done} из {len(ids)}. " + " | ".join(out)


# ----- schedule & publish -----

def _slots(mc: MyChannel) -> list[tuple[int, int]]:
    out = []
    for t in (mc.post_times or "").split(","):
        t = t.strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", t):
            h, m = t.split(":")
            out.append((int(h), int(m)))
    return sorted(out) or [(9, 0), (13, 0), (19, 0)]


async def next_free_slot(s: AsyncSession, mc: MyChannel) -> datetime:
    taken = set((await s.scalars(select(ContentItem.scheduled_at).where(
        ContentItem.my_channel_id == mc.id, ContentItem.status == "queued"))).all())
    now_local = datetime.now(TASHKENT)
    for day in range(0, 60):
        d = (now_local + timedelta(days=day)).date()
        for h, m in _slots(mc):
            local = datetime(d.year, d.month, d.day, h, m, tzinfo=TASHKENT)
            if local <= now_local + timedelta(minutes=2):
                continue
            naive = local.astimezone(timezone.utc).replace(tzinfo=None)
            if naive not in taken:
                return naive
    return utcnow() + timedelta(hours=1)


async def schedule(item_id: int, when_utc: datetime | None = None) -> datetime:
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        mc = await s.get(MyChannel, item.my_channel_id)
        item.scheduled_at = when_utc or await next_free_slot(s, mc)
        item.status = "queued"
        item.error = None
        await s.commit()
        return item.scheduled_at


async def publish(item_id: int) -> None:
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        mc = await s.get(MyChannel, item.my_channel_id)
        text, paths, pin = post_text(mc, item.text), list(item.media_paths or []), bool(item.pin)
    try:
        if not mc.is_live:
            raise RuntimeError("Канал ещё не создан в Telegram")
        acc_id, client = await pool.pick("publisher", prefer=mc.publisher_account_id)
        entity = await resolve_my_channel(client, mc)
        if paths:
            caption = text if len(text) <= 1024 else ""
            sent = await client.send_file(entity, paths if len(paths) > 1 else paths[0], caption=caption)
            if not caption and text:
                sent = await client.send_message(entity, text)
        else:
            sent = await client.send_message(entity, text, link_preview=True)
        msg_id = sent[0].id if isinstance(sent, list) else sent.id
        status, err = "published", None
        if pin:
            try:
                await client.pin_message(entity, msg_id, notify=False)
                await _drop_service_messages(client, entity)
            except Exception as e:
                log.info("pin of item %s failed: %s", item_id, e)
    except Exception as e:
        status, err, msg_id = "failed", str(e)[:500], None
    async with session() as s:
        item = await s.get(ContentItem, item_id)
        item.status, item.error, item.tg_msg_id = status, err, msg_id
        if status == "published":
            item.published_at = utcnow()
        await s.commit()


async def publisher_loop() -> None:
    while True:
        try:
            async with session() as s:
                due = (await s.scalars(select(ContentItem.id).where(ContentItem.status == "queued",
                                                                    ContentItem.scheduled_at <= utcnow()))).all()
            for item_id in due:
                await publish(item_id)
        except Exception:
            log.exception("publisher loop")
        await asyncio.sleep(20)

