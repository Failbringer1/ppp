"""Database models. All datetimes are naive UTC."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    pass


# Python None -> SQL NULL (plain JSON would store the string 'null')
JSONN = JSON(none_as_null=True)


class Account(Base):
    """A Telegram user account driven through MTProto (Telethon)."""
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    roles: Mapped[str] = mapped_column(String(64), default="monitor,outreach,publisher")
    status: Mapped[str] = mapped_column(String(16), default="new")  # new | code_sent | active | disabled | error
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)
    counters: Mapped[dict] = mapped_column(JSON, default=dict)  # {"2026-10-27": {"joins": 3, "dms": 5}}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def has_role(self, role: str) -> bool:
        return role in (self.roles or "").split(",")


class MyChannel(Base):
    """Our own channel that we fill with content and promote."""
    __tablename__ = "my_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(128))  # @username or invite link
    tg_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(256), default="")
    language: Mapped[str] = mapped_column(String(8), default="uz")  # uz | uz_cyrl | ru
    topic: Mapped[str] = mapped_column(String(64), default="")
    positioning: Mapped[str] = mapped_column(Text, default="")  # what the channel is about, tone (for AI)
    publisher_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    post_times: Mapped[str] = mapped_column(String(128), default="09:00,13:00,19:00")  # Tashkent time
    subscribers: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    donors: Mapped[list[Donor]] = relationship(back_populates="my_channel", cascade="all, delete-orphan")


class Donor(Base):
    __tablename__ = "donors"
    __table_args__ = (UniqueConstraint("my_channel_id", "channel_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    my_channel_id: Mapped[int] = mapped_column(ForeignKey("my_channels.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))

    my_channel: Mapped[MyChannel] = relationship(back_populates="donors")
    channel: Mapped[Channel] = relationship()


class Channel(Base):
    """Any external channel: niche research, donor, ad platform candidate."""
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    invite_hash: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(256), default="")
    about: Mapped[str] = mapped_column(Text, default="")
    pinned_text: Mapped[str] = mapped_column(Text, default="")
    subscribers: Mapped[int | None] = mapped_column(Integer)
    avg_views: Mapped[float | None] = mapped_column(Float)   # median views of posts aged 24-72h
    err: Mapped[float | None] = mapped_column(Float)
    posts_per_day: Mapped[float | None] = mapped_column(Float)
    fwd_per_1k: Mapped[float | None] = mapped_column(Float)
    react_per_1k: Mapped[float | None] = mapped_column(Float)
    ad_share: Mapped[float | None] = mapped_column(Float)
    last_post_at: Mapped[datetime | None] = mapped_column(DateTime)
    topic: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(32), default="manual")  # search | import | snowball | tgstat | manual
    source_detail: Mapped[str] = mapped_column(String(256), default="")
    # new -> collected; candidate checks: monitoring -> checked; approved / rejected by the user
    status: Mapped[str] = mapped_column(String(16), default="new")
    error: Mapped[str | None] = mapped_column(Text)
    static: Mapped[dict | None] = mapped_column(JSONN)       # StaticMetrics
    tgstat: Mapped[dict | None] = mapped_column(JSONN)       # scraped TGStat numbers
    verdict: Mapped[str] = mapped_column(String(16), default="unknown")
    score: Mapped[float | None] = mapped_column(Float)
    reasons: Mapped[list | None] = mapped_column(JSONN)
    honest_reach: Mapped[float | None] = mapped_column(Float)
    monitor_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    joined: Mapped[bool] = mapped_column(Boolean, default=False)
    last_msg_id: Mapped[int | None] = mapped_column(Integer)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def ref(self) -> str:
        if self.username:
            return "@" + self.username
        if self.invite_hash:
            return "https://t.me/+" + self.invite_hash
        return f"id{self.tg_id}"

    @property
    def link(self) -> str:
        if self.username:
            return f"https://t.me/{self.username}"
        if self.invite_hash:
            return f"https://t.me/+{self.invite_hash}"
        return ""


class ChannelSnapshot(Base):
    __tablename__ = "channel_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    subscribers: Mapped[int | None] = mapped_column(Integer)
    avg_views: Mapped[float | None] = mapped_column(Float)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("channel_id", "msg_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    msg_id: Mapped[int] = mapped_column(Integer)
    date: Mapped[datetime] = mapped_column(DateTime)
    text: Mapped[str] = mapped_column(Text, default="")
    media_type: Mapped[str | None] = mapped_column(String(32))
    grouped_id: Mapped[int | None] = mapped_column(BigInteger)
    views: Mapped[int] = mapped_column(Integer, default=0)
    forwards: Mapped[int] = mapped_column(Integer, default=0)
    reactions: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    urls: Mapped[list] = mapped_column(JSON, default=list)
    is_ad: Mapped[bool] = mapped_column(Boolean, default=False)
    promotes_channel: Mapped[bool] = mapped_column(Boolean, default=False)
    ad_targets: Mapped[list] = mapped_column(JSON, default=list)
    text_hash: Mapped[str] = mapped_column(String(16), default="", index=True)
    creative_id: Mapped[int | None] = mapped_column(ForeignKey("creatives.id"))
    # real-time view tracking (module 3 / module 6)
    tracked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    next_sample_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    analysis: Mapped[dict | None] = mapped_column(JSONN)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    channel: Mapped[Channel] = relationship()
    samples: Mapped[list[ViewSample]] = relationship(order_by="ViewSample.t_sec", cascade="all, delete-orphan")


class ViewSample(Base):
    __tablename__ = "view_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    t_sec: Mapped[int] = mapped_column(Integer)
    views: Mapped[int] = mapped_column(Integer)
    forwards: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Creative(Base):
    """A cluster of identical ad posts placed in several channels."""
    __tablename__ = "creatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    targets: Mapped[list] = mapped_column(JSON, default=list)
    placements: Mapped[int] = mapped_column(Integer, default=0)
    hosts: Mapped[int] = mapped_column(Integer, default=0)
    days_span: Mapped[float] = mapped_column(Float, default=0)
    total_views: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    score: Mapped[float] = mapped_column(Float, default=0)
    topic: Mapped[str] = mapped_column(String(64), default="")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("kind", "value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # username | phone | email | invite
    value: Mapped[str] = mapped_column(String(128))
    entity_type: Mapped[str | None] = mapped_column(String(16))  # user | bot | channel | group | unknown
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    links: Mapped[list[ChannelContact]] = relationship(back_populates="contact", cascade="all, delete-orphan")

    @property
    def label(self) -> str:
        return "@" + self.value if self.kind == "username" else self.value


class ChannelContact(Base):
    __tablename__ = "channel_contacts"
    __table_args__ = (UniqueConstraint("channel_id", "contact_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    ad_context: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="about")  # about | pinned | price_channel
    priority: Mapped[int] = mapped_column(Integer, default=0)

    channel: Mapped[Channel] = relationship()
    contact: Mapped[Contact] = relationship(back_populates="links")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    my_channel_id: Mapped[int | None] = mapped_column(ForeignKey("my_channels.id"))
    channel_ids: Mapped[list] = mapped_column(JSON, default=list)  # channels this talk is about
    # queued -> sent -> replied -> agreed | declined | no_reply ; failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    first_text: Mapped[str] = mapped_column(Text, default="")
    draft_reply: Mapped[str] = mapped_column(Text, default="")
    unread: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    contact: Mapped[Contact] = relationship()
    messages: Mapped[list[Message]] = relationship(order_by="Message.at", cascade="all, delete-orphan")
    offers: Mapped[list[Offer]] = relationship(cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    direction: Mapped[str] = mapped_column(String(4))  # in | out
    text: Mapped[str] = mapped_column(Text, default="")
    tg_msg_id: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"))
    amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))
    price_uzs: Mapped[float | None] = mapped_column(Float)
    fmt: Mapped[str | None] = mapped_column(String(16))
    notes: Mapped[str] = mapped_column(Text, default="")
    expected_views: Mapped[float | None] = mapped_column(Float)
    cpm_uzs: Mapped[float | None] = mapped_column(Float)
    recommendation: Mapped[str] = mapped_column(String(16), default="unknown")
    source: Mapped[str] = mapped_column(String(8), default="regex")  # regex | ai | manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channel: Mapped[Channel | None] = relationship()


class MyCreative(Base):
    """Our own ad post texts (for A/B across purchases)."""
    __tablename__ = "my_creatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    my_channel_id: Mapped[int] = mapped_column(ForeignKey("my_channels.id"), index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | ai
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    my_channel_id: Mapped[int] = mapped_column(ForeignKey("my_channels.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"))
    creative_id: Mapped[int | None] = mapped_column(ForeignKey("my_creatives.id"))
    amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="UZS")
    price_uzs: Mapped[float | None] = mapped_column(Float)
    fmt: Mapped[str] = mapped_column(String(16), default="1/24")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="planned")  # planned | paid | published | done | cancelled
    ad_post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channel: Mapped[Channel] = relationship()
    my_channel: Mapped[MyChannel] = relationship()
    creative: Mapped[MyCreative | None] = relationship()
    ad_post: Mapped[Post | None] = relationship()
    invite_link: Mapped[InviteLink | None] = relationship(back_populates="purchase", uselist=False)


class InviteLink(Base):
    __tablename__ = "invite_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    my_channel_id: Mapped[int] = mapped_column(ForeignKey("my_channels.id"))
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id"), unique=True)
    link: Mapped[str] = mapped_column(String(128), unique=True)
    title: Mapped[str] = mapped_column(String(64), default="")
    request_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    usage: Mapped[int] = mapped_column(Integer, default=0)
    requested: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    purchase: Mapped[Purchase | None] = relationship(back_populates="invite_link")
    joins: Mapped[list[Join]] = relationship(cascade="all, delete-orphan")


class Join(Base):
    __tablename__ = "joins"
    __table_args__ = (UniqueConstraint("invite_link_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    invite_link_id: Mapped[int] = mapped_column(ForeignKey("invite_links.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime)
    left_at: Mapped[datetime | None] = mapped_column(DateTime)


class ContentItem(Base):
    __tablename__ = "content_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    my_channel_id: Mapped[int] = mapped_column(ForeignKey("my_channels.id"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # copy | rewrite | ai | manual
    source_post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"))
    text: Mapped[str] = mapped_column(Text, default="")
    media_paths: Mapped[list] = mapped_column(JSON, default=list)
    # draft | generating | queued | published | failed | rejected
    status: Mapped[str] = mapped_column(String(16), default="draft")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    tg_msg_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source_post: Mapped[Post | None] = relationship()


class AiJob(Base):
    __tablename__ = "ai_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(256), default="")
    ref_id: Mapped[int | None] = mapped_column(Integer)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    system: Mapped[str] = mapped_column(Text, default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    # pending | running | manual (waits for a pasted answer) | done | error
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
