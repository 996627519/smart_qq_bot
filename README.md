# Smart QQ Bot

把 QQ 小号变成群友的聊天机器人：**NapCat + OneBot V11 + NoneBot2 + DeepSeek**。

它不是一个"问答机器人"，而是一个尽量像真人的群友：会接梗、会分条发消息、会发表情包、
能看懂别人的图和动图，还记得住群里谁是谁、最近发生过什么。

---

## 目录

- [功能](#功能)
- [架构](#架构)
- [目录结构](#目录结构)
- [前置条件](#前置条件)
- [部署](#部署)
- [第一次使用](#第一次使用)
- [配置项](#配置项)
- [命令](#命令)
- [故障排查](#故障排查)
- [数据与备份](#数据与备份)
- [本地自检](#本地自检)
- [安全与风控](#安全与风控)

---

## 功能

| 能力 | 说明 |
| --- | --- |
| 群聊/私聊 | 群里每条都接，私聊直接聊；按人格说话 |
| 分条发送 | 长一点的话题会拆成 2~3 条发，条间有停顿 |
| 发表情包 | 表情库全部来自群友，自动收录、自动打标签；支持 `<表情:最近>` 原样重发 |
| 看懂图片 | 接多模态模型，把图翻译成一句话再进上下文 |
| 看懂动图 | 本地抽 3 帧，一次请求发给模型 |
| 长期记忆 | 外号表、长期备忘、原文库按名字召回 |
| 自动学群梗 | 每 40 条消息提炼一次黑话/固定玩笑 |
| 命令 | `/状态`、`/问`、`/记住我`、`/忘记我` |

---

## 架构

```
 QQ 客户端
    │
    ▼
 NapCat（Docker）── WebSocket :3001 ──► bot（Docker）
                                         │
                                         ├─► DeepSeek API（对话，文本）
                                         └─► 智谱 GLM-4V（看图，可选）
```

- NapCat 负责登录 QQ、收发消息，通过 OneBot V11 协议把事件推给 bot。
- bot 是一个 NoneBot2 进程，收到消息后决定说什么、发什么，再通过 NapCat 发回群里。
- 两个容器在同一个 Docker 网络 `qq-deepseek-botnet` 里，按服务名互相访问。
- NapCat 的 WebUI 端口只绑在 `127.0.0.1`，外网访问不到，需要 SSH 隧道。

---

## 目录结构

```
smart_qq_bot/
└── qq-deepseek/              # compose 项目目录（所有命令都在这里执行）
    ├── compose.yaml          # 两个服务：napcat + bot
    ├── .env                  # 所有密钥和配置（不要提交到 git）
    ├── bot/
    │   ├── Dockerfile
    │   ├── requirements.txt
    │   ├── bot.py            # 接线：收消息、防抖、调模型、发送、记忆
    │   ├── persona.py        # 人格、发言判断、上下文渲染、输出清洗
    │   └── selftest.py       # 离线自检，不连 QQ 不花钱
    ├── stickers/             # 表情包（运行时自动创建）
    │   ├── auto/             # 从群里收录的图
    │   └── index.json        # 标签、描述、使用次数
    └── data/
        ├── qq/               # NapCat 的 QQ 登录态
        ├── config/           # NapCat 配置
        ├── plugins/
        └── bot/
            └── bot-memory.sqlite3   # 记忆数据库
```

`stickers/` 和 `data/` 是运行时生成的，删了会丢登录态和记忆，别乱动。

---

## 前置条件

| 必需 | 说明 |
| --- | --- |
| Linux 服务器 | 2C2G 起步，能装 Docker |
| Docker + Compose v2 | `docker compose version` 能输出版本号 |
| QQ 小号 | 能扫码登录，建议先养几天号 |
| DeepSeek API Key | 官方或兼容中转都行 |

| 可选 | 说明 |
| --- | --- |
| 智谱 API Key | 免费，用来看图；不填也能跑，只是看不懂图 |

---

## 部署

### 1. 放文件

把 `qq-deepseek/` 整个目录传到服务器，比如 `/opt/qq-deepseek`。

```bash
cd /opt/qq-deepseek
ls    # 应该看到 compose.yaml、.env、bot/
```

### 2. 填 `.env`

```dotenv
# ---- 对话模型（必填）----
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_MODEL=deepseek-v4-flash

# ---- 身份（强烈建议填）----
BOT_NAME=小鲸鱼
BOT_ALIASES=小鲸鱼,大肥鱼,蓝色大肥鱼,D指导
BOT_OWNER=你的QQ号
BOT_TZ=Asia/Shanghai

# ---- 人格 ----
PERSONA_STYLE=whale

# ---- 看图（可选，智谱免费）----
VISION_MODEL=glm-4v-flash
VISION_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
VISION_API_KEY=你的智谱key

# ---- NapCat ----
BOT_ACCESS_TOKEN=随便一串随机字符串
WEBUI_TOKEN=另一串随机字符串
```

几个要点：

- `BOT_NAME` 是它对自己的称呼；`BOT_ALIASES` 是群友叫它的外号，**不填就识别不到"在叫它"**。
- `BOT_ACCESS_TOKEN` 和 NapCat 里的 token 必须一致，否则连不上。
- 这两串 token 用 `openssl rand -hex 32` 生成。
- `.env` 里不要有引号、不要有行尾注释，等号两边不要空格。

### 3. 构建并启动

```bash
docker compose up -d --build
```

首次构建要装依赖，大概 1~3 分钟。看日志：

```bash
docker compose logs -f bot
```

正常应该看到：

```
构建=2026-11-01d 配置：name=小鲸鱼 人格=whale 回复率=1.0 ...
表情包 0 张，标签 -｜看图 开（glm-4v-flash）
OneBot V11 | Bot 123456789 connected
```

最后一行出现，说明 bot 和 NapCat 连上了。

### 4. 登录 QQ

如果 `data/qq/` 是空的，NapCat 需要扫码登录。

```bash
docker compose logs -f napcat
```

日志里会打印一个二维码（终端字符画），用手机 QQ 扫码即可。

扫码不方便的话，用 WebUI：

```bash
# 在你自己电脑上执行，把服务器端口映射到本地
ssh -L 6099:127.0.0.1:6099 用户名@服务器IP
```

然后浏览器打开 <http://127.0.0.1:6099>，token 填 `.env` 里的 `WEBUI_TOKEN`。

### 5. 确认 WebSocket 配置

登录后，在 WebUI 的「网络配置」里确认有一个 **WebSocket 服务器**：

- 端口 `3001`
- token 与 `.env` 里的 `BOT_ACCESS_TOKEN` 一致

不一致就改过来，保存后重启 NapCat：

```bash
docker compose restart napcat
```

### 6. 验证

私聊机器人发：

```
/状态
```

能收到运行时长、收消息数、当前人格，就说明整条链路通了。

---

## 第一次使用

1. **把小号拉进群**（或者用群主号拉）。它进群后不会立刻说话，因为还没收到消息。
2. **在群里 @ 它** 说句话，比如 `@小鲸鱼 在吗`。
3. 它会回你，日志里能看到：

```
收到 123456｜你的昵称：在吗  -> chat
发言 123456 -> 在 怎么了
```

4. **发张表情包** 试试，它会收录并看图：

```
看图结果：猫咪眼神专注...
收录表情包 ba197f66.jpg（疑惑）猫咪眼神专注...
```

5. **过几天再问它** "老王是谁"，如果群里提过，它答得上来——这是长期记忆在起作用。

---

## 配置项

全部写在 `.env`，改完 `docker compose up -d` 生效。

### 模型

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无 | 必填 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 对话模型 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 中转站改这里 |
| `DEEPSEEK_CONCURRENCY` | `3` | 并发上限 |
| `CHAT_TEMPERATURE` | `1.3` | 越高越跳脱，觉得油就降到 1.15 |
| `CHAT_MAX_TOKENS` | `120` | 单条回复长度上限 |

### 身份

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BOT_NAME` | 空 | 它对自己的称呼 |
| `BOT_ALIASES` | 空 | 外号，逗号分隔，**每个至少 2 个字** |
| `BOT_OWNER` | 空 | 主人的 QQ，会被标成"（主人）" |
| `BOT_TZ` | `Asia/Shanghai` | 服务器是 UTC 时一定要设 |
| `PERSONA_STYLE` | `whale` | `whale` 小鲸鱼 / `normal` 普通人 / `cute` 萌系 / `tieba` 贴吧老哥 |

### 行为

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `REPLY_RATE` | `1` | 每条消息回复的概率，想收敛就 0.6~0.8 |
| `IGNORE_AT_OTHERS` | `1` | 别人在 @ 别人时不插嘴 |
| `REPLY_DEBOUNCE_MIN` / `_MAX` | `0.8` / `1.8` | 先停顿多久再回（秒） |
| `MULTI_MSG_RATE` | `0.45` | 连发多条的概率 |
| `MULTI_MSG_MAX` | `3` | 一次最多几条 |
| `ALLOW_PRIVATE_CHAT` | `1` | 私聊能不能闲聊 |
| `ALLOW_GROUP_ASK` | `0` | `/问` 是否允许在群里用 |

### 表情包与看图

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `STICKER_ENABLED` | `1` | 是否启用表情包 |
| `STICKER_DIR` | `/app/stickers` | 对应 compose 里的挂载 |
| `VISION_MODEL` | `glm-4v-flash` | 视觉模型 |
| `VISION_BASE_URL` | 智谱地址 | OpenAI 兼容接口 |
| `VISION_API_KEY` | 空 | 不填就不看图 |

### 记忆

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GROUP_NOTES_ENABLED` | `1` | 是否提炼群梗/外号/备忘 |
| `GROUP_NOTES_EVERY_MESSAGES` | `40` | 每多少条更新一次 |
| `GROUP_NOTES_MIN_MESSAGES` | `25` | 少于这么多条不更新 |
| `ALIAS_ENABLED` | `1` | 是否抽外号 |

### 调试

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEBUG_GATE` | `1` | 每条消息打一行"收到 + 判定"，嫌吵设 `0` |
| `BOT_DB_PATH` | `/app/data/bot-memory.sqlite3` | 数据库路径 |

---

## 命令

| 命令 | 场景 | 作用 |
| --- | --- | --- |
| `/状态` | 私聊 | 运行时长、收消息数、表情包数量、各群记忆概况 |
| `/问 问题` | 私聊（群里需 `ALLOW_GROUP_ASK=1`） | 走"工具模式"，直接给答案 |
| `/记住我` | 群聊 | 恢复自己的长期记忆 |
| `/忘记我` | 群聊/私聊 | 删除自己的历史消息，此后不再记录 |

`/状态` 是最有用的：它能看到每个群的 `缓存 / 外号 / 备忘` 和最近 5 条判定原因。

---

## 故障排查

### 构建失败

```
ERROR: Could not find a version that satisfies the requirement ...
```

- 网络问题：Dockerfile 里已经用了清华源，海外服务器可以换回官方源。
- 依赖写错：`requirements.txt` 里四行，版本号别乱改。

### 起来了但报 ModuleNotFoundError

```
ModuleNotFoundError: No module named 'persona'
```

Dockerfile 里是 `COPY *.py .`，确保 `persona.py` 在 `bot/` 目录下，并且重新 `--build`。

### bot 日志没有 "Bot connected"

1. `docker compose logs napcat` 看 NapCat 是否登录成功；
2. WebUI 里 WebSocket 端口的 token 是否等于 `BOT_ACCESS_TOKEN`；
3. 两个服务是否在同一个网络：

```bash
docker compose exec bot python -c "import socket;socket.create_connection(('napcat',3001),3);print('通')"
```

### 群里不回消息

先私聊 `/状态` 看两行：

- **收消息数不涨** → 消息没进来。检查小号是否还在群里、是否被禁言、NapCat 是否掉线。
- **收消息涨、发言不涨** → 看 `/状态` 里的判定原因，或者把 `DEBUG_GATE` 打开看日志。

### 看不懂图

| 日志 | 处理 |
| --- | --- |
| 启动第二行 `看图 关（没填 VISION_API_KEY）` | 补 key |
| `图片下载失败：HTTP Error 403` | 容器出不了网，或图片链接过期 |
| `视觉模型调用失败` + 401 | key 错，或 `VISION_BASE_URL` 不匹配 |
| `动图抽帧失败（没装 Pillow？）` | `requirements.txt` 里加 `pillow` 后重建 |

### 表情包一直是 0 张

- 只有别人**发表情包**才会收录，普通照片不算；
- 动图超过 3MB 不收；
- `/status` 里看 `收录失败，下载不了` 说明是网络问题。

### 它不记得外号

- 记忆是**攒出来**的：每 40 条消息才提炼一次，刚部署时是空的；
- 外号必须是群里**明确说过**的（"老王就是老白"），模型不会瞎猜。

### 它一直重复同一句话

`CHAT_TEMPERATURE` 调高到 1.4，或者把 `MULTI_MSG_RATE` 降下来减少复读概率。

---

## 数据与备份

| 路径 | 内容 | 丢了会怎样 |
| --- | --- | --- |
| `data/qq/` | QQ 登录态 | 要重新扫码 |
| `data/bot/bot-memory.sqlite3` | 记忆库 | 群梗、外号、备忘、原文全没 |
| `stickers/` | 收录的表情包 | 表情库清空 |

备份：

```bash
docker compose stop bot
tar czf backup-$(date +%F).tar.gz data/ stickers/ .env
docker compose start bot
```

恢复时解压回原位即可。

---

## 本地自检

不连 QQ、不花钱，直接看"什么话它接、接成什么样"：

```bash
cd bot
python selftest.py
```

`selftest.py` 只依赖 `persona.py`（纯标准库），**不需要装任何依赖**。
它会回放一段群聊，打印每条消息的判定结果、清洗效果和最终 prompt 结构。
改完 `persona.py` 先跑这个，比部署上去试快得多。

---

## 安全与风控

- **`.env` 不要提交到 git**，里面有 API key 和 token。仓库根目录的 `.gitignore` 已经把它排除了；
  如果之前已经提交过，执行 `git rm --cached qq-deepseek/.env` 并**换掉那两个 token**（历史里还能翻到）。
- WebUI 端口只绑了 `127.0.0.1`，别改成 `0.0.0.0` 暴露到公网。
- 小号容易被风控，建议：
  - 新号先正常聊几天再上机器人；
  - `REPLY_RATE` 不要长期 1.0，群里人多时降到 0.6；
  - 不要在群里发链接、发政治/色情内容。
- 隐私：群友发 `/忘记我` 会删掉他的历史消息且不再记录；`memory_consent` 表记录了这个开关。
- 人格卡里有"开车""调情"之类的设定，模型会掌握分寸，但**请自行确认所在群的接受度**，被举报的是你的号。

---

## 升级

```bash
# 覆盖 bot.py / persona.py 后
docker compose up -d --build
docker compose logs -f bot | head -3
```

启动日志第一行带构建号（`构建=2026-11-01d`），**看它有没有变**——没变就说明镜像没重建成功，
这是排查"改了没生效"最快的办法。
