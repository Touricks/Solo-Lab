# Claude 适配交接

Codex 一侧在 **0.5.1-codex.2** 交接时（分支 `codex/desktop-adapter`），给 Claude 一侧留了六项要调整的流程。这份文档记这六项在 **0.6.0** 里各自怎么处理、落在哪里。Codex 交接时的实测基线见 [Codex 实测修复记录](Codex实测修复.md)。

0.6.0 只做了离线回归。Claude 的现场测试还没做，结果另记在 `docs/Claude实测记录.md`，这里不写测试结论。

## 交接的六项

| 交接的要求 | 怎么处理的 | 位置 |
| --- | --- | --- |
| 模板维护从用户初始化中分离 | 初始化只用已发布的模板：找不到项目空间时脚本给出模板链接和“使用模板”的做法，由初始化会话创建。空间缺东西一律报给模板维护者。搭建手册搬出插件安装包 | `scripts/setup.py` 的 `read_back`、`template_hint`；链接在 `orgspec/contract.json` 的 `template.url`，任务文件的 `feishu.template_url` 可以另指；`skills/setup/references/create-space.md`；仓库顶层 `feishu-template/` |
| 零视图配置 | 插件不建、不改、不查视图，就绪判定不看视图。读台账只走 `ledger.py` 的条件查询。书记员的视图写权限已去掉 | `scripts/setup.py` 的 `SCRIBE_MEEGLE`、`check`；`scripts/ledger.py` 的 `query`；`skills/setup/SKILL.md` 的“不做的事” |
| 分清 CLI 模式和桌面会话池的验收 | CLI 模式取消，只剩会话池，验收也只剩一套。21 份角色定义归本地配置层；会话层核对清单里的会话全部认领、入口和书记员各一条、预登记全部转正、权限模式同一类、工作目录是项目目录，带 `--sessions` 再对照客户端自己的会话列表。手工建七个会话改成回退档，默认由初始化会话开出全部会话 | `scripts/setup.py` 的 `load_task_and_seeds`、`pool_plan`、`pool_checklist`、`session_layer`、`listed_sessions`；`scripts/route.py` 的 `expect_cmd`；`skills/setup/SKILL.md` 第 6 步 |
| 统一初始化终点 | 终点是四层都就绪：本地配置、飞书、会话、首条研究任务。`init --apply` 生成最小研究任务单草稿，书记员开局时登记；`check --live` 只读回读结构和绑定、21 个节点，以及研究任务、主文档和上一级节点三者的关联。研究访谈、业务派活和开发测试放到初始化之后 | `scripts/setup.py` 的 `first_task_sheet`、`SCRIBE_FIRST`、`first_task_layer`、`live_feishu`；访谈后的修订稿用 `scripts/ledger.py` 的 `register --existing` 换主文档 |
| 五分钟目标要完整计时，首次和补齐分开 | 分阶段计时，阶段的归类写死在脚本里。只有飞书操作计入 300 秒，它是指标，不改变就绪结论；Claude 平台、本机和等人的时间单独记。首次和补齐各记一条，互不覆盖 | `scripts/onboarding.py` 的 `STAGES`、`summary`；`skills/setup/SKILL.md` 的“计时”；书记员的两段由它自己打点 |
| 保留唯一书记员的写入机制，开局顺序和客户端能力匹配 | 写入锁、登记回执、`--resume` 没有改，正式写入仍只有唯一认领的书记员。书记员第一个开，认领以后接着建节点、登记首条研究任务；回退档里由项目管理 +2 在首轮开出书记员 | `scripts/ledger.py` 的 `write_lock`、`save_receipt`；`scaffold/hooks/guard.py` 的 `check_ledger`；`scripts/setup.py` 的 `pool_plan`（`order`） |

## 交接以外，这一版一并做的

| 事项 | 怎么处理的 | 位置 |
| --- | --- | --- |
| 登录 | 命令行登录失效时，由研究员的会话发起设备码登录，给出二维码图片和链接，研究员扫码。设备码不打印。角色会话发起会被 hook 拒绝。lark-cli 的用户身份还能续期的算可用 | `scripts/setup.py` 的 `login`、`lark_user_ready`；`scaffold/hooks/guard.py` 的 `check_login`；`skills/setup/references/precheck.md` |
| `.env` | 在项目目录的 `.env` 里写飞书项目和知识空间的两条链接，其他行不动也不显示；`check` 的飞书层核对 | `scripts/setup.py` 的 `env_cmd`、`env_gaps` |
| 主文档里“关联工作项”的占位残留 | 交接里列在 Codex 的后续事项。登记时按包头那一行解析，“待建”“待登记”这类占位统一换成工作项 ID，写了别的内容就在写入前停下；已登记的文档用 `relink` 原位修正，工作项和文档的身份不变 | `scripts/ledger.py` 的 `link_line`、`relink` |

## 和交接时不一样的地方

交接是照 0.5.1 的结构写的。0.6.0 里这几样变了，读交接原文时要对上：

- 运行方式只有会话池。任务文件不再写 `mode`，写 `"mode": "cli"` 会报错。
- 工作项从六类变成五类：研究任务、同序列需求、跨序列需求、决策、反馈，都走节点流程。“项目”和“送审”两类没有了，最小立项换成首条研究任务。
- 任务包从五种变成三种：需求包、交付包、决策包。把关改由测量承担。
- 知识库节点从 29 个变成 21 个。
- 开池默认 19 个会话：外联开池只开 +2。
- 飞书模板换成新的一版，用节点流程；旧结构的空间不迁移。流程、表单和字段不写在规则里，脚本运行时读空间，按含义处理的槽位写在 `orgspec/contract.json`。

## 留给 Codex 一侧的

- 共用脚本的改动随 0.6.0-codex.1 进了 Codex 的安装包，Codex 一侧只保证离线回归。
- `skills-codex/setup/SKILL.md` 还是上一版的做法（复制模板空间、六类工作项），要 Codex 一侧照新的模板和约定改写。
- Codex 的首次配置耗时、hook 信任引导这两项后续事项不在这次改动范围内。

## 修改后的验证

共用代码变更运行下面的检查；纯文档修改核对事实、链接和差异，再跑前两条。

```bash
python3 -I -B plugins/agent-org/scripts/orgspec.py check
```

```bash
python3 -I -B plugins/agent-org/tests/test_plugin.py
```

```bash
python3 -I -B plugins/agent-org/tests/test_space.py
```

```bash
python3 -I -B plugins/agent-org/tests/test_ledger.py
```

```bash
python3 -I -B plugins/agent-org/tests/test_binding.py
```

```bash
python3 -I -B plugins/agent-org/tests/test_initialization.py
```

```bash
python3 -I -B plugins/agent-org/tests/test_codex.py
```

```bash
python3 -I -B plugins/agent-org/tests/test_codex_upgrade.py
```

各项对应的离线用例：开池清单和任务文件在 `test_plugin.py` 的 `PoolSetupTest`，四层检查在 `CheckLayersTest`，初始化会话开出全部会话在 `PoolOpenedByTheResearcherTest`；绑定、找空间、知识空间、登录和 `.env` 在 `test_binding.py`；计时在 `test_initialization.py` 的 `StageClockTest`；换研究任务单在 `test_ledger.py` 的 `RegisterTest`。

Claude 的现场测试单独保存报告，分开记安装、生成文件、实际会话、飞书回读、首次耗时和各流程的推进。涉及共用运行代码的发布，同时说明两端的版本和各自的验证范围。
