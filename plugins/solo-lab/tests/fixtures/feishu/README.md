# 飞书真实输出

2026-11-01 在测试空间上用只读命令录下的输出。租户域名、文档和节点 token、研究员的标识已换成假的；工作项 ID、type_key、field_key、选项 ID 保持原样，脚本靠它们对上规则文件。

| 文件 | 命令 |
| --- | --- |
| workitem-get-all.json | `meegle workitem get --work-item-id 7151870875 --fields '["_all"]'` |
| transitions-decision.json | `meegle workflow list-state-transitions --work-item-id 7152108550` |
| transitions-minimal.json | 同上，对 7151870875，不带 --user-key 和 --work-item-type |
| state-required-decision.json | `meegle workflow list-state-required --state-key 'In Progress' --work-item-id 7152108550` |
| docs-fetch-outline.json | `lark-cli docs +fetch --scope outline --detail with-ids` |
| docs-fetch-range-status.json | `lark-cli docs +fetch --scope range`，从“状态包”到“交接记录” |
| docs-create.json | `lark-cli docs +create` 的返回（2026-10-31 建第一份包时） |
| wiki-node-get.json | `lark-cli wiki +node-get --node-token <document_id>` |
| event-consume-dryrun.json | `lark-cli event consume im.message.receive_v1 --as bot --dry-run` |
| workitem-query-queue.json | `meegle workitem query --mql 'SELECT ... FROM <空间>.决策'`，一行（session_id 换成假的） |
| workitem-query-empty.json | 同上，条件查不到工作项：`data` 是空对象，`list` 为 null |
| wiki-node-list-root.json | `lark-cli wiki +node-list --space-id <space_id> --page-all --as user`：知识空间根下的 8 个节点。结构和标题照 2026-11-01 在模板空间上的实测，token 是假的 |
| workitem-query-page-past-end.json | 带首查的 `--session-id` 和 `--group-pagination-list '[{"group_id":"1","page_num":2}]'` 翻到结果之后：各项都是 null |
| event-message.json | 按 `lark-cli event schema im.message.receive_v1` 造的一条私聊事件 |
| workitem-query-user.json | ``meegle workitem query --mql 'SELECT `work_item_id`, `name`, `field_c9def8` FROM <空间>.决策 WHERE `work_item_id` = 7152108550'``：人员字段（决定人）是 `user_value`。单元格的形状照 2026-11-01 写入测试时的实测，外层结构同 workitem-query-queue.json；姓名和 user_key 是假的，邮箱也填了假的，用来确认不会打出来 |
| meta-types.json | `meegle workitem meta-types --project-key <project_key>`（2026-11-02 录）：12 个类型，`is_disable` 为 1 的四个系统类型在空间里停用，查 `meta-fields` 会报找不到类型 |

## v2：参考模板空间的结构

2026-11-12 用 `space.py` 只读读取参考模板空间时录下的输出，放在 `v2/`。2026-11-21 模板改到 2.3.0，这里的流程、节点、状态、字段和选项的名字照 2.3.0 换过，其余保持录制时的样子：决策的状态还叫“已决定”，也没有 2.2.0 加的两个字段，测试用它覆盖老空间。负责人的标识已换成假的，姓名和邮箱清空；工作项 ID、type_key、field_key、状态和选项 ID 保持原样。

| 文件 | 内容 |
| --- | --- |
| v2/space-calls.json | `space.py` 读一个空间时发出的 30 条 `meegle` 命令和各自的输出：`project search`、`workitem meta-types`、每个类型的 `meta-fields`（研究任务翻两页）、`meta-roles`、`meta-create-fields`，每个流程一条 `workitem query` 和一条 `workflow get-node` |
| v2/space.json | 上面这些输出读成的快照：5 个类型、6 个流程、27 个节点。`test_space.py` 核对重放结果和它一致，其他测试拿它当空间结构 |
