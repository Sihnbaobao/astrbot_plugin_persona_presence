# 对话线程与话题理解

> 本文说明 `utils/modules/dialogue_threads.py`、决策 AI 和回复提示词之间的分工。
> 它只描述当前实现，不代表完整的语义话题模型。

## 1. 为什么需要这一层

群聊里同一句话可能属于不同线程：

```text
A：为什么不找女朋友
B：因为是男同
C：因为找不到吧
```

这里的“为什么不找女朋友”不是新开一个公共话题，而是在接前面的对话。
如果只把当前一句交给回复模型，模型很容易把省略的主语补成“我”，
于是回复变成“你问我？我璃月……”而不是接着 A/B/C 的话题说。

线程层的目标是保存：

```text
最近谁在说话
这些消息属于哪个 thread
这个 thread 之前聊过什么
```

## 2. 数据与生命周期

`DialogueThreads` 每个群保存最近 24 条 turn。

每条 turn 记录：

```text
sender_id / sender_name
text / time
reply_to_other / at_other
thread_id
is_bot
```

每个 thread 保存：

```text
最近 24 条 turn
最近一次话题归纳 summary
```

线程划分是启发式，不调用模型：

```text
回复/@ 别人 -> 接到上一条 thread
短反应（<=8 字）-> 继承上一条 thread
时间超过 180 秒 -> 新 thread
和当前 thread 有中文 bigram 重合 -> 同一 thread
其余 -> 新 thread
```

## 3. 决策与回复的分工

决策 AI 只负责：

```text
reply=yes/no
target=bot/other/open
participation=direct/side/open/none
```

它不负责写回复，也不负责解释话题。

回复前另有一次轻量话题归纳（`_summarize_group_topic`）：

```text
输入：
  这个 thread 之前的 summary
  thread 最近 12 条原话
  formatted_context 的尾部（更早历史）
  当前消息

输出：
  一句 [话题] 现在在聊谁/什么事，前面说到哪，当前这句在接什么
```

结果会：

```text
注入正式回复上下文
写入 thread.summary，供下一轮继续更新
注入补一句上下文
```

归纳有 8 秒超时，失败就跳过，不阻塞回复；同一群的同一话题会缓存。

## 4. 它解决什么

```text
省略主语的追问（为什么不找女朋友）
跨多轮的同一话题
当前这句是在接谁
回复不要把话题主语换成自己
```

## 5. 它不解决什么

```text
真正的语义 topic model
跨进程/重启持久化
多人快速交叉、反讽、省略的深层指代
超过 24 条 turn 的完整线程
```

线程和 summary 都是内存态；进程重启后会重新积累。
更早的历史只能依赖 `formatted_context` 和外部记忆插件，不会自动恢复成线程。

## 6. 相关代码

- `utils/modules/dialogue_threads.py`：turn、thread、summary
- `main.py::_summarize_group_topic`：回复前的话题归纳
- `main.py::_prepare_reply_context`：注入 `[话题]`
- `utils/decision_ai.py`：只看参与判断，不看回复正文
