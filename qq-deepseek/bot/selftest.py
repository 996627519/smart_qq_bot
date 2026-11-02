#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project ：smart_qq_bot 
@File ：selftest.py
@Author ：zlh
@Date ：2026-11-02 20:16 
"""
"""selftest.py — 不连 QQ、不花钱，直接看"什么话它接、什么话它闭嘴、接成什么样"。

用法：python selftest.py
调完 persona.py / 环境变量里的概率，重跑一遍看效果。
"""

from __future__ import annotations

import random
import sys
from datetime import datetime

try:  # Windows 控制台默认 GBK，直接打中文/emoji 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from persona import (
    Cleaned,
    GateConfig,
    Msg,
    build_messages,
    clean_reply,
    decide,
    is_duplicate,
    local_tz,
    render_context,
)

TZ = local_tz("Asia/Shanghai")
BOT_ID = "10001"
BOT_NAMES = ("小号", "半仙")
START = datetime(2026, 5, 20, 21, 0, tzinfo=TZ).timestamp()

# (距上一条多少秒, 昵称, QQ, 内容, 有没有@bot)
SCRIPT = [
    (0, "老白", "20001", "今天这排位打得我想死", False),
    (40, "阿祖", "20002", "又连跪了？", False),
    (25, "老白", "20001", "六把 一把没赢", False),
    (90, "小圆", "20003", "有没有人知道这赛季啥时候结束", False),
    (15, "阿祖", "20002", "月底吧", False),
    (30, "老白", "20001", "草", False),
    (50, "阿祖", "20002", "小号你说这版本射手还能玩吗", False),
    (20, "小圆", "20003", "@小号 你上次那个耳机在哪买的", True),
    (10, "老白", "20001", "早", False),
    (600, "老白", "20001", "有人吗", False),
    (15, "阿祖", "20002", "@阿祖 你昨天那个链接发我", False),
    (20, "小圆", "20003", "算了 不问了", False),
    (25, "老白", "20001", "这游戏是不是要凉了", False),
    (30, "阿祖", "20002", "凉不了 还有人充钱呢", False),
    (20, "老白", "20001", "@小号 你是不是机器人啊", True),
]


def run_script() -> None:
    cfg = GateConfig()
    rng = random.Random(7)
    history: list[Msg] = [
        Msg(ts=START - 120, user_id="20002", nickname="阿祖", text="今晚有人开黑吗"),
        Msg(ts=START - 60, user_id="20003", nickname="小圆", text="我来 等我吃完饭"),
    ]
    last_bot_reply = -1e9
    reply_times: list[float] = []
    now = START

    print("=" * 78)
    print("发言决策回放（cfg 用 persona.py 默认值，随机种子固定）")
    print("=" * 78)
    for gap, nick, uid, text, at_bot in SCRIPT:
        now += gap
        msg = Msg(
            ts=now,
            user_id=uid,
            nickname=nick,
            text=text,
            at_ids=[BOT_ID] if at_bot else [],
            msg_id=f"m{len(history)}",
        )
        history.append(msg)
        recent = history[-40:]
        decision = decide(
            msg,
            recent,
            now=now,
            cfg=cfg,
            last_bot_reply=last_bot_reply,
            reply_times=reply_times,
            bot_id=BOT_ID,
            bot_names=BOT_NAMES,
            rng=rng,
        )
        mark = "说" if decision.should else "  "
        stamp = datetime.fromtimestamp(now, TZ).strftime("%H:%M")
        print(f"{stamp} [{mark}] {nick}：{text}   -> {decision.reason}")

        if decision.should:
            reply = "……（这里省略模型生成）"
            history.append(
                Msg(ts=now + 3, user_id=BOT_ID, nickname="小号", text=reply, is_self=True)
            )
            last_bot_reply = now + 3
            reply_times.append(now + 3)


def run_clean() -> None:
    print()
    print("=" * 78)
    print("输出清洗")
    print("=" * 78)
    samples = [
        "作为一个AI，我很乐意帮你分析这个问题～😊",
        "两百就随便买，这价位都一样，挑个顺眼的。",
        "首先，你需要打开设置；其次，修改参数。",
        "哈哈哈😄",
        "好的，我来帮你看看",
        "确实呢～",
        "open 一下的事 用完记得 close\n记得加 try",
        "抱歉，我无法回答这个问题。",
        "我看了下 这版本射手确实坐牢",
    ]
    for raw in samples:
        result: Cleaned = clean_reply(raw)
        if result.ok:
            print(f"保留 {result.bubbles}")
        else:
            print(f"丢弃（{result.reason}）  <- {raw!r}")

    print()
    history = ["挂机大厅 进去就是看别人发呆", "两百就随便买 这价位都一样"]
    for text in ["挂机大厅 进去就是看别人发呆", "进去就是看别人发呆", "两百随便买 都一样"]:
        print(f"重复检测 {text!r}: {is_duplicate(text, history)}")


def run_prompt() -> None:
    cfg = GateConfig()
    now = START + 400
    entries = [
        Msg(ts=now - 180, user_id="20001", nickname="老白", text="今天这排位打得我想死"),
        Msg(ts=now - 140, user_id="20002", nickname="阿祖", text="又连跪了？"),
        Msg(ts=now - 120, user_id=BOT_ID, nickname="小号", text="六把一把没赢 别打了", is_self=True),
        Msg(ts=now - 60, user_id="20003", nickname="小圆", text="有没有人知道这赛季啥时候结束"),
        Msg(ts=now - 3, user_id="20003", nickname="小圆", text="你不是说月底吗", at_ids=[BOT_ID]),
    ]
    context = render_context(entries, now=now, tz=TZ, bot_id=BOT_ID)
    print()
    print("=" * 78)
    print("上下文渲染")
    print("=" * 78)
    print(context)
    messages = build_messages(name="小号", context=context, notes="连跪=日常；月底=赛季结算")
    print()
    print(f"messages 条数：{len(messages)}，最后一条 user 内容：")
    print("-" * 78)
    print(messages[-1]["content"])
    _ = cfg


if __name__ == "__main__":
    run_script()
    run_clean()
    run_prompt()
