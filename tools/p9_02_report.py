"""Write P9-02's factual report from retained replay/live evidence."""
import json
from pathlib import Path
from datetime import datetime,timezone
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def fmt(value,digits=2):return '—' if value is None else f'{value:.{digits}f}'


def main():
    assets=ROOT/'reports/assets';replay=json.loads((assets/'P9-02-replay/results.json').read_text())
    live=json.loads((assets/'P9-02-live/results.json').read_text());validated=json.loads((assets/'P9-02-validation.json').read_text())
    tests=(assets/'P9-02-pytest.log').read_text().strip().splitlines()[-1]
    changed=subprocess.check_output(['git','diff','--name-only'],cwd=ROOT,text=True).splitlines()
    lines=['# P9-02：检测接入、颜色粗采样、主体计数与盲审缩图', '',
    '代码、配置、离线测试及 14 图回放完成；两次真实检测蒙版局部重绘各生成一次，前后对照已交付。**不能宣称修复效果通过：去人失败；面部蓝瞳变绿有局部改善，但两张输出的实际蒙版轮廓色差均超过 24，冻结整镜复审也均未通过。** 新检测项全部标为 `uncalibrated / verified=false / 未校准`，不替代或重新解释冻结视觉审核结果。', '',
    '## 实现与配置', '',
    '- `src/storycg/detection_review.py` 与 `config/detection_review.json`：本机检测裁切记录、颜色证据、主体计数、检测区域蒙版及 image-only 盲审缩图。使用 P9-01 已有权重和 `.venv`；无依赖安装、无模型下载。',
    '- 面部猜框调用从定妆审核、普通整组审核、候选复审、L3 参考/定妆裁切中删除；IPAdapter 及普通审核参考脸也从完整定妆图重新本机检测，不继续使用旧猜框裁切。裁切选最大检测脸，记录其框、置信度和 hash；这不是身份识别。无脸记录 `face_not_detected`，背影/远景不凭几何缺脸额外否决视觉审核；定妆或 IPAdapter 必需参考无脸时仍阻止使用，绝不猜框回退。',
    '- `repairs.py` 接收检测蒙版路径、sha256 和原图 provenance。沿用区域面积限制、上下文边距、比例分辨率选择，面部精修也保持比例。羽化蒙版经 `LoadImageMask → SetLatentNoiseMask` 上传约束潜变量，再用同一原尺寸蒙版合成，蒙版外像素保持逐字节一致。面部携带新检测定妆脸 IPAdapter；去人正向只取 scene 背景，负向加 person/people/girl/boy/human/face/body，并去掉区域 IPAdapter/角色 LoRA。',
    '- 新蒙版节点分配空闲 ID，避免覆盖 L1 角色 LoRA 的 node 30。首次蒙版及 provenance 持久化，恢复校验原图/purpose/蒙版 hash 并复用，拒绝篡改，不重新检测覆盖。',
    '- 计数固定使用监工阈值：脸 confidence≥0.6 且面积≥最大脸 30%；人物 confidence≥0.5 且框面积比≥0.08；先检查所有原始候选，删除同时包含另两人物框的大合并框；主体数=max(主要脸数,主要人物数)。输出 `ok / duplicate / none_visible`。`shot.reflection=true` 或旧 shot 的明确 `reflection` tag 允许 2；`false` 与否定/自由描述不会误开豁免。',
    '- `other_people` 保留在证据中；小路人由固定阈值排除，partial/offscreen 不会无条件允许一个大额外主体。检测器不能识别身份或肢体归属，大幅入镜的允许他人仍可能误报；这是未校准局限。',
    '- 瞳色从检测面部框内归一化区域 `[0.12,0.25,0.88,0.58]` 取高饱和像素，排除肤色色相与有支持映射的发色；颜色名→色相范围、饱和度/像素下限、显著色簇要求与定妆色相容差在独立配置。并列记录 identity tag 与定妆采样比对、hue/saturation/pixel_count/ROI。不足、歧义、unsupported 无色相依据时 `not_visible`。配饰只检查面部周边环内定妆显著色块是否出现，无法确认存在则 `not_visible`，不据此声称配饰缺失。',
    '- `cycle.json` 重启面部检测蒙版精修与 `protagonist_count → inpaint/relayout`；道具、动作等其它映射保留 D6。重复主体检测可作为明确未校准的修复诊断，记录 `uncalibrated_detection` 改善依据，不能升格成 reliable；候选不得增加原有审核失败/不可见项。合并分割空间分区有歧义时自动循环停止并记录原因，本工单只在指定试验中使用一次。',
    '- 盲审前长边缩到 768px，不放大小图，记录缩放前后尺寸、hash 和独立路径，`blind_thumbnail` 日志与 manifest/review 均保留；实际请求仍只有有序图片。冻结盲审 schema、single/consistency 提示词与 reliability 配置不变。',
    '- 新任务配置 `budget.max_job_minutes=60`、`cycle.max_minutes=60`；45 分钟仍可继续，60 分钟停止。旧任务/旧作品未改，试验仅修改新副本。', '',
    '## 14 张图的真实离线回放', '',
    f'串行新推理，无出图、无视觉服务调用。本次最终回放耗时 **{replay["elapsed_seconds"]:.3f} 秒**。完整输入/参考 hash、原始检测框、采样 ROI/色相/饱和度/像素数、定妆采样与差值、缩图 hash 见 `reports/assets/P9-02-replay/results.json`。', '',
    'D3/D10/D11 使用监工在 P9-01 明确描述的倒影声明，在新回放记录中加 `reflection=true`；没有改旧 storyboard。D10/D11 旧分镜只有 mirror 描述，没有明确 reflection 声明，按旧状态会判 duplicate；回放同时保留 `legacy_count`，不隐瞒这个接入差异。', '',
    '| 图 | 主要脸/人物 | 主体数/判定 | 瞳色粗采样 | H° / S / 像素数 | 定妆 H° | 配饰粗检 | 缩图尺寸 |',
    '|---|---:|---|---|---|---:|---|---|']
    for row in replay['results']:
        c=row['protagonist_count'];e=row['eye_color_sampled'];ref=e.get('reference_sample') or {};a=row['accessory_color_sampled'];s=row['blind_thumbnail']['output_size']
        lines.append(f"| {row['id']} | {len(c['major_face_indices'])}/{len(c['major_person_indices'])} | {c['protagonist_count']} / {c['status']} | {e['status']} | {fmt(e.get('hue'))} / {fmt(e.get('saturation'),3)} / {e.get('pixel_count',0)} | {fmt(ref.get('hue'))} | {a['status']} | {s[0]}×{s[1]} |")
    lines.extend(['',
    '**颜色方法未校准，不能拿本表当真实错误分类。** D1/D2/D3 的 pink tag 在固定色相区间中与近红的采样不符，即使与定妆 hue 差仅约 3–8°仍会报 mismatch；D12 的定妆样本采到近红色而当前约 201°，可能混入皮肤/头发/其他色块。D7 仅 10 像素而 not_visible；D9/D10/D11/D13 候选色簇歧义也 not_visible。没有按测试图调整区间、降低像素/显著性阈值或改判结果。D14 使用该校准样本登记的原始 identity/参考，不把换角色图改成自我比对。', '',
    '## 两次真实局部重绘', '',
    '对照：`review/P9-02-inpaint.jpg`（1440×1120，上行去人，下行面部；左原图、右各一张新结果）。原图均未改，副本、raw patch、composite、蒙版、workflow、prompt_id、manifest、复审记录均保存。', '',
    '### 1. 指定双主体镜头', '',
    '- 来源：`jobs/cg-76d2eec57dfc/storycg_cg-76d2eec57dfc_shot01_a001.png`。本机得到两张主要脸、一个合并人物分割。',
    '- 指定试验用通用“按最近面部进行合并分割的空间分区”，保持最大脸分区，试着去掉另一分区；配置垂直距离权重 0.15，记录 `uncertain=true`，没有角色名/剧情词/固定坐标特例。这只是检测派生的实验性蒙版，不是可靠人物归属。蒙版确实包含了部分主体腿与桌面，自动循环拒绝使用这种歧义蒙版。',
    '- **结果：去人失败**。站立人物仍存在，外貌变成另一个人物，主体数仍 2/duplicate；主体腿/桌面也有误改风险。背景正向+人物负向不能保证在含原人物潜变量的 img2img 区域消除人体。只试一次，没有重复换 seed。',
    '### 2. 按工单允许的备选面部问题镜头', '',
    '- 指定的 `runs/p5_02_b_r1/storycg_p5_02_b_r1_shot02_a003.png` 中多余物体不属于现有人脸/人物检测类别，无法由检测定位该物体；做了本机检测与蒙版预览，未猜框、未生成该镜修复。',
    '- 改试 `runs/p3_02_a_none/storycg_p3_02_a_none_shot03_a001.png`；已有 `review.json` eye_color=fail，P3-02 人眼记录明确蓝瞳，当前也检查到可见蓝色虹膜。该镜确有面部问题，不是假设约尔脸有错。',
    '- **结果：蓝瞳变绿，局部改善可见**。新采样 mismatch→ok，约 218.48° / S=0.684 / n=549 → 159.45° / S=0.439 / n=219；面部形状/发丝也变化。冻结整镜结果仍未通过，且边界检查未通过，不能自动选为合格结果。', '',
    '| 试验 | ComfyUI prompt_id | 生成等待秒 | 实际蒙版新增色差/阈值 | 冻结整镜复审 | 蒙版外像素 |',
    '|---|---|---:|---|---|---|'])
    for row,v in zip(live['results'],validated['live']):
        edge=v['mask_boundary_check']
        lines.append(f"| {row['id']} | `{v['prompt_id']}` | {row['repair_seconds']:.3f} | {edge['max_new_color_jump']:.1f} / {edge['threshold']}，未通过 | {row['visual_conclusion']} | 完全一致 |")
    lines.extend(['',
    '两次试验生成时沿用的 P5 上下文矩形边界检查都得到 0/通过。只读代码复核发现这个检查没有覆盖位于矩形内部的检测蒙版轮廓；随后新增沿蒙版 50% 轮廓的“相对原图新增 RGB 梯度”检查，保留阈值 24，回测两张现有新输出得到 52/56，均拒绝。原始生成/审核记录原样保留，补测在 `P9-02-live/mask-boundary-recheck.json` 和 `P9-02-validation.json`，没有为了通过放宽阈值或改写视觉结论。该几何拒绝规则也需要后续更多样本验证。', '',
    '## 修改文件', '',
    *['- `'+name+'`' for name in changed],
    '- 新增：`src/storycg/detection_review.py`、`config/detection_review.json`、`tests/test_detection_review.py`、`tests/local_face_fixture.py`。',
    '- 新增可运行工具：`tools/p9_02_replay.py`、`tools/p9_02_live.py`、`tools/p9_02_verify.py`、`tools/p9_02_report.py`。',
    '- 新增证据：`reports/assets/P9-02-*`、`runs/p9_02_duplicate/`、`runs/p9_02_face/`、`review/P9-02-inpaint.jpg`、本报告。',
    '- 开工时已有未跟踪的 `logs/`、P9-01 的 tools 和 `verify/p9-01_d7zoom.jpg` 未改。未修改 skills、已有作品、模型/节点/主机环境、禁止目录与监督文件，未 git commit。', '',
    '## 验证与可复制命令', '',
    '在项目根目录执行；前 4 条不访问 ComfyUI 或视觉服务：', '',
    '```bash',
    'export UV_CACHE_DIR="$PWD/.uv-cache"',
    'uv run --locked pytest -q',
    'uv run --locked python tools/p9_02_replay.py',
    'uv run --locked python tools/p9_02_verify.py',
    'git diff --check',
    '# 已完成的同一 repair_id 会复用，不新增出图；首次执行才串行提交两个试验',
    'uv run --locked python tools/p9_02_live.py',
    '# 从证据重写报告',
    'uv run --locked python tools/p9_02_report.py',
    '```', '',
    f'- 默认完整测试：**{tests}**；最终完整日志 `reports/assets/P9-02-pytest.log`。',
    '- 离线测试覆盖：监工阈值与合并框/倒影/路人/背影/无主体、瞳色锁定与参考/隐藏/皮肤发色排除、配饰参考粗检、768 缩图 hash 和真正 image-only payload、无脸零视觉猜框、定妆/L3/IPAdapter 检测裁切、羽化蒙版/合成上下文保护、60 分钟门限、LoRA 节点冲突、实际蒙版边界拒绝、持久化蒙版篡改拒绝、未校准计数不升格可靠审核。远程传输测试使用离线模拟服务，只验证编排/恢复；真实效果证据来自上面两次新 ComfyUI 提交，未用测试图片冒充新出图。',
    '- 测试先失败再实施：red/integration-red/mask-red/regression-red/cycle-red/review-red/reference-red 日志保留。第一次完整测试 27 failed/299 passed，主要是旧纯色测试依赖视觉猜框以及 Pillow 全局 patch 引起缺失文件错误；修正真实检测 fixture、调用数、60 分钟期望和加载副作用后完整测试通过。没有删除失败验收测试来掩盖问题。',
    '- Ultralytics 导入全局替换 `PIL.Image.open`，原缺失文件异常触发 HEIF 导入；检测模块恢复导入前的 Pillow opener，回归测试证明缺失文件继续抛 FileNotFoundError。不安装额外 HEIF 包。',
    '- 冻结核对：single/consistency 等审核函数与全部顶层 prompt/schema 常量 AST 与 HEAD 完全一致，reliability 文件逐字节相同；证据在 `P9-02-frozen-check.json`、`P9-02-validation.json`。',
    '- requesting-code-review 只读子代理复核提出参考脸、真实蒙版边界与恢复风险；修复后再次复核无残留 Critical/Important。没有委派生成或调用外部服务。', '',
    '## 产出与用量', '',
    '- 图片/拼图：`runs/p9_02_duplicate/storycg_p9_02_duplicate_shot01_a002_composite.png`、`runs/p9_02_face/storycg_p9_02_face_shot03_a002_composite.png`、`review/P9-02-inpaint.jpg`。',
    '- manifest：`runs/p9_02_duplicate/manifest.json`、`runs/p9_02_face/manifest.json`；其中每个新试验 generated_images=1、submission_attempts=1；旧图标 inherited。',
    '- 检测/蒙版：两目录下 `p9_detection/` 与 `repair_assets/shot01_p9_02/` / `repair_assets/shot03_p9_02/`，含原尺寸羽化蒙版、缩放噪声蒙版及上传记录。',
    '- 新复审：两目录下 `cycle_artifacts/p9_after.json` 与 `review_log.jsonl`；冻结结论未通过均保留。',
    '- 回放：`reports/assets/P9-02-replay/results.json` 与每图 `blind/01.png`；用量账本 `reports/assets/P9-02-live/usage.json`、`results.json`；独立校验 `P9-02-validation.json`。',
    '- **ComfyUI 新生成 2 张 / 上限 8；视觉服务审核 4 次 / 上限 20；文本 LLM 调用 0**。蒙版、裁切、缩图、合成、拼图属于派生文件，不算新生成。两次生成等待合计约 36.5 秒，复审/本机检测/图像落盘计入试验总耗时。',
    f'- 实际串行试验总耗时 {live["elapsed_seconds"]:.3f} 秒；最终回放 {replay["elapsed_seconds"]:.3f} 秒；完整测试 118.71 秒。工单从日志文件创建 2026-10-01 13:37:34 HKT 开始，报告生成于 {datetime.now(timezone.utc).isoformat()}（UTC），墙钟约 30 分钟，低于 90 分钟。',
    '- 仅通过现有 ComfyUI HTTP API 上传/提交/下载、既有视觉 HTTP API 审核；未登录 Windows/MacBook、安装或改动任何主机内容；未清空/取消队列。', '',
    '## 未完成、不确定与已知问题', '',
    '1. 去人没有成功，也未证明合并分割空间分区能安全隔离非主体。自动循环对此停止；两个背影被合并时仍可能漏检，检测计数不提供身份/倒影对应/局部肢体归属证明。',
    '2. 颜色采样经常 not_visible，也有疑似错误颜色簇和固定 pink 区间误报；只是粗检，不具有眼部关键点/虹膜分割，仍需专门新校准，不能取代视觉审核或据 mismatch 单独判失败。没有把结果重判为 ok。',
    '3. 面部修复仅瞳色有局部改善，形状/发丝有变化，冻结整体仍未通过，边界阈值也拒绝。两次试验均不适合宣称正式验收通过。',
    '4. 原 P5 矩形边界检查对内部蒙版没有效力，已补轮廓检查；新的新增梯度最大值规则可能对自然纹理变化敏感，尚无足够真接缝/无接缝校准集，保持阈值 24 未放宽。',
    '5. 旧分镜 mirror 描述不自动声明 reflection；D10/D11 要在新任务/后续分镜明确反射状态，否则重复提示会保留。旧作品未改；生产 schema 新增可选 reflection。',
    '6. 真实蒙版路径已通过这两个试验验证提交、参考、上传和合成，但未遍历更多角色/景别/LoRA 实机组合；L1 节点冲突由实际 workflow 离线测试覆盖。预算剩余没有用于额外出图。',
    '7. 本工单未做 P9-03 的“看不见/不存在”视觉提示词改版、重新冻结或校准；不接入 Qwen-Image 2.1/ControlNet。', '',
    '## 需要用户转派', '',
    '无主机安装/修改请求。若要求稳定去人，需要后续工单明确实例分割/人体归属或区域去除生成方案并重新校准；不能在本工单里安装节点、换模型或扩大试验范围。', ''])
    (ROOT/'reports/P9-02.md').write_text('\n'.join(lines),encoding='utf-8')

if __name__=='__main__':main()
