# Persona Presence 项目结构

本文描述当前仓库 astrbot_plugin_persona_presence 的源代码边界。被删除的旧版主动对话、情绪、注意力、随机回复和独立 Web 面板模块不属于当前架构。

## 目录结构

astrbot_plugin_persona_presence/
├── main.py                         插件入口和事件编排
├── metadata.yaml                   插件 ID、展示名和版本
├── _conf_schema.json               AstrBot 配置页与插件页字段定义
├── README.md                       使用说明
├── CHANGELOG.md                    版本记录
├── requirements.txt                可选依赖
├── logo.png                        插件图标
├── data/                           模板和命令配置
├── pages/control/                  AstrBot Plugin Pages 管理页面
├── utils/                          功能域模块
├── tests/                          回归测试
└── docs/                           架构、流程、配置和兼容文档

## 主入口

main.py / PersonaPresence 负责：

- 注册 astrbot_plugin_persona_presence 和生命周期钩子；
- 读取、校验和同步配置；
- 执行群聊/私聊范围、黑名单、命令、重复和媒体边界；
- 提取当前发送者、机器人目标、其他用户目标和 Smart 批次；
- 调用 DecisionAI.evaluate；
- 应用 ParticipationThrottle；
- 调用 ReplyHandler 生成、发送和保存正式回复；
- 维护 observation-only 缓存和插件页 API。

main.py 不承担正式回复正文的人格生成，也不应重新引入随机概率或“命中即必回”的旁路。

## 核心 utils

| 文件 | 责任 |
| --- | --- |
| decision_ai.py | 构造参与判断请求、注入当前 Persona、解析结构化 JSON，并兼容旧 yes/no。 |
| participation.py | 校验不可信模型字段、校验消息对象和说话姿态、生成最小 handoff、限制主动参与频率；不替 Persona 做主观兴趣或连续性裁决。 |
| reply_handler.py | 解析当前会话 Persona，创建 AstrBot provider request，复用正式回复和 Hook 边界。 |
| message_cache_manager.py | 管理待处理消息、过期和数量限制；排除 decision_state=observed 的 active 读取。 |
| smart_concurrent_manager.py | 按到达序号选择 anchor，吸收 follower，维护有界 Smart 批次。 |
| context_manager.py | 格式化消息上下文、同步官方历史、管理缓存转正。 |
| message_processor.py | 提取、清洗和标注发送者、时间、At、引用和媒体消息。 |
| message_cleaner.py | 清理运行时标记、解析原始消息链和媒体引用。 |
| image_handler.py | 图片、视频、语音、文件和描述信息处理。 |
| image_description_cache.py | 缓存图片描述，减少重复识别。 |
| emoji_detector.py | 识别纯表情和贴图。 |
| keyword_checker.py | 匹配触发关键词和黑名单关键词；触发词只提高注意力。 |
| mention_processor.py | 处理 @机器人、@全体和 @其他用户边界。 |
| poke_processor.py | 处理戳一戳过滤、反戳和回复后戳。 |
| command_processor.py | 处理指令过滤和图片缓存清理命令。 |
| save_processor.py | 保存用户消息、正式回复和批次历史。 |
| memory_injector.py | 可选接入 livingmemory，记忆只作背景。 |
| platform_ltm_helper.py | 平台图片描述和媒体辅助逻辑。 |
| ai_response_filter.py | 过滤和兼容判断模型响应；正式回复仍走独立过滤链路。 |
| ai_error_formatter.py | 将 provider 错误转为日志和诊断文本。 |
| probability_manager.py | 保留会话 key、reset/status 和旧接口兼容，不再作为当前群聊回复闸门。 |

## 配置与插件页

_conf_schema.json 的分组与字段由 AstrBot 配置页读取。pages/control/ 是内嵌插件页，不再启动独立 HTTP 面板。

插件页后端 API 路由前缀为 /astrbot_plugin_persona_presence：

| 路由 | 方法 | 责任 |
| --- | --- | --- |
| /status | GET | 返回插件版本、启用状态、参与预算和运行时统计。 |
| /config/save | POST | 保存 schema 允许的配置并同步实例状态。 |
| /prompts | GET | 展示参与判断和正式回复的当前提示拼接。 |

## 数据状态

ParticipationDecision 是一次判断的不可变结果，至少包含 reply、target、continuation、participation、information、interest、reason_code、confidence 和 topic_key。

被拒绝消息写入缓存时添加 decision_state=observed。MessageCacheManager 的 active、regular、window 和图片候选读取都排除它，避免被拒绝消息制造续话、目标或下一次视觉处理。

Smart anchor 是主要回复对象。follower 只提供背景；无论是否通过，发送者和到达顺序都不能被覆盖。

## 测试

tests/test_persona_presence.py 覆盖：

- 正式回复与参与判断 prompt 边界；
- direct/open/side/other 的参与硬规则；
- JSON 结构化解析、未知枚举和旧 yes/no 兼容；
- handoff 不泄露分析过程；
- observation-only 缓存隔离；
- open/side 预算 interval、window、cap 与 direct bypass；
- 关键词、缓存和 Smart 相关回归。

其他 tests 目录测试图片、上下文持久化、群聊目标识别和 Smart 到达顺序。

推荐在仓库根目录执行：

    /home/ubuntu/AstrBot/.venv/bin/ruff format .
    /home/ubuntu/AstrBot/.venv/bin/ruff check .
    /home/ubuntu/AstrBot/.venv/bin/python -m compileall -q .
    /home/ubuntu/AstrBot/.venv/bin/pytest -q

相关文档：[README](../README.md) · [架构指南](ARCHITECTURE.md) · [消息流程](MESSAGE_WORKFLOW.md) · [配置参考](CONFIG_REFERENCE.md) · [重构设计](REFACTOR_DESIGN.md) · [桌面端兼容](DESKTOP_COMPATIBILITY.md)
