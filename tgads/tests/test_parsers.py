from datetime import datetime, timedelta

from tgads.analysis.contacts import extract_contacts
from tgads.analysis.niches import AdPost, ChannelRow, cluster_creatives, niche_table
from tgads.analysis.posts import ScoredPost, detect_ad, score_posts
from tgads.analysis.pricing import cpm, detect_format, parse_prices, recommend, to_uzs
from tgads.analysis.text import extract_refs, parse_channel_ref


def test_extract_refs_and_parse_ref():
    refs = extract_refs("Подпишись: @SuperKanal и https://t.me/+AbCdEfGh123 и t.me/news_uz/123")
    assert refs["usernames"] == {"superkanal", "news_uz"}
    assert refs["invites"] == {"AbCdEfGh123"}
    assert parse_channel_ref("https://t.me/Kun_uz") == "kun_uz"
    assert parse_channel_ref("@daryo") == "daryo"
    assert parse_channel_ref("t.me/joinchat/AAAAAEfgh123") == "+AAAAAEfgh123"
    assert parse_channel_ref("not a channel!") is None


def test_detect_ad():
    ad = detect_ad("🔥 Eng zo'r kino kanali! Obuna bo'ling 👉 https://t.me/+Xyz12345abc\n#reklama")
    assert ad.is_ad and ad.promotes_channel and "+Xyz12345abc" in ad.targets
    ad2 = detect_ad("Подписывайся на @crypto_signals_uz — лучшие сигналы")
    assert ad2.is_ad
    news = detect_ad("Сегодня в Ташкенте +25, без осадков. Источник: @meteo_uz", own_username="meteo_uz")
    assert not news.is_ad
    shop = detect_ad("Скидки 50% в магазине! uzum.uz/shop #reklama")
    assert shop.is_ad and not shop.promotes_channel


def test_score_posts_prefers_viral():
    posts = [ScoredPost(i, 1000, 5, 10, 48) for i in range(10)]
    posts.append(ScoredPost("viral", 3000, 90, 60, 48))
    posts.append(ScoredPost("young", 9000, 900, 900, 2))
    ranked = score_posts(posts)
    assert ranked[0].key == "viral"
    assert all(p.key != "young" for p in ranked)


def test_extract_contacts_prioritizes_ad_manager():
    about = ("Самые свежие новости Узбекистана 🇺🇿\n"
             "Наш чат: @news_chat_uz\n"
             "По вопросам рекламы: @ads_manager_uz\n"
             "Тел: +998 90 123-45-67, почта ads@news.uz")
    found = extract_contacts(about, own_username="news_uz")
    assert found[0].kind == "username" and found[0].value == "ads_manager_uz" and found[0].ad_context
    kinds = {(c.kind, c.value) for c in found}
    assert ("phone", "+998901234567") in kinds
    assert ("email", "ads@news.uz") in kinds
    assert ("username", "news_chat_uz") in kinds


def test_extract_contacts_uzbek():
    found = extract_contacts("Reklama bo'yicha murojaat: @reklama_admin", own_username="kanal")
    assert found and found[0].value == "reklama_admin" and found[0].ad_context


def test_parse_prices_variants():
    p = parse_prices("1/24 — 300 000 сум\n2/48 - 450.000 so'm\nнавсегда 1 млн")
    assert [(o.amount, o.currency, o.fmt) for o in p] == [
        (300000, "UZS", "1/24"), (450000, "UZS", "2/48"), (1_000_000, "UZS", "forever")]
    p = parse_prices("1/24 — 250 ming so'm, 1/48 — 350 ming")
    assert [(o.amount, o.fmt) for o in p] == [(250000, "1/24"), (350000, "1/48")]
    p = parse_prices("250к за 1/24, 400к за 2/48")
    assert [(o.amount, o.fmt) for o in p] == [(250000, "1/24"), (400000, "2/48")]
    p = parse_prices("Narxi 250 ming so'm, 24 soat")
    assert p[0].amount == 250000 and p[0].fmt == "1/24"
    p = parse_prices("Цена $25 за 1/24")
    assert p[0].amount == 25 and p[0].currency == "USD"
    p = parse_prices("у нас 45k подписчиков, пост выйдет в 19:00")
    assert p == []
    p = parse_prices("1500 руб")
    assert p[0].currency == "RUB"


def test_detect_format():
    assert detect_format("размещение 1/48") == "1/48"
    assert detect_format("пост без удаления") == "forever"
    assert detect_format("72 soat turadi") == "1/72"
    assert detect_format("просто текст") is None


def test_cpm_and_recommend():
    uzs = to_uzs(20, "USD", usd_uzs=12_700, rub_uzs=150)
    assert uzs == 254_000
    c = cpm(254_000, 10_000)
    assert round(c) == 25_400
    assert recommend(c, 30_000) == "buy"
    assert recommend(c, 20_000) == "negotiate"
    assert recommend(c, 10_000) == "skip"
    assert recommend(None, 10_000) == "unknown"


def test_cluster_creatives_groups_reused_text():
    base = datetime(2026, 10, 1)
    text = "Eng sara kinolar va seriallar faqat bizda! Har kuni yangi premyera. Obuna bo'ling 👉 t.me/+Kino123abc"
    ads = [AdPost(i, f"host{i}", text + (" 🔥" if i % 2 else ""), {"+Kino123abc"}, base + timedelta(days=i * 3))
           for i in range(6)]
    ads.append(AdPost("other", "hostX", "Kripto signallar: kuniga 3 ta signal, obuna: @crypto_uz", {"crypto_uz"}, base))
    clusters = cluster_creatives(ads)
    assert len(clusters) == 2
    top = clusters[0]
    assert len(top.posts) == 6 and len(top.hosts) == 6
    assert top.score > clusters[1].score


def test_niche_table_ranks():
    chans = [ChannelRow("кино/сериалы", 50_000, 12_000, 0.24, 0.2) for _ in range(5)]
    chans += [ChannelRow("новости", 500_000, 60_000, 0.12, 0.1) for _ in range(40)]
    rows = niche_table(chans, {"кино/сериалы": 50, "новости": 5})
    assert {r.topic for r in rows} == {"кино/сериалы", "новости"}
    assert rows[0].score == 100.0
    kino = next(r for r in rows if r.topic == "кино/сериалы")
    assert kino.channels == 5 and kino.incoming_ads == 50
