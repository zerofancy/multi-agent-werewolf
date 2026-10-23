# 完整时间线 HTML

## 生成与检查

将实际对局整理为以下规范化 JSON，再运行：

```bash
python3 <skill目录>/scripts/render_replay.py <对局目录>/replay.json --validate-only
python3 <skill目录>/scripts/render_replay.py <对局目录>/replay.json --output <交付目录>/狼人杀复盘.html
```

需要 Python 3.10 或更高版本，无第三方运行依赖。脚本只负责结构、原文覆盖和离线排版，不负责从未发生的行动推演对局，不代替法官规则结算或全局总结投递。规范化文件及完整原始日志由法官保存。

离线模板直接输出所有正文，不依赖 JavaScript 或网络。实时模式使用单独的模板与同源脚本轮询，操作见 [live-view.md](live-view.md)。生成后检查锚点有效、全部正文可见、没有内层剧情滚动区，桌面与约 390px 窄屏无横向溢出。无需做整页超长截图，检查开局、一次私聊、一次票型和终局边界即可。

生成器保留数组顺序，但不能证明传话和结算正确。法官须核对：对白逐人广播发布后才调度下一人；每轮私票使用同一截止快照、不泄漏先票、全部锁定才统一公告；警长权重及选民资格正确；药量、刀口权限、发言环形顺序和冻结的终局优先级一致；总结确实投递并获回执。历史局按原规则保留，不回填新规则。

## 数据结构

顶层字段：

| 字段 | 内容 |
| --- | --- |
| `title` | 对局标题 |
| `subtitle` | 可选简介 |
| `winner` | 可选最终胜方；开局人物卡不必提前剧透 |
| `rules` | 实际冻结规则的字符串数组 |
| `ruleset` / `voting_mode` | 新局用 `classic-12-guard-v1` / `sealed`；发布后不可修改。旧局可缺省 |
| `players` | 人物数组，字段见下例；`role` 是真实游戏身份，`persona.identity` 是公开人物身份 |
| `records` | 每条原始 Agent 回复的规范化内容，保留原文与 ID，用于覆盖检查 |
| `chapters` | 按实际先后排列的场景数组；内部 `events` 也按实际顺序排列 |
| `postgame` | 赛后全局总结 ID 和入选感言作者的实际收取回执 |

实时模式中 `records`、`chapters` 可以从空数组起步，每次只提交已确认的完整记录与事件。未完成的收取进度另存运行状态，不能削弱原文恰好覆盖一次的校验。章节标题发布后冻结。`summary` 是旧局原始元数据，不显示为章首长摘要；新局可省略。实际法官公告和结果用带真实接收者的事件逐条追加。

单个玩家：

```json
{"seat":1,"name":"林鸮","avatar":"🦉","persona":{"identity":"档案管理员","personality":"审慎，愿意修正判断","speaking_style":"先列证据，再给暂定结论"},"role":"平民","camp":"good"}
```

`camp` 取 `wolves` 或 `good`；人数由当前对局确定，生成器不把人数写死为 12。

原始回复的规范化记录：

```json
{
  "id":"p01-D1-speech",
  "actor":1,
  "phase":"D1_speech",
  "public":"我先把两条线索分开。",
  "private":"目前依据不足，暂不把怀疑当成事实。"
}
```

可包含的内容字段为 `public`、`team_message`、`private`、`actions`。前三项为原始字符串，`actions` 为本阶段真实行动对象，例如 `{"vote":2}`。默认锁票用 `actions: {"vote": 2}`、`action/judge_private`、接收者仅本人，理由只用 `private/review`。`voting_mode=sealed` 拒绝含public/team_message的投票记录及公开源投票事件。仅兼容旧局或明确另设的公开唱票模式允许纯vote来源用all；其他能力或混合行动始终私交法官。没有的内容就省略，不补台词。原始调用元数据另存法官日志。

每个内容字段都要由一个时间线事件的 `source` 引用一次。同一回复的公开发言与内心判断必须拆成两个事件；如果有狼队私聊与行动，也分别拆开。`kind=action` 事件也必须有字符串 `text`（可为空），不应只填写行动来源。`actions` 对象作为一次完整行动记录展示；生成器自动将每个实际动作列在正文，防止说明文漏写目标或药物选择。

场景与事件：

```json
{
  "id":"D1",
  "title":"第1天",
    "events":[
    {
      "id":"e-001",
      "kind":"scene",
      "actor":0,
      "channel":"all",
      "recipients":[1,2],
      "title":"法官公开指令",
      "text":"天亮了，昨夜无人死亡。请1号先发言，其余玩家依座位顺序接续。",
      "origin":"reconstructed"
    },
    {
      "id":"e-002",
      "kind":"dialogue",
      "actor":1,
      "channel":"all",
      "recipients":[1,2],
      "title":"首轮发言",
      "text":"我先把两条线索分开。",
      "origin":"verbatim",
      "source":{"record_id":"p01-D1-speech","fields":["public"]}
    },
    {
      "id":"e-003",
      "kind":"thought",
      "actor":1,
      "channel":"review",
      "recipients":[],
      "title":"内心判断，未传话",
      "text":"目前依据不足，暂不把怀疑当成事实。",
      "origin":"verbatim",
      "source":{"record_id":"p01-D1-speech","fields":["private"]}
    }
  ]
}
```

上例仅说明结构，不是一局完整游戏，座位与投递对象必须按实际对局填。`actor=0` 保留给法官，玩家座位为正整数。

## 范围、来源和场景顺序

| 字段 | 值及约束 |
| --- | --- |
| `kind` | `scene`、`dialogue`、`action`、`thought`、`summary`、`reflection` |
| `channel` | `all` 全体公开；`wolves` 狼队私聊；`judge_private` 与法官私聊；`review` 未传话内心或法官复盘真相 |
| `recipients` | 当时实际收到的有权玩家座位；法官不占座位。`all` 必须非空，不能把无人接收的旁白冒记为公开事件。`review` 必须为空；私交法官的行动只含行动者；法官私告查验只含预言家；狼队私聊列实际允许的狼人子集 |
| `origin` | 原话、原始内心文本为 `verbatim`；行动呈现为 `recorded_action`；法官串联文字为 `reconstructed`，不能冒充原话 |
| `source` | 可选来源，`record_id` 指向 `records.id`，`fields` 为本事件覆盖的内容字段；原话和行动必须关联，法官整理文字不冒用玩家来源 |
| `facts` | 可选字符串数组，用于列票型、死亡、能力和存活人数；范围继承所在事件 |

初投、复投分别写独立的 `summary`，先列票数与弃票，再宣布是否有人出局。初投平票不能提前显示复投出局。行动、回复、查验结果以实际提交或投递先后排序；公开对白严格串行；各自独立锁票可按工具能力串行收取，共用本轮公开截止快照，先票不传给后者，不事后将并行输出剪成有前后因果的对话。旧局同时提交的事实仍保持原样。

一条公开公告不能夹带未公开的真实死因或剩余神职。只显示实际公开的死亡、警徽、发言顺序和统一票型公告与各自原始行动、真实私聊；不另插未向任何人发送的全知夜间结算或流程总结。赛内身份宣称保留原话，不转成法官认证。法官整理的实际发牌、公告和私告用 `reconstructed`，不伪造逐字指令。渲染器不显示章节 `summary`，也不显示 `actor=0, channel=review, kind=scene/summary` 的未投递旁白；原始数据仍保留。玩家的真实 `thought`、全部原话与行动不受此过滤影响。

## 赛后数据

```json
{
  "summary_id":"postgame-global-v1",
  "reviewers":[
    {"seat":2,"side":"winner","selection_reason":"参与关键对跳和定刀","received_summary_id":"postgame-global-v1"},
    {"seat":8,"side":"winner","selection_reason":"观察另一条狼队策略线","received_summary_id":"postgame-global-v1"},
    {"seat":3,"side":"loser","selection_reason":"终局技能执行者","received_summary_id":"postgame-global-v1"}
  ]
}
```

这段作为顶层 `postgame`。实际总结全文放在终局之后的一个法官 `summary` 事件中，按实际投递使用 `judge_private` 加入选接收者，或确实向全体广播时使用 `all`；不要把已投递总结误标为无人接收的 `review`；投递记录和回执也按实际发生顺序用法官整理事件标明。`received_summary_id` 来自对应玩家真实回复，不能仅因为计划发送就预填。

感言原文作为新的 `records` 中 `public` 字段，附在 `kind=reflection`、`channel=all`、`origin=verbatim` 的事件里，放在全局总结和投递记录之后。感言作者须与入选名单对应；默认 2 胜 1 败。人数不足或无法续接时解释实际调整，不生成假回执或假感言。

生成器会拒绝缺字段覆盖、重复覆盖、原文改变、错误可见范围、不合法引用、私聊对象错误或全局总结回执不匹配等数据。它不能证明日志真实性或从模板断言胜负，法官仍须对照原始记录核对关键结算和实际工具调用。

## 身份标签、警长与独立锁票

人物卡、每条玩家事件和感言作者名字旁仍显示真实身份，仅为观众全知标注，不进入玩家请求。警长是公开职务，竞选、当选、传徽、撕徽、方向与归票均用实际行动/公告事件追加，不修改players.role，不回填旧事件的职务状态。

```json
{"id":"p01-D1-vote","actor":1,"phase":"D1_vote","actions":{"vote":2},"private":"我锁定2号，理由只交法官。"}
```

该回复拆成action/judge_private/[1]和thought/review/[]，两字段各覆盖一次，立即publish。记录自己的锁票不表示其他玩家已知。票箱ID、公共截止快照ID、票权和调用原件另存法官目录。统一公告由法官all/summary另写完整选民、投向、权重、加权票数、弃票和结果；收齐前不得提前公告部分票。

规范化actions中的candidate、withdraw、direction、badge_target、explode等按真实阶段保存，私交法官；之后实际公布的结果另加公告。警上竞选发言和警长归票的public原话仍逐字呈现。旧局公开顺序票仍可合法渲染，但不得补写sealed元数据伪装成独立投票。
