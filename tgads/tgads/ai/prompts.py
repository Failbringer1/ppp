"""Prompt builders. Each returns (system, prompt)."""
from __future__ import annotations

import json

from ..analysis.niches import TOPICS

LANG_NAMES = {"uz": "узбекский (латиница)", "uz_cyrl": "узбекский (кириллица)", "ru": "русский"}

BASE_SYSTEM = (
    "Ты помогаешь вести Telegram-каналы для аудитории Узбекистана и закупать в них рекламу. "
    "Отвечай по делу, без вступлений и пояснений, строго в запрошенном формате."
)


def classify_topics(channels: list[dict]) -> tuple[str, str]:
    prompt = (
        "Определи тематику каждого Telegram-канала. Выбирай строго одну из списка:\n"
        f"{', '.join(TOPICS)}\n\n"
        "Каналы (JSON):\n" + json.dumps(channels, ensure_ascii=False, indent=1) +
        '\n\nВерни только JSON-объект вида {"<id>": "<тематика>", ...} для всех каналов.'
    )
    return BASE_SYSTEM, prompt


def channel_ideas(niches: list[dict], creatives: list[dict], wishes: str) -> tuple[str, str]:
    prompt = (
        "Мне нужно запустить новый Telegram-канал в Узбекистане и раскрутить его закупом рекламы "
        "в других каналах. Ниже данные, которые я собрал парсером.\n\n"
        "Таблица ниш (score — привлекательность 0-100; incoming_ads — сколько найдено рекламных постов, "
        "которые продвигают каналы этой ниши, т.е. сколько в нишу вкладывают; median_err — живость аудитории):\n"
        + json.dumps(niches, ensure_ascii=False, indent=1) +
        "\n\nСамые повторяемые рекламные креативы (placements — сколько раз покупали размещение, "
        "hosts — на скольких площадках, days — сколько дней креатив крутят; чем больше, тем вероятнее он окупается):\n"
        + json.dumps(creatives, ensure_ascii=False, indent=1) +
        (f"\n\nМои пожелания: {wishes}" if wishes else "") +
        "\n\nПредложи 5 идей каналов. Для каждой:\n"
        "1. Название и позиционирование (1–2 предложения), язык канала.\n"
        "2. Почему эта ниша (опираясь на цифры выше).\n"
        "3. Контент-план: рубрики и частота, откуда брать контент.\n"
        "4. Два готовых рекламных поста для закупа (на языке канала), по мотивам лучших креативов, "
        "с цепляющим первым предложением и призывом подписаться.\n"
        "5. Риски.\n"
        "В конце — какую идею выбрать первой и почему. Формат — Markdown."
    )
    return BASE_SYSTEM, prompt


def ad_variants(creative_text: str, channel: dict, n: int = 4) -> tuple[str, str]:
    lang = LANG_NAMES.get(channel.get("language", "uz"), "узбекский (латиница)")
    prompt = (
        f"Вот рекламный пост, который рекламодатели многократно покупают (значит, он конвертит):\n---\n"
        f"{creative_text}\n---\n\n"
        f"Мой канал: {channel.get('title') or channel.get('ref')}\n"
        f"Тематика: {channel.get('topic')}\nПозиционирование: {channel.get('positioning')}\n\n"
        f"Напиши {n} разных рекламных поста для моего канала на языке: {lang}. "
        "Сохрани то, что делает исходный пост сильным (крючок в первой строке, выгода, срочность, призыв), "
        "но не копируй текст. Длина как у исходного. Вместо ссылки ставь {link}. Эмодзи уместны.\n"
        'Верни только JSON-массив строк: ["пост 1", "пост 2", ...]'
    )
    return BASE_SYSTEM, prompt


def rewrite_post(text: str, channel: dict) -> tuple[str, str]:
    lang = LANG_NAMES.get(channel.get("language", "uz"), "узбекский (латиница)")
    prompt = (
        f"Перепиши пост для моего канала «{channel.get('title') or channel.get('ref')}» "
        f"(тематика: {channel.get('topic')}; {channel.get('positioning')}).\n"
        f"Язык результата: {lang}.\n"
        "Требования: смысл и факты сохранить, формулировки сделать своими; убрать подписи, ссылки, "
        "@упоминания и хэштеги исходного канала; не добавлять выдуманных фактов; оформление для Telegram "
        "(короткие абзацы, эмодзи по месту).\n\nИсходный пост:\n---\n"
        f"{text}\n---\n\nВерни только готовый текст поста."
    )
    return BASE_SYSTEM, prompt


def generate_post(channel: dict, hint: str, examples: list[str]) -> tuple[str, str]:
    lang = LANG_NAMES.get(channel.get("language", "uz"), "узбекский (латиница)")
    ex = "\n---\n".join(examples[:3])
    prompt = (
        f"Напиши пост для Telegram-канала «{channel.get('title') or channel.get('ref')}» "
        f"(тематика: {channel.get('topic')}; {channel.get('positioning')}).\n"
        f"Язык: {lang}.\nТема/задание: {hint or 'на твой выбор, в духе канала'}\n"
        + (f"\nПримеры постов, которые хорошо зашли у похожих каналов:\n---\n{ex}\n---\n" if ex else "")
        + "\nНе выдумывай конкретные новости, цифры и даты, если их нет в задании. Верни только текст поста."
    )
    return BASE_SYSTEM, prompt


def parse_offer(messages: list[dict], channels: list[str]) -> tuple[str, str]:
    prompt = (
        "Это переписка с админом/менеджером Telegram-канала о покупке рекламы "
        f"(обсуждаемые каналы: {', '.join(channels) or 'не указаны'}).\n"
        + json.dumps(messages, ensure_ascii=False, indent=1) +
        "\n\nИзвлеки все предложения цены из сообщений админа. Форматы размещения: "
        "\"1/24\" (сутки в топе), \"1/48\", \"1/72\", \"2/48\", \"forever\" (без удаления). "
        "Суммы вида «300к», «300 ming» — это 300000 сум. Если цена в долларах — currency USD.\n"
        'Верни только JSON: {"offers": [{"channel": "@username или null", "amount": число, '
        '"currency": "UZS|USD|RUB", "format": "1/24", "notes": "скидки, свободные даты, условия"}], '
        '"status": "price_given|declined|question|other", "summary": "одной фразой по-русски"}'
    )
    return BASE_SYSTEM, prompt


def draft_reply(messages: list[dict], language: str, context: dict) -> tuple[str, str]:
    lang = LANG_NAMES.get(language, "узбекский (латиница)")
    prompt = (
        "Я закупаю рекламу для своего Telegram-канала. Переписка с админом площадки:\n"
        + json.dumps(messages, ensure_ascii=False, indent=1) +
        "\n\nКонтекст (CPM — цена за 1000 просмотров в сумах; honest_reach — сколько реально просмотров "
        "набирает пост в этом канале по моим замерам):\n"
        + json.dumps(context, ensure_ascii=False, indent=1) +
        f"\n\nНапиши мой следующий ответ на языке: {lang}. Коротко и вежливо, как живой человек. "
        "Если цена выше целевого CPM — аргументированно торгуйся (охват, пакет из нескольких постов, "
        "оплата сразу). Если цена ок — уточни свободные даты/время и реквизиты. Верни только текст сообщения."
    )
    return BASE_SYSTEM, prompt
