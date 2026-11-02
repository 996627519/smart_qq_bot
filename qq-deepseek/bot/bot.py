"""bot.py — QQ 群聊机器人（NoneBot2 + OneBot V11 + DeepSeek）

设计原则：
1. 先判断"该不该说话"，再让模型"说话"。不把沉默交给模型去赌。
2. 上下文按真实时间线喂给模型，带时间、带"回谁"，自己的消息标成「我」。
3. 输出必须过一遍清洗：长度、表情、客服腔、复读，全都要卡。
4. 发消息要有人味：先"看一会儿"，再分条发，中间有间隔。

配套文件：persona.py（人设/判断/清洗）、selftest.py（离线调参）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import sqlite3
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import nonebot
from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from openai import AsyncOpenAI

from persona import (
    Cleaned,
    GateConfig,
    Msg,
    build_messages,
    choose_bubbles,
    clean_reply,
    current_style,
    decide,
    is_duplicate,
    local_tz,
    looks_silent,
    render_context,
)


# --------------------------------------------------------------------------- 配置


def _load_dotenv(path: Path) -> None:
    """把 bot.py 同目录的 .env 读进 os.environ。

    NoneBot 自己也会读 .env，但只填它自己的 Config，不会写进进程环境变量，
    所以这里用 os.getenv 读的配置（BOT_NAME / BOT_ALIASES / BOT_TZ …）必须自己加载一次。
    已存在的真实环境变量优先，不会被 .env 覆盖（docker-compose environment 仍然最高优先级）。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
        return
    except Exception:
        pass
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if " #" in value and not value.startswith(("'", '"')):
                value = value.split(" #", 1)[0].strip()
            value = value.strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


_load_dotenv(Path(__file__).resolve().parent / ".env")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_quiet(raw: str) -> tuple[int, int] | None:
    raw = (raw or "").strip()
    if not raw or raw.lower() in {"off", "none", "0"}:
        return None
    try:
        start, end = raw.replace("~", "-").split("-", 1)
        a, b = int(start) % 24, int(end) % 24
        return (a, b) if a != b else None
    except ValueError:
        return None


TZ = local_tz(os.getenv("BOT_TZ", "Asia/Shanghai"))
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
BUILD = "2026-11-01d"  # 启动日志里会打出来，用来确认代码真的更新了
BOT_NAME = os.getenv("BOT_NAME", "").strip()
ALIASES = tuple(
    item.strip()
    for item in os.getenv("BOT_ALIASES", "").replace("，", ",").split(",")
    if item.strip()
)
CHAT_MAX_TOKENS = _env_int("CHAT_MAX_TOKENS", 120)
TEMPERATURE = _env_float("CHAT_TEMPERATURE", 1.3)
DEBOUNCE = (
    _env_float("REPLY_DEBOUNCE_MIN", 0.8),
    _env_float("REPLY_DEBOUNCE_MAX", 1.8),
)
NOTES_ENABLED = _env_bool("GROUP_NOTES_ENABLED", True)
NOTES_EVERY = max(20, _env_int("GROUP_NOTES_EVERY_MESSAGES", 40))
NOTES_MIN_MESSAGES = _env_int("GROUP_NOTES_MIN_MESSAGES", 25)
ALIAS_ENABLED = _env_bool("ALIAS_ENABLED", True)
ALLOW_GROUP_ASK = _env_bool("ALLOW_GROUP_ASK", False)
ALLOW_PRIVATE_CHAT = _env_bool("ALLOW_PRIVATE_CHAT", True)
DEBUG_GATE = _env_bool("DEBUG_GATE", True)
MULTI_MSG_RATE = min(1.0, max(0.0, _env_float("MULTI_MSG_RATE", 0.45)))
MULTI_MSG_MAX = max(1, min(4, _env_int("MULTI_MSG_MAX", 3)))
DATABASE_PATH = Path(os.getenv("BOT_DB_PATH", "/app/data/bot-memory.sqlite3"))

# 表情包：不手工维护，全部来自群友，自动收录到 stickers/auto/
STICKER_ROOT = Path(os.getenv("STICKER_DIR", "/app/stickers"))
AUTO_DIR = STICKER_ROOT / "auto"
INDEX_PATH = STICKER_ROOT / "index.json"
STICKER_ENABLED = _env_bool("STICKER_ENABLED", True)

# 主人：这个 QQ 号会被标成"主人"，人设里只对 TA 撒娇
BOT_OWNER = os.getenv("BOT_OWNER", "").strip()

# 看图：DeepSeek 文本模型看不了图，这里默认用智谱的免费多模态 glm-4v-flash
VISION_MODEL = os.getenv("VISION_MODEL", "glm-4v-flash").strip()
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/").strip()
VISION_API_KEY = os.getenv("VISION_API_KEY", "de50373b245d44ca86a6750e19d6b7c2.fB6QoibvRNix0uIC").strip()

# 没有冷却、没有额度、没有静默时段：默认每条都回。
GATE = GateConfig(
    reply_rate=min(1.0, max(0.0, _env_float("REPLY_RATE", 1.0))),
    ignore_at_others=_env_bool("IGNORE_AT_OTHERS", True),
    tz=TZ,
)

PERSONA_NAME = BOT_NAME or (ALIASES[0] if ALIASES else "小号")

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(Adapter)
logger = nonebot.logger

logger.info(
    "构建={} 配置：name={} 人格={} 回复率={} 忽略@别人={} aliases={} tz={} model={} 私聊={} 调试={}",
    BUILD,
    PERSONA_NAME,
    current_style().key,
    GATE.reply_rate,
    GATE.ignore_at_others,
    "/".join(ALIASES) or "-",
    TZ,
    MODEL,
    ALLOW_PRIVATE_CHAT,
    DEBUG_GATE,
)

client = AsyncOpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    timeout=float(os.getenv("DEEPSEEK_TIMEOUT", "45")),
    max_retries=1,
)
semaphore = asyncio.Semaphore(max(1, _env_int("DEEPSEEK_CONCURRENCY", 3)))

vision_client = (
    AsyncOpenAI(api_key=VISION_API_KEY, base_url=VISION_BASE_URL, timeout=25, max_retries=1)
    if VISION_MODEL and VISION_API_KEY
    else None
)
vision_semaphore = asyncio.Semaphore(2)
_caption_cache: dict[str, str] = {}


# --------------------------------------------------------------------------- 表情包
#
# 表情包不手工维护，全部来自群友：有人发表情包就自动下载、去重、打标签、入库。
# 目录结构：
#   stickers/auto/<sha1>.png     收录来的图
#   stickers/index.json          {文件名: {tag, caption, count}}
# 也兼容手工放：stickers/<标签>/01.png

_STICKER_ALIASES: dict[str, tuple[str, ...]] = {
    "笑": ("搞笑", "哈哈", "乐", "开心", "笑脸", "滑稽", "绷不住"),
    "无语": ("沉默", "汗", "尴尬", "离谱", "翻白眼"),
    "疑惑": ("疑问", "问号", "不懂", "迷惑", "what"),
    "赞同": ("赞", "同意", "行", "OK", "ok", "大拇指"),
    "生气": ("怒", "气", "火大", "滚"),
    "哭": ("泪", "难过", "委屈", "破防"),
    "萌": ("可爱", "猫", "狗", "萌", "心"),
}

# 从图片描述里猜标签，猜不到就进「随便」。
# 注意别用"好""心"这种太泛的字，否则"好奇"会被判成"赞同"。
_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("笑", ("笑", "哈哈", "乐", "滑稽", "搞笑", "咧嘴")),
    ("无语", ("无语", "沉默", "尴尬", "汗", "离谱", "白眼", "摊手")),
    ("疑惑", ("疑惑", "问号", "不懂", "迷惑", "歪头", "好奇")),
    ("赞同", ("赞", "牛", "强", "竖起", "ok", "大拇指", "厉害")),
    ("生气", ("怒", "生气", "火大", "滚", "瞪")),
    ("哭", ("哭", "泪", "委屈", "破防", "难过")),
    ("萌", ("可爱", "猫", "狗", "萌", "抱")),
)


def guess_tag(caption: str) -> str:
    text = (caption or "").lower()
    if not text:
        return "随便"
    for tag, words in _TAG_RULES:
        if any(word in text for word in words):
            return tag
    return "随便"


def scan_manual(root: Path) -> dict[str, list[Path]]:
    """手工放的表情包：stickers/<标签>/xx.png 或 stickers/<标签>_01.png。"""
    lib: dict[str, list[Path]] = {}
    if not root.is_dir():
        return lib
    suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if rel.parts[0] == "auto":
            continue
        tag = rel.parts[0] if len(rel.parts) > 1 else path.stem.split("_")[0]
        tag = tag.strip()
        if tag:
            lib.setdefault(tag, []).append(path)
    return lib


def load_index() -> dict[str, dict]:
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_index() -> None:
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(
            json.dumps(_AUTO_META, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception:
        logger.exception("写入表情包索引失败")


def rebuild_library() -> None:
    STICKERS.clear()
    if not STICKER_ENABLED:
        return
    for tag, files in scan_manual(STICKER_ROOT).items():
        STICKERS.setdefault(tag, []).extend(files)
    for name, meta in _AUTO_META.items():
        path = AUTO_DIR / name
        if path.exists():
            STICKERS.setdefault(meta.get("tag") or "随便", []).append(path)


async def collect_sticker(url: str, caption: str = "") -> Path | None:
    """把群友发的表情包收进库里。同一张图按内容哈希去重。返回本地文件路径。"""
    if not url or not STICKER_ENABLED:
        return None
    try:
        raw = await asyncio.to_thread(_fetch_bytes, url)
    except Exception as exc:
        logger.warning("收录失败，下载不了：{}（{}）", exc, url[:60])
        return None
    if not raw or len(raw) > 3_000_000:
        return None
    if raw[:4] == b"GIF8":
        ext = ".gif"  # 动图也收：发出去还是动的，看图时取第一帧
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif raw[:2] == b"\xff\xd8":
        ext = ".jpg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = ".webp"
    else:
        return None

    name = hashlib.sha1(raw).hexdigest()[:16] + ext
    path = AUTO_DIR / name
    meta = _AUTO_META.get(name)
    if meta is not None:
        meta["count"] = int(meta.get("count", 1)) + 1
        if caption and not meta.get("caption"):
            meta["caption"] = caption
            if meta.get("tag") in (None, "", "随便"):
                meta["tag"] = guess_tag(caption)
        save_index()
        return path if path.exists() else None

    try:
        AUTO_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    except Exception:
        logger.exception("保存表情包失败")
        return None

    tag = guess_tag(caption) if caption else "随便"
    _AUTO_META[name] = {"tag": tag, "caption": caption, "count": 1}
    STICKERS.setdefault(tag, []).append(path)
    save_index()
    logger.info("收录表情包 {}（{}）{}", name, tag, caption[:20])
    return path


async def collect_and_remember(chat_key: str, url: str, caption: str) -> None:
    """收录，并记住"这个会话最近收到的那张图"，方便之后 <表情:最近> 重发。"""
    path = await collect_sticker(url, caption)
    if path is not None:
        LAST_STICKER[chat_key] = path


def resolve_sticker_tag(tag: str) -> str | None:
    """模型给的标签可能很随意，做个宽松匹配。"""
    t = (tag or "").strip()
    if not t or not STICKERS:
        return None
    if t in STICKERS:
        return t
    for key, aliases in _STICKER_ALIASES.items():
        if key in STICKERS and (t in aliases or any(a in t for a in aliases)):
            return key
    for key in STICKERS:
        if key in t or t in key:
            return key
    return None


def pick_sticker(tag: str) -> Path | None:
    key = resolve_sticker_tag(tag)
    return random.choice(STICKERS[key]) if key else None


STICKERS: dict[str, list[Path]] = {}
_AUTO_META: dict[str, dict] = load_index()
LAST_STICKER: dict[str, Path] = {}  # 会话 -> 最近收到的表情包，供 <表情:最近> 重发
_RECENT_TAGS = {"最近", "刚才", "上一张", "原图", "recent", "last"}
rebuild_library()


# --------------------------------------------------------------------------- 看图

def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://q.qq.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read(4_000_000)


_CAPTION_PROMPT = (
    "用一句中文描述这张表情包：画面是什么、什么情绪，"
    "20 字以内，不要客套，不要“这是一张”开头。"
)
_FRAMES_PROMPT = (
    "这是一张动图按时间顺序抽的 {n} 帧。用一句中文说出它表达的梗或情绪，"
    "20 字以内，不要客套，不要逐帧描述。"
)


def _extract_frames(raw: bytes, count: int = 3) -> list[bytes]:
    """从动图里均匀抽几帧，转成 JPEG。没装 Pillow 或不是动图就返回空。"""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            total = int(getattr(im, "n_frames", 1) or 1)
            if total <= 1:
                return []
            index = sorted({0, total // 2, total - 1})[:count]
            frames: list[bytes] = []
            for idx in index:
                im.seek(idx)
                frame = im.convert("RGB")
                frame.thumbnail((384, 384))  # 多帧要走体积，别太大
                out = io.BytesIO()
                frame.save(out, format="JPEG", quality=75)
                frames.append(out.getvalue())
            return frames
    except Exception:
        return []


def _data_url(payload: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


async def _vision_request(images: list[str], prompt: str) -> str:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": image}})
    async with vision_semaphore:
        resp = await asyncio.wait_for(
            vision_client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{"role": "user", "content": content}],
                max_tokens=80,
            ),
            timeout=25,
        )
    return (resp.choices[0].message.content or "").strip().replace("\n", " ")[:40]


async def describe_image(url: str) -> str:
    """用多模态模型说一句这张图/表情包是什么。没配 VISION_API_KEY 就返回空。"""
    if not url or vision_client is None:
        return ""
    key = hashlib.md5(url.encode()).hexdigest()
    cached = _caption_cache.get(key)
    if cached is not None:
        return cached
    try:
        raw = await asyncio.to_thread(_fetch_bytes, url)
    except Exception as exc:
        logger.warning("图片下载失败：{}（{}）", exc, url[:60])
        return ""

    # 按优先级准备几种问法，模型不吃就换下一种
    attempts: list[tuple[list[str], str]] = []
    if raw[:4] == b"GIF8":
        frames = await asyncio.to_thread(_extract_frames, raw, 3)
        if len(frames) >= 2:
            attempts.append(
                (
                    [_data_url(f) for f in frames],
                    _FRAMES_PROMPT.format(n=len(frames)),
                )
            )
        elif frames:
            attempts.append(([_data_url(frames[0])], _CAPTION_PROMPT))
        else:
            logger.info("动图抽帧失败（没装 Pillow？），退回整图试一次")
        attempts.append(([_data_url(raw, "image/gif")], _CAPTION_PROMPT))
    else:
        attempts.append(([_data_url(raw)], _CAPTION_PROMPT))

    text = ""
    for images, prompt in attempts:
        try:
            text = await _vision_request(images, prompt)
        except Exception:
            try:
                # 有些接口（比如智谱）直接吃裸 base64，不吃 data: 前缀
                text = await _vision_request(
                    [img.split(",", 1)[-1] for img in images], prompt
                )
            except Exception:
                logger.warning("视觉模型不吃这种输入（{} 张图），换下一种问法", len(images))
                continue
        if text:
            break

    if not text:
        logger.warning("看图失败：所有格式都不行")
        return ""
    if len(_caption_cache) > 500:
        _caption_cache.clear()
    _caption_cache[key] = text
    logger.info("看图结果：{}", text)
    return text


logger.info(
    "表情包 {} 张，标签 {}｜看图 {}",
    sum(len(v) for v in STICKERS.values()),
    "/".join(STICKERS) or "-",
    f"开（{VISION_MODEL}）"
    if vision_client is not None
    else "关（没填 VISION_API_KEY）",
)


# --------------------------------------------------------------------------- 存储

def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_consent ("
        "group_id TEXT NOT NULL, user_id TEXT NOT NULL, enabled INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL, PRIMARY KEY (group_id, user_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS group_notes ("
        "group_id TEXT PRIMARY KEY, notes TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS group_aliases ("
        "group_id TEXT NOT NULL, alias TEXT NOT NULL, target TEXT NOT NULL, "
        "updated_at INTEGER NOT NULL, PRIMARY KEY (group_id, alias))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS group_diary ("
        "group_id TEXT PRIMARY KEY, text TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, nickname TEXT NOT NULL, text TEXT NOT NULL, "
        "ts REAL NOT NULL, is_self INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages(group_id, ts)"
    )
    conn.commit()
    return conn


db = open_database(DATABASE_PATH)


def memory_enabled(group_id: str, user_id: str) -> bool:
    try:
        row = db.execute(
            "SELECT enabled FROM memory_consent WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
    except Exception:
        # 数据库出问题也不能让整个群的消息处理失败
        logger.exception("读取记忆开关失败，先按允许处理")
        return True
    return True if row is None else bool(row[0])


def set_consent(group_id: str, user_id: str, enabled: bool) -> None:
    try:
        db.execute(
            "INSERT INTO memory_consent(group_id,user_id,enabled,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(group_id,user_id) DO UPDATE SET enabled=excluded.enabled, "
            "updated_at=excluded.updated_at",
            (group_id, user_id, int(enabled), int(time.time())),
        )
        db.commit()
    except Exception:
        logger.exception("写入记忆开关失败")


def load_notes(group_id: str) -> str:
    try:
        row = db.execute(
            "SELECT notes FROM group_notes WHERE group_id=?", (group_id,)
        ).fetchone()
    except Exception:
        logger.exception("读取群梗失败，先当没有")
        return ""
    return row[0] if row else ""


def load_aliases(group_id: str) -> dict[str, str]:
    """群里的外号 → 本人。只保留最近 20 条。"""
    try:
        rows = db.execute(
            "SELECT alias, target FROM group_aliases WHERE group_id=? "
            "ORDER BY updated_at DESC LIMIT 20",
            (group_id,),
        ).fetchall()
    except Exception:
        logger.exception("读取外号失败，先当没有")
        return {}
    return {str(alias): str(target) for alias, target in rows}


def load_diary(group_id: str) -> str:
    try:
        row = db.execute(
            "SELECT text FROM group_diary WHERE group_id=?", (group_id,)
        ).fetchone()
    except Exception:
        logger.exception("读取长期记忆失败")
        return ""
    return row[0] if row else ""


MESSAGE_KEEP_DAYS = 90
_message_writes = 0


def prune_messages() -> None:
    try:
        cutoff = time.time() - MESSAGE_KEEP_DAYS * 86400
        db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        # 再兜一层总量：只留最近 20 万条
        db.execute(
            "DELETE FROM messages WHERE id NOT IN "
            "(SELECT id FROM messages ORDER BY id DESC LIMIT 200000)"
        )
        db.commit()
    except Exception:
        logger.exception("清理消息表失败")


def save_message(group_id: str, msg: Msg) -> None:
    global _message_writes
    try:
        db.execute(
            "INSERT INTO messages(group_id,user_id,nickname,text,ts,is_self) "
            "VALUES(?,?,?,?,?,?)",
            (
                group_id,
                msg.user_id,
                msg.nickname,
                msg.text[:500],
                msg.ts,
                1 if msg.is_self else 0,
            ),
        )
        db.commit()
        _message_writes += 1
        if _message_writes % 200 == 0:
            prune_messages()
    except Exception:
        logger.exception("写消息表失败")


def recall_mentions(
    group_id: str,
    text: str,
    names: set[str],
    *,
    now: float,
    limit: int = 3,
) -> list[str]:
    """消息里提到了谁，就翻一下以前谁说过什么。

    只按"名字"召回，不做向量检索——中文不分词，关键词方案噪声太大；
    名字召回精度高、零依赖，配合 diary 足够。
    """
    hits = [n for n in names if len(n) >= 2 and n in text][:3]
    if not hits:
        return []
    cutoff = now - 600  # 只回忆 10 分钟之前的，最近的本来就在上下文里
    rows: list[tuple[str, str, float]] = []
    for name in hits:
        try:
            found = db.execute(
                "SELECT nickname, text, ts FROM messages "
                "WHERE group_id=? AND ts < ? AND is_self=0 AND text LIKE ? "
                "ORDER BY ts DESC LIMIT 2",
                (group_id, cutoff, f"%{name}%"),
            ).fetchall()
        except Exception:
            continue
        rows.extend((str(n), str(t), float(ts)) for n, t, ts in found)

    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    for nickname, body, ts in sorted(rows, key=lambda r: r[2], reverse=True):
        key = (nickname, body[:20])
        if key in seen:
            continue
        seen.add(key)
        stamp = datetime.fromtimestamp(ts, TZ).strftime("%m-%d")
        out.append(f"{stamp} {nickname}：{body[:50]}")
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- 状态

@dataclass
class GroupState:
    messages: deque = field(default_factory=lambda: deque(maxlen=60))
    last_bot_reply: float = -1e9
    reply_times: deque = field(default_factory=lambda: deque(maxlen=60))
    recent_replies: deque = field(default_factory=lambda: deque(maxlen=12))
    handled: deque = field(default_factory=lambda: deque(maxlen=200))
    notes: str = ""
    notes_running: bool = False
    aliases: dict[str, str] = field(default_factory=dict)
    diary: str = ""
    counter: int = 0
    task: asyncio.Task | None = None
    task_started: float = 0.0
    generating: bool = False
    queued: Msg | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=30))
    last_event: float = 0.0


group_state: dict[str, GroupState] = {}
_names_cache: dict[str, tuple[str, ...]] = {}
_bg_tasks: set[asyncio.Task] = set()
STATS = {"start": time.time(), "msg": 0, "reply": 0}


def state_for(group_id: str) -> GroupState:
    st = group_state.get(group_id)
    if st is None:
        st = GroupState(
            notes=load_notes(group_id),
            aliases=load_aliases(group_id),
            diary=load_diary(group_id),
        )
        group_state[group_id] = st
    return st


def seen_before(st: GroupState, msg: Msg) -> bool:
    """按 message_id 去重，防止重连后同一条事件被处理两次。

    message_id 缺失或者是 0 的时候不能用来去重——那种情况下所有消息都长一样，
    第一条之后整个群都会被当成"已经处理过"而永久静默。
    """
    if not msg.msg_id or msg.msg_id == "0":
        return False
    if msg.msg_id in st.handled:
        return True
    st.handled.append(msg.msg_id)
    return False


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# --------------------------------------------------------------------------- 模型调用

async def chat(messages: list[dict], *, max_tokens: int) -> str:
    async with semaphore:
        # 双保险：openai 客户端自己有 timeout，这里再兜一层，
        # 防止某次请求挂死把 semaphore 永久占住（占满 3 个之后全群都不说话了）
        result = await asyncio.wait_for(
            client.chat.completions.create(
                model=MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=TEMPERATURE,
                top_p=0.9,
            ),
            timeout=60,
        )
    return (result.choices[0].message.content or "").strip()


async def bot_names(bot: Bot) -> tuple[str, ...]:
    """群友可能叫它昵称、群名片、或者随便起的外号。能识别的名字越全，越不会漏接。"""
    global PERSONA_NAME
    sid = str(bot.self_id)
    cached = _names_cache.get(sid)
    if cached is not None:
        return cached
    names = {item for item in ALIASES if item}
    if PERSONA_NAME:
        names.add(PERSONA_NAME)
    try:
        info = await asyncio.wait_for(bot.get_login_info(), timeout=5)
        nick = str((info or {}).get("nickname") or "").strip()
        if nick:
            names.add(nick)
            if not BOT_NAME and PERSONA_NAME == "小号":
                PERSONA_NAME = nick  # 人设里的名字和 QQ 昵称对齐
                logger.info("人设名字取自 QQ 昵称：{}", PERSONA_NAME)
    except Exception:
        logger.debug("取不到登录昵称，用别名兜底")
    result = tuple(sorted((n for n in names if len(n) >= 2), key=len, reverse=True))
    _names_cache[sid] = result
    return result


# --------------------------------------------------------------------------- 群梗记忆

NOTES_PROMPT = (
    "下面是某个 QQ 群最近的部分聊天记录，以及你之前记下的长期备忘。做三件事：\n"
    "1) 提炼这个群特有的、可以安全复述的梗、黑话、固定玩笑或共同话题，最多 3 条，每条不超过 20 字。\n"
    "2) 找出明确出现过的“外号 / 称呼 → 本人”对应关系，例如“老王就是张三”“叫白哥的是老白”。"
    "只记聊天里明确成立的说法，别猜，别把普通昵称当外号，最多 5 条。\n"
    "3) 更新一份 300 字以内的长期备忘：谁是谁、外号、最近在忙什么、没聊完的话题、约定和梗。"
    "在旧备忘基础上增删，过时的删掉，别流水账。\n"
    "要求：只写群里公开说过的；不要记录任何人的真实姓名、住址、单位、联系方式、"
    "政治宗教观点、健康或经济状况；不要记录一次性玩笑；没有就给空。\n"
    '只返回 JSON：{"notes":["..."],"aliases":[{"alias":"外号","target":"本人昵称"}],"diary":"..."}\n\n'
    "旧备忘：{diary}\n\n聊天记录：\n"
)


async def refresh_notes(group_id: str, st: GroupState, eligible: list[Msg]) -> None:
    if st.notes_running:
        return
    st.notes_running = True
    try:
        if len(eligible) < NOTES_MIN_MESSAGES:
            return
        lines = "\n".join(f"{m.nickname}：{m.text[:120]}" for m in eligible[-50:])
        prompt = NOTES_PROMPT.replace("{diary}", st.diary or "（还没有）") + lines
        async with semaphore:
            result = await asyncio.wait_for(
                client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=420,
                    temperature=0.3,
                ),
                timeout=45,
            )
        content = (result.choices[0].message.content or "").strip()
        start, end = content.find("{"), content.rfind("}")
        data = json.loads(content[start:end + 1]) if start >= 0 and end > start else {}
        raw_notes = data.get("notes") if isinstance(data, dict) else None
        notes = "；".join(str(x).strip()[:24] for x in (raw_notes or []) if str(x).strip())
        notes = notes[:160]
        db.execute(
            "INSERT INTO group_notes(group_id,notes,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET notes=excluded.notes, "
            "updated_at=excluded.updated_at",
            (group_id, notes, int(time.time())),
        )
        db.commit()
        st.notes = notes
        logger.info("群梗已更新 {}：{}", group_id, notes or "(空)")

        if ALIAS_ENABLED:
            raw_aliases = data.get("aliases") if isinstance(data, dict) else None
            now = int(time.time())
            added = 0
            for item in (raw_aliases or [])[:5]:
                if not isinstance(item, dict):
                    continue
                alias = str(item.get("alias") or "").strip()[:12]
                target = str(item.get("target") or "").strip()[:12]
                if not alias or not target or alias == target:
                    continue
                db.execute(
                    "INSERT INTO group_aliases(group_id,alias,target,updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(group_id,alias) DO UPDATE SET "
                    "target=excluded.target, updated_at=excluded.updated_at",
                    (group_id, alias, target, now),
                )
                added += 1
            if added:
                # 只留最近 20 条，防止无限膨胀
                db.execute(
                    "DELETE FROM group_aliases WHERE group_id=? AND alias NOT IN "
                    "(SELECT alias FROM group_aliases WHERE group_id=? "
                    "ORDER BY updated_at DESC LIMIT 20)",
                    (group_id, group_id),
                )
            db.commit()
            st.aliases = load_aliases(group_id)
            if added:
                logger.info("外号已更新 {}：{}", group_id, st.aliases)

        diary = str(data.get("diary") or "").strip().replace("\n", " ")[:500]
        if diary and diary != st.diary:
            db.execute(
                "INSERT INTO group_diary(group_id,text,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET text=excluded.text, "
                "updated_at=excluded.updated_at",
                (group_id, diary, int(time.time())),
            )
            db.commit()
            st.diary = diary
            logger.info("长期备忘已更新 {}：{}", group_id, diary[:60])
    except Exception:
        logger.exception("group notes refresh failed")
    finally:
        st.notes_running = False


# --------------------------------------------------------------------------- 消息解析

def build_msg(event: MessageEvent) -> Msg:
    at_ids: list[str] = []
    chunks: list[str] = []
    media = False
    media_kind = ""
    media_url = ""
    caption = ""
    for seg in event.get_message():
        if seg.type == "text":
            chunks.append(str(seg.data.get("text", "")))
        elif seg.type == "at":
            qq = str(seg.data.get("qq", "") or "")
            if qq:
                at_ids.append(qq)
        elif seg.type == "image":
            media = True
            if not media_url:
                media_url = str(seg.data.get("url") or "")
            summary = str(seg.data.get("summary") or "")
            sub_type = str(seg.data.get("sub_type") or seg.data.get("subType") or "")
            filename = str(seg.data.get("file") or "")
            if (
                sub_type == "1"
                or summary in {"[动画表情]", "[表情]"}
                or filename.lower().endswith(".gif")
            ):
                media_kind = "sticker"
            elif not media_kind:
                media_kind = "image"
        elif seg.type == "mface":
            # 商城表情：本身就带 summary，等于免费的"看懂"
            media = True
            media_kind = "sticker"
            label = str(seg.data.get("summary") or "").strip(" []")
            if label and label != "表情":
                caption = label
        elif seg.type in {"record", "video", "json", "xml", "file", "dice", "rps"}:
            media = True
            media_kind = media_kind or "image"

    reply_user = reply_nick = ""
    reply = getattr(event, "reply", None)
    if reply is not None:
        try:
            reply_user = str(reply.sender.user_id)
            reply_nick = (reply.sender.card or reply.sender.nickname or reply_user).strip()[:20]
        except Exception:
            pass

    sender = event.sender
    nickname = (sender.card or sender.nickname or str(event.user_id)).strip()[:20]
    # NapCat 的 event.time 正常是秒级时间戳；万一它给的是 0、毫秒或者时钟漂移，
    # 直接用会让"距上一条多久"全部算错，所以偏差过大时退回本地接收时间。
    now = time.time()
    raw_ts = float(getattr(event, "time", 0) or 0)
    ts = raw_ts if raw_ts and abs(now - raw_ts) <= 120 else now
    return Msg(
        ts=ts,
        user_id=str(event.user_id),
        nickname=nickname,
        text="".join(chunks).strip()[:1000],
        msg_id=str(getattr(event, "message_id", "") or ""),
        at_ids=at_ids,
        reply_to_user=reply_user,
        reply_to_nick=reply_nick,
        has_media=media,
        media_kind=media_kind,
        media_url=media_url,
        caption=caption,
        arrived=now,
    )


# --------------------------------------------------------------------------- 主流程

def _is_direct(msg: Msg, bot_id: str, names: tuple[str, ...]) -> bool:
    """这条消息是不是冲着我来的（@ 我、回我、或者叫我名字）。"""
    if bot_id and bot_id in msg.at_ids:
        return True
    if msg.reply_to_user and msg.reply_to_user == bot_id:
        return True
    return any(n and n in msg.text for n in names if len(n) >= 2)


group_messages = on_message(priority=50, block=False)


@group_messages.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    if str(event.user_id) == str(event.self_id):
        return

    plain = event.get_plaintext().strip()
    group_id = str(event.group_id)
    user_id = str(event.user_id)

    if plain in {"忘记我", "忘记我吧", "别记我"}:
        set_consent(group_id, user_id, False)
        await bot.send(event, "行 删了 以后不记你")
        return

    st = state_for(group_id)
    msg = build_msg(event)
    if seen_before(st, msg):
        return

    if msg.text.startswith(("/", "／", "!", "！")):
        return
    st.messages.append(msg)
    if msg.is_self or (not msg.text and not msg.has_media):
        return

    # 图片/表情包：配了视觉模型就翻译成一句话，进上下文
    if msg.media_url and vision_client is not None and not msg.caption:
        msg.caption = await describe_image(msg.media_url)

    # 表情包自动入库（群友发一张，库里多一张）
    if msg.media_kind == "sticker" and msg.media_url and STICKER_ENABLED:
        spawn(collect_and_remember(f"g{group_id}", msg.media_url, msg.caption))

    names = await bot_names(bot)
    STATS["msg"] += 1
    st.last_event = time.time()
    # 长期记忆的原料，落库；发过 /忘记我 的人不存
    if memory_enabled(group_id, msg.user_id):
        save_message(group_id, msg)

    st.counter += 1
    if NOTES_ENABLED and st.counter % NOTES_EVERY == 0:
        eligible = [
            m
            for m in list(st.messages)[-55:]
            if not m.is_self and m.text and memory_enabled(group_id, m.user_id)
        ]
        spawn(refresh_notes(group_id, st, eligible))

    decision = decide(msg, cfg=GATE, bot_id=str(event.self_id))
    st.log.append((time.time(), msg.nickname, decision.reason))
    if DEBUG_GATE:
        # 每条消息都留一条痕，方便确认"到底有没有收到、为什么没回"
        logger.info(
            "收到 {}｜{}：{}  -> {}",
            group_id,
            msg.nickname,
            msg.text[:30],
            decision.reason,
        )
    if not decision.should:
        if _is_direct(msg, str(event.self_id), names):
            logger.warning("被点名却没回（{}）：{}", decision.reason, msg.text[:40])
        return

    schedule_reply(bot, st, group_id, msg, names)


def schedule_reply(
    bot: Bot, st: GroupState, group_id: str, trigger: Msg, names: tuple[str, ...]
) -> None:
    if st.task is not None and not st.task.done():
        # 理论上不该卡这么久，真卡了就强制换掉，别让后面所有消息陪葬
        if time.time() - st.task_started < 150:
            st.queued = trigger  # 正在生成时又来了新消息，等这一轮结束再看
            return
        logger.warning("上一个回复任务卡了 {} 秒，强制重置", int(time.time() - st.task_started))
        st.task.cancel()
    st.task_started = time.time()
    st.task = asyncio.create_task(_reply_later(bot, st, group_id, trigger, names))


async def _reply_later(
    bot: Bot, st: GroupState, group_id: str, trigger: Msg, names: tuple[str, ...]
) -> None:
    """先"看一会儿"再回：合并连发消息，也避免刚发完一条又接一条。"""
    try:
        await asyncio.sleep(random.uniform(*DEBOUNCE))
        if st.generating:
            return
        now = time.time()
        # 只挡明显过期的排队消息，正常情况不会触发
        if now - (trigger.arrived or trigger.ts) > 120:
            logger.info("消息太旧，不回了 {}（延迟 {}s）", group_id, int(now - (trigger.arrived or trigger.ts)))
            return

        # 等待期间可能又发生了"别人在 @ 别人"之类的事，再判一次
        decision = decide(trigger, cfg=GATE, bot_id=str(bot.self_id))
        if not decision.should:
            logger.info("防抖后放弃 {}：{}", group_id, decision.reason)
            return

        st.generating = True
        try:
            # 兜底：万一下游连接半死，别让这个任务永远挂着把后面所有消息都堵死
            await asyncio.wait_for(_produce(bot, st, group_id), timeout=90)
        except asyncio.TimeoutError:
            logger.error("这一轮生成超过 90 秒，放弃 {}", group_id)
        finally:
            st.generating = False
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("reply task crashed")
    finally:
        queued = st.queued
        st.queued = None
        if queued is not None and time.time() - (queued.arrived or queued.ts) < 30:
            st.task = asyncio.create_task(
                _reply_later(bot, st, group_id, queued, names)
            )


async def _generate(base: list[dict], max_bubbles: int = 2) -> Cleaned | None:
    """生成 1~N 条短消息（可能带表情包）。模型想沉默或语句不合格时，催它重说一次。"""
    messages = base
    for attempt in range(3):
        try:
            raw = await chat(messages, max_tokens=CHAT_MAX_TOKENS)
        except Exception:
            logger.exception("DeepSeek chat failed")
            return None
        if not raw or looks_silent(raw):
            if attempt >= 2:
                return None
            messages = base + [
                {"role": "assistant", "content": (raw or "（沉默）")[:200]},
                {"role": "user", "content": "别沉默，随便接一句，短一点。"},
            ]
            continue
        cleaned = clean_reply(raw, max_bubbles=max_bubbles)
        if cleaned.ok:
            return cleaned
        if attempt >= 2:
            logger.info("丢弃发言：{}", cleaned.reason)
            return None
        messages = base + [
            {"role": "assistant", "content": raw[:200]},
            {"role": "user", "content": f"这句不行（{cleaned.reason}），换个说法，短一点。"},
        ]
    return None


async def _produce(bot: Bot, st: GroupState, group_id: str) -> None:
    context = render_context(
        list(st.messages),
        now=time.time(),
        tz=TZ,
        bot_id=str(bot.self_id),
        owner_id=BOT_OWNER,
    )
    if not context:
        return

    # 这一轮分几条发：多数一条，话题长/被提问时更容易连发
    last_in = next(
        (m for m in reversed(st.messages) if not m.is_self and m.text), None
    )
    target = choose_bubbles(
        last_in.text if last_in else "",
        rate=MULTI_MSG_RATE,
        max_bubbles=MULTI_MSG_MAX,
    )

    # 长期记忆：按"提到的人"翻旧账
    recalled: list[str] = []
    if last_in and last_in.text:
        known = set(st.aliases) | set(st.aliases.values())
        known |= {m.nickname for m in list(st.messages)[-20:] if not m.is_self}
        try:
            recalled = await asyncio.to_thread(
                recall_mentions,
                group_id,
                last_in.text,
                known,
                now=time.time(),
            )
        except Exception:
            logger.exception("召回旧消息失败")

    base = build_messages(
        name=PERSONA_NAME,
        context=context,
        notes=st.notes,
        avoid=list(st.recent_replies)[-6:],
        bubbles=target,
        sticker_tags=tuple(STICKERS) + ("最近",),
        aliases=st.aliases,
        diary=st.diary,
        recall=recalled,
    )

    cap = max(2, target)
    cleaned = await _generate(base, max_bubbles=cap)
    if not cleaned:
        logger.info("这一条没生成出内容 {}", group_id)
        return
    if any(is_duplicate(b, st.recent_replies) for b in cleaned.bubbles):
        # 换个说法再来一次，实在不行也照发，总比不说话强
        retry = await _generate(
            base
            + [
                {"role": "assistant", "content": " | ".join(cleaned.bubbles)[:200]},
                {"role": "user", "content": "这句刚说过了，换个说法，别重复。"},
            ],
            max_bubbles=cap,
        )
        if retry and not any(is_duplicate(b, st.recent_replies) for b in retry.bubbles):
            cleaned = retry

    if len(cleaned.items) > 1:
        logger.info("这一轮 {} 条 {}", len(cleaned.items), group_id)

    sent: list[str] = []
    for index, (kind, value) in enumerate(cleaned.items):
        if index:
            await asyncio.sleep(random.uniform(0.55, 1.25))
        if kind == "sticker":
            path = pick_sticker(value)
            if path is None and value.strip() in _RECENT_TAGS:
                path = LAST_STICKER.get(f"g{group_id}")
            if path is None:
                logger.warning("表情包没匹配上标签：{}（现有标签 {}）", value, "/".join(STICKERS) or "无")
                continue
            try:
                payload = path.read_bytes()
            except Exception:
                logger.exception("读取表情包失败 {}", path)
                continue
            try:
                await asyncio.wait_for(
                    bot.send_group_msg(
                        group_id=int(group_id), message=MessageSegment.image(payload)
                    ),
                    timeout=15,
                )
            except Exception:
                logger.exception("发表情包失败 {}", path.name)
                continue
            st.messages.append(
                Msg(
                    ts=time.time(),
                    user_id=str(bot.self_id),
                    nickname=PERSONA_NAME,
                    text="",
                    is_self=True,
                    has_media=True,
                    media_kind="sticker",
                    caption=value,
                )
            )
            logger.info("表情包 {} -> {}", group_id, path.name)
            continue

        try:
            await asyncio.wait_for(
                bot.send_group_msg(group_id=int(group_id), message=value),
                timeout=15,
            )
        except asyncio.TimeoutError:
            logger.error("发消息超时 {}，剩下的分条不发了", group_id)
            break
        except Exception:
            logger.exception("send_group_msg failed")
            break
        sent.append(value)
        st.messages.append(
            Msg(
                ts=time.time(),
                user_id=str(bot.self_id),
                nickname=PERSONA_NAME,
                text=value,
                is_self=True,
            )
        )

    if sent:
        st.last_bot_reply = time.time()
        st.reply_times.append(st.last_bot_reply)
        st.recent_replies.extend(sent)
        STATS["reply"] += len(sent)
        logger.info("发言 {} -> {}", group_id, " | ".join(sent))


# --------------------------------------------------------------------------- 私聊

@dataclass
class PrivateState:
    messages: deque = field(default_factory=lambda: deque(maxlen=20))
    recent: deque = field(default_factory=lambda: deque(maxlen=8))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last: float = 0.0


private_state: dict[str, PrivateState] = {}
private_messages = on_message(priority=50, block=False)


@private_messages.handle()
async def handle_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    if not ALLOW_PRIVATE_CHAT:
        return
    if str(event.user_id) == str(event.self_id):
        return

    uid = str(event.user_id)
    st = private_state.setdefault(uid, PrivateState())
    msg = build_msg(event)
    if not msg.text and not msg.has_media:
        return
    st.messages.append(msg)

    # 私聊也要看图、也要收录表情包（之前这里漏了，导致"没看到图"）
    if msg.has_media and vision_client is None:
        logger.info("收到图片/表情包，但看图没开（没填 VISION_API_KEY），只能记成[表情包]")
    if msg.media_url and vision_client is not None and not msg.caption:
        msg.caption = await describe_image(msg.media_url)
    if msg.media_kind == "sticker" and msg.media_url and STICKER_ENABLED:
        spawn(collect_and_remember(f"p{uid}", msg.media_url, msg.caption))

    now = time.time()
    if now - st.last < 2.0:  # 连发几条只回一次
        return
    st.last = now

    async with st.lock:
        context = (
            "（这是私聊，不是群里；可以比群里多说两句，但还是一样别当客服）\n"
            + render_context(list(st.messages), now=now, tz=TZ, owner_id=BOT_OWNER)
        )
        base = build_messages(
            name=PERSONA_NAME,
            context=context,
            avoid=list(st.recent)[-4:],
            sticker_tags=tuple(STICKERS) + ("最近",),
        )
        bubbles = await _generate(base)
        if not bubbles:
            return

        await asyncio.sleep(random.uniform(0.6, 1.4))
        sent: list[str] = []
        for index, (kind, value) in enumerate(bubbles.items):
            if index:
                await asyncio.sleep(random.uniform(0.5, 1.0))
            if kind == "sticker":
                path = pick_sticker(value)
                if path is None and value.strip() in _RECENT_TAGS:
                    path = LAST_STICKER.get(f"p{uid}")
                if path is None:
                    logger.warning("表情包没匹配上标签：{}（现有标签 {}）", value, "/".join(STICKERS) or "无")
                    continue
                try:
                    payload = path.read_bytes()
                except Exception:
                    continue
                try:
                    await asyncio.wait_for(
                        bot.send_private_msg(
                            user_id=int(uid), message=MessageSegment.image(payload)
                        ),
                        timeout=15,
                    )
                except Exception:
                    logger.exception("私聊发表情包失败")
                    continue
                st.messages.append(
                    Msg(
                        ts=time.time(),
                        user_id=str(bot.self_id),
                        nickname=PERSONA_NAME,
                        text="",
                        is_self=True,
                        has_media=True,
                        media_kind="sticker",
                        caption=value,
                    )
                )
                logger.info("私聊表情包 {} -> {}", uid, path.name)
                continue
            try:
                await asyncio.wait_for(
                    bot.send_private_msg(user_id=int(uid), message=value),
                    timeout=15,
                )
            except Exception:
                logger.exception("send_private_msg failed")
                break
            sent.append(value)
            st.recent.append(value)
            st.messages.append(
                Msg(
                    ts=time.time(),
                    user_id=str(bot.self_id),
                    nickname=PERSONA_NAME,
                    text=value,
                    is_self=True,
                )
            )
        if sent:
            logger.info("私聊 {} -> {}", uid, " | ".join(sent))


# --------------------------------------------------------------------------- 指令

ask = on_command("问", aliases={"ai", "ask"}, priority=5, block=True)

TOOL_PERSONA = (
    "你是一个简洁、准确的中文助手。直接给结论，需要步骤时最多 5 条，"
    "不寒暄、不复述问题、不说客套话。"
)


@ask.handle()
async def handle_ask(event: MessageEvent, args: Message = CommandArg()) -> None:
    if isinstance(event, GroupMessageEvent) and not ALLOW_GROUP_ASK:
        await ask.finish("私聊我")
    question = args.extract_plain_text().strip()
    if not question:
        await ask.finish("用法：/问 你的问题")
    try:
        answer = await chat(
            [
                {"role": "system", "content": TOOL_PERSONA},
                {"role": "user", "content": question[:4000]},
            ],
            max_tokens=1000,
        )
    except Exception:
        logger.exception("DeepSeek ask failed")
        await ask.finish("调不通模型 等会儿再试")
    await ask.finish(answer[:3500] or "模型没返回内容")


consent_command = on_command("记住我", priority=5, block=True)


@consent_command.handle()
async def handle_consent(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        await consent_command.finish("在群里发 /记住我")
    set_consent(str(event.group_id), str(event.user_id), True)
    await consent_command.finish("行 记着了。发 /忘记我 随时删。")


forget_command = on_command("忘记我", priority=5, block=True)


@forget_command.handle()
async def handle_forget_command(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        await forget_command.finish("在群里发 /忘记我")
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    set_consent(group_id, user_id, False)
    st = state_for(group_id)
    st.messages = deque((m for m in st.messages if m.user_id != user_id), maxlen=60)
    try:
        db.execute(
            "DELETE FROM messages WHERE group_id=? AND user_id=?", (group_id, user_id)
        )
        db.commit()
    except Exception:
        logger.exception("删除历史消息失败")
    await forget_command.finish("行 删了 以后不记你")


status_command = on_command("状态", priority=5, block=True)


def _fmt_ts(ts: float) -> str:
    if not ts or ts <= 0:
        return "从未"
    try:
        return datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


@status_command.handle()
async def handle_status(event: MessageEvent) -> None:
    """私聊发 /状态：看它到底收没收到消息、每次为什么没开口。"""
    if isinstance(event, GroupMessageEvent):
        await status_command.finish("私聊我发 /状态")
    uptime = int(time.time() - STATS["start"])
    hours, rest = divmod(uptime, 3600)
    lines = [
        f"运行 {hours}h{rest // 60}m｜收消息 {STATS['msg']}｜发言 {STATS['reply']}",
        f"人格 {current_style().key}｜构建 {BUILD}｜模型 {MODEL}｜回复率 {GATE.reply_rate}｜调试 {DEBUG_GATE}",
        f"表情包 {sum(len(v) for v in STICKERS.values())} 张（{'/'.join(STICKERS) or '无'}）"
        f"｜看图 {VISION_MODEL or '关'}｜已缓存 {len(_caption_cache)} 条",
    ]
    if not group_state:
        lines.append("还没收到过群消息")
    for gid, st in list(group_state.items())[:5]:
        busy = "忙" if st.task is not None and not st.task.done() else "闲"
        lines.append(
            f"群 {gid}｜缓存 {len(st.messages)} 条｜外号 {len(st.aliases)} 条"
            f"｜备忘 {len(st.diary)} 字"
            f"｜上次收到 {_fmt_ts(st.last_event)}"
            f"｜上次发言 {_fmt_ts(st.last_bot_reply)}｜{busy}"
        )
        for ts, nick, reason in list(st.log)[-5:]:
            lines.append(
                f"  {datetime.fromtimestamp(ts, TZ).strftime('%H:%M')} {nick}：{reason}"
            )
    await status_command.finish("\n".join(lines)[:1800])


if __name__ == "__main__":
    nonebot.run()