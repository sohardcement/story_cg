# story-cg-v1

输入一个具名角色和一句剧情，默认自动生成 4–6 张连续 CG、编号拼图和结果说明。适合由单主角的表情与动作推进的故事，例如：

> 在山顶迎着朝阳伸懒腰，被风吹乱头发后有点生气，最后看着日出笑了。

v1 擅长单主角的表情、动作、身份与服装一致，以及场景变化。不擅长道具归属、状态、数量、“拿错东西”类情节、两个具名角色同框、远处小人物。道具问题仍审核并写进已知问题，但不否决、不触发修复。核心依赖道具的剧情会照常处理，交付时提醒可能读不出；多个具名角色按指定单主角处理。

## 使用

在项目目录使用已有 `.venv`：

```bash
.venv/bin/python -m storycg make --character '角色名' --story '在山顶迎着朝阳伸懒腰，被风吹乱头发后有点生气，最后看着日出笑了' --shots 4
.venv/bin/python -m storycg status jobs/cg-任务编号
.venv/bin/python -m storycg resume jobs/cg-任务编号
.venv/bin/python -m storycg deliver jobs/cg-任务编号
```

默认后台运行。主动要求先看分镜时，在 `make` 加 `--storyboard-first`；看到 `awaiting_approval` 后用 `approve <job_dir>`，修改时加 `--edit '修改说明'`。仅角色身份无法确定、关键资源不可用、用户要求冲突时提问。`deliver` 只重建已存图片的交付文件，不出图、不重新审核。

交付包括 `contact_sheet.jpg`、完整单图、`result.md`、`manifest.json`。整组门禁按 v1 范围归并，冻结原始审核保留；“未经可靠审核”与已知问题应一并阅读，尤其配饰、发型及远处小人物。原始审核失败不能被理解为所有人物项都失败，范围内的失败也不会因道具降级自动消失。

Hermes 入口见 `skill/story-cg-v1/SKILL.md`。安装由维护者运行已有 `tools/install_hermes_skill.sh`；本工单不重装、不写入 Hermes 目录。
