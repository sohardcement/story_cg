"""Assemble numerical P9-01 report from unmodified real calibration JSON."""
import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
NOTES = {
'D1': '不符：应为 2 人，检出 1 个合并人物框。2 张脸正确；人物蒙版还污染桌面与物品，不能直接用于局部重绘。',
'D2': '符合：单人正面，1 脸 / 1 人。',
'D3': '倒影会被检出；结果是 3 个人物框：P1 是大合并框，P2/P3 分别覆盖右侧本人和左侧倒影，不能按 3 位主体计数。',
'D4': '符合：远景背影 1 人、0 脸。',
'D5': '符合：背影奔跑 1 人、0 脸。',
'D6': '人物数符合：1 人、0 脸；画外手没有独立人物框，但分割包含手臂及部分非主体区域，不能认为手已被可靠隔离。',
'D7': '符合：1 脸 / 1 人。面部框约 105 × 101 px，原始小脸能检出。',
'D8': '2 个人物框分别覆盖背景路人与主角。背景背影位置也检出面部候选，需看原图确认可见性；不能用脸数作为主角数。',
'D9': '人物数符合：1 人、1 脸；画外手没有独立人物框。分割边界不能承担人物身份/肢体归属判断。',
'D10': '本人 + 镜中倒影分别检出，2 脸 / 2 人；物理主角仍为 1，直接人物计数会误报重复主体。',
'D11': '2 张面部区域检出，人物模型将本人和镜中影像合成 1 框/蒙版，未分开两个人像；数值 1 不代表理解了反射。',
'D12': '符合：单人奔跑正面，1 脸 / 1 人。',
'D13': '符合：单人中景，1 脸 / 1 人。',
'D14': '符合：换角色单人中景，1 脸 / 1 人。',
}


def main():
    data = json.loads((ROOT/'calibration/detect/results.json').read_text())
    env = json.loads((ROOT/'reports/assets/P9-01-environment.json').read_text())
    lines = ['# P9-01：本机检测基础设施与校准报告', '',
        '## 范围与结果', '',
        '已实现独立 CPU 检测、框/分割蒙版羽化、面部裁切、配置阈值和 CLI，并对工单指定的 14 张现有图片做真实检测。未接入生成或审核流程。检测基础设施可用，人物计数效果存在明显局限：D1 两人合并，D3 多重重叠框，D10 将倒影算第二人，D11 合并本人与倒影。不能据此声称单主角计数已可靠。', '',
        '遵守 AGENTS.md 补充禁令 4/5 与 TASK.md §2；仅在本工作目录安装依赖/下载权重/写新输出，没有修改已有作品。ComfyUI 任务 0，出图 0，视觉模型与审核调用 0，未访问 Windows/MacBook；未修改冻结审核器、reliability、skills、TASK.md、SUPERVISOR.md、PROGRESS.md、orders/、AGENTS.md、verify/ 或 ~/.hermes，未 git commit。', '',
        '## 修改与新增文件', '',
        '- 修改 `.gitignore`、`pyproject.toml`、`uv.lock`、`src/storycg/__main__.py`（仅增加 detect 命令分派）。',
        '- 新增 `src/storycg/detect.py`、`config/detect.json`、`tests/test_detect.py`。',
        '- 新增 `tools/download_detect_models.py`、`tools/calibrate_detect.py`、`tools/render_detect_calibration.py`、`tools/verify_detect_artifacts.py`、`tools/write_detect_report.py`。',
        '- 新增模型 manifest、校准数据/蒙版/叠加图、总览、本报告及 `reports/assets/P9-01-*` 证据。开始时已有 `logs/`、`verify/P9-calibration-v2-design.md` 未跟踪文件；工作期间出现 `orders/P9-02.md`，均未处理。', '',
        '## 安装与来源', '',
        f'实际环境：Python {env["python"]}，{env["platform"]}，uv 0.12.13。所有 Python 包安装于本工作目录 `.venv`，缓存 `.uv-cache`。', '',
        '| 包 | 实际版本 |', '|---|---|']
    lines += [f'| {key} | {value} |' for key,value in env['versions'].items()]
    lines += ['', '`torch.version.cuda` 为 null、`torch.cuda.is_available()` 为 false。torch/torchvision 仅绑定 CPU wheel 索引，其余包使用 PyPI；未下载 CUDA runtime。间接依赖 nvidia-ml-py 是 Python NVML 接口，不是 CUDA wheel。详见 `reports/assets/P9-01-environment.json`。', '',
        '初次 `uv add --index pytorch-cpu=...` 因普通包不在 CPU 索引中解析失败，未安装任何包；改为 pyproject 的 `explicit=true` 和包级 sources 后成功。初次下载遇到 httpx 环境 SOCKS 代理缺少 socksio，下载器改为 `trust_env=False` 后直连成功，没有额外安装代理依赖。', '',
        '依据：[uv PyTorch 集成](https://docs.astral.sh/uv/guides/integration/pytorch/)、[PyTorch 安装](https://pytorch.org/get-started/locally/)、[Ultralytics predict/Results](https://docs.ultralytics.com/modes/predict/)。', '',
        '选择工单指定的两个 s 模型，兼顾本机 CPU 开销与精度。Bingsu 模型卡中 s 比 n 的指标更高，m 更大；这里没有自行比较 n/m，因此不宣称 s 在本样本集最优。[Bingsu/adetailer 模型卡](https://huggingface.co/Bingsu/adetailer)。', '',
        f'固定仓库 revision：`{data["model_manifest"]["revision"]}`。来源、大小和本地计算 sha256 均写入 `models/detect/manifest.json`；权重通过 .gitignore 排除，manifest 可供监工提交。没有执行 git add/commit。', '',
        '| 模型 | 大小（字节） | sha256 |', '|---|---:|---|']
    for model in data['model_manifest']['models']:
        lines.append(f'| [{model["filename"]}]({model["source_url"]}) | {model["size_bytes"]} | `{model["sha256"]}` |')
    lines += ['', '## 接口、配置及数据语义', '',
        '`detect_faces(image)` 返回像素 xyxy 框、confidence、area_ratio；`detect_persons(image)` 另含原尺寸 L 模式分割 mask 和 mask_area_ratio。image 可为路径或 PIL 图片。`crop_face` 外扩后裁切，`feather_mask` 优先用分割、无分割时用框。模型延迟缓存加载，路径不存在即报错，禁止自动下载；YOLO 配置和 Matplotlib 缓存限定 `.cache/detect/`，YOLO_OFFLINE=true、YOLO_AUTOINSTALL=false。', '',
        '坐标以原图为准，右/下边界排他；裁切向外取整并裁到图界。框外扩比例是每侧增加框宽/高的相应比例；分割外扩以 ceil(比例 × 框最大边) 作椭圆膨胀，再 GaussianBlur 羽化；输出尺寸与原图一致。mask_contours 为像素坐标，含 contour hierarchy；无损 PNG 是分割蒙版的权威像素输出。JSON 不直接嵌入大规模 mask 数组。', '',
        'area_ratio = 裁到图内的框面积 / 原图面积；mask_area_ratio = 二值前景像素数 / 原图面积。表中面积均为比例（0.1 即 10%）。F/P 编号按本次模型输出顺序，不是语义身份。', '',
        '校准配置：CPU、imgsz=640、NMS IoU=0.7、max_det=100；面部/人物 confidence≥0.25、最小框面积比=0，保留全部上述阈值以上候选。羽化默认 8px、蒙版每侧外扩 0.1、裁切外扩 0.25；参数均在独立 `config/detect.json`，未改变审核阈值。当前数值是校准基线，不是监工确认的主体规则。', '',
        'CLI 单图 `--overlay out.jpg` 保存文件；多图 `--overlay directory` 按序号与文件名保存至目录。stdout 是单一 JSON 对象，启动日志走 stderr。', '',
        '## 逐张检测', '',
        '下表每个 F 为 `置信度 / 框面积比`，每个 P 为 `置信度 / 框面积比 / 分割面积比`。叠加图黄框=脸，绿框/轮廓=人物；c=置信度、box=框面积比、seg=分割面积比。完整框坐标、轮廓、输入哈希、蒙版路径及分项耗时见 `calibration/detect/results.json`。', '',
        '| 编号（叠加图） | 脸数 | 人数 | F：c / box | P：c / box / seg | 总检测秒 |', '|---|---:|---:|---|---|---:|']
    for item in data['results']:
        faces = '<br>'.join(f'F{i+1}: {x["confidence"]:.4f} / {x["area_ratio"]:.4f}' for i,x in enumerate(item['faces'])) or '—'
        people = '<br>'.join(f'P{i+1}: {x["confidence"]:.4f} / {x["area_ratio"]:.4f} / {x["mask_area_ratio"]:.4f}' for i,x in enumerate(item['persons'])) or '—'
        lines.append(f'| [{item["id"]}](../{item["overlay"]}) | {len(item["faces"])} | {len(item["persons"])} | {faces} | {people} | {item["timing_seconds"]["total"]:.3f} |')
    lines += ['', '逐张对照监工预期（人工阅读本次叠加图，不调用视觉模型）：', '']
    lines += [f'- **{item["id"]}**：{NOTES[item["id"]]}' for item in data['results']]
    warm = [item['timing_seconds']['total'] for item in data['results'][1:]]
    lines += ['', '## 计数规则草案（未接入流程、未定阈值）', '',
        '1. 主体候选可先用人物 confidence≥0.4、框面积比≥0.03、且有分割时分割面积比≥0.02。此组合在这 14 张中保留所有主要人物框，D8 的路人 box=0.0237、seg=0.0156 被排除；D4 的远景背影 box=0.0861、seg=0.0421 被保留。仅是待监工审图的候选，不能推广为所有远景/构图的可靠阈值。小框一律保留原始数据，主体过滤另设字段。',
        '2. 面部数只作辅助警报：一个人物框包含多个相距明显的面部候选时标为“可能合并或反射、待复核”，不能直接把多脸解释成多人，也不能用零脸排除背影。D1 可被此辅助信号发现，D11 同样触发，二者仍需不同语义判断。',
        '3. 重叠处理可先提出 mask containment / IoU 候选：当一个大框/蒙版近似覆盖两个较小候选时标为“合并/重复疑似”，不要直接按置信度最高者保留一个。D3 最高置信度就是合并框，直接 NMS 再去重会丢失结构。阈值与删除规则尚无充分校准，保持全部原始候选。',
        '4. 倒影不能通过置信度或面积可靠排除：D10 的倒影 c=0.774、box=0.2055，不是低置信度小路人；D3 的倒影也很大。需另行利用镜面/窗面几何及人物与倒影对应关系，或转人工确认。当前检测器没有这些语义能力；不得针对某张图的坐标写特例，也不得默认第二人就是倒影。',
        '5. 路人只作“背景候选”而非“已证明不是主角”：可结合绝对面积、相对最大主体面积、任务角色身份，但这些仍待后续方案验证。局部手未独立检出不等于无人入镜；D6/D9 的肢体归属与分割污染无法用人物计数解决。',
        '6. 最终三态宜为“单主体候选 / 多主体候选 / 不确定待复核”；不确定不能记成单人通过。本工单仅提出规则，没有修改任何现有门禁或审核结论。', '',
        '## 验证与重现命令', '',
        '在项目根目录执行（CPU 推理，不访问 ComfyUI/视觉模型）：', '',
        '```bash',
        'export UV_CACHE_DIR="$PWD/.uv-cache"',
        'uv sync --locked',
        'uv run pytest -q',
        'uv run pytest -q tests/test_detect.py',
        '# 缺权重时下载；已有 manifest 时使用其中的固定 revision',
        'uv run python tools/download_detect_models.py',
        'uv run python -m storycg detect calibration/samples/C2.png --overlay reports/assets/P9-01-cli-single.jpg > reports/assets/P9-01-cli-single.json',
        'uv run python -m storycg detect calibration/samples/C2.png calibration/samples/E1a.png --overlay reports/assets/P9-01-cli-multi > reports/assets/P9-01-cli-multi.json',
        'uv run python tools/calibrate_detect.py',
        'uv run python tools/render_detect_calibration.py',
        'uv run python tools/verify_detect_artifacts.py',
        'git diff --check',
        'git check-ignore models/detect/face_yolov8s.pt models/detect/person_yolov8s-seg.pt',
        '```', '',
        '离线测试覆盖：框扩展与图界裁切、蒙版原尺寸与排他边界、羽化边缘、分割孔洞/膨胀、配置置信度与最小面积筛选、配置几何默认值、无效参数和尺寸拒绝。先写测试运行，因检测模块不存在而收集失败；实施后通过。CLI 与检测效果由真实权重/真实图片运行验证，没有 mock 检测充当证据。', '',
        '默认 `uv run pytest -q` 最终 **314 passed in 96.65s**；检测离线测试 **6 passed**。完整输出见 `reports/assets/P9-01-pytest.log`；模型/输入 sha256、叠加图与蒙版尺寸、蒙版二值性与面积、CLI JSON/图路径均由 `tools/verify_detect_artifacts.py` 验证，输出 `reports/assets/P9-01-artifacts.log`。', '',
        '## 产出与用量', '',
        '- 依赖：`pyproject.toml`、`uv.lock`；模型来源与哈希：`models/detect/manifest.json`；本地权重 `models/detect/*.pt`（忽略 git）。',
        '- 逐张结果：`calibration/detect/results.json`；叠加图 `calibration/detect/D1-overlay.jpg` 至 `D14-overlay.jpg`。',
        '- 原尺寸分割蒙版：`calibration/detect/D*/masks/person*.png`（本组 18 个）。',
        '- 总览：`review/P9-01-detect.jpg`（2560 × 2240）。',
        '- 用量：新生成图片 0；ComfyUI 提交 0；视觉模型/审核调用 0。本机完整校准运行 2 次 × 14 张，CLI 验证 3 张，共 31 次双模型检测（62 次模型 predict）。首次校准 7.185 秒，第二次结果为最终证据；额外从最终结果重画全套标签仅做可视化，无新推理、无结果选择或修改。',
        f'- 最终校准含读图、推理、蒙版/叠加图保存与总览共 {data["elapsed_seconds"]:.3f} 秒；D1 冷启动 {data["results"][0]["timing_seconds"]["total"]:.3f} 秒（含导入/模型加载），其余双模型单图 {min(warm):.3f}–{max(warm):.3f} 秒，均值 {mean(warm):.3f} 秒。检测耗时含框/蒙版转换，不含 JSON/叠加图写盘。首次冷启动 4.859 秒，存在缓存/环境初始化差异。',
        '- 工单墙钟约 15 分钟（会话起止估算）；安装约 90 秒，测试以日志的精确耗时为准，均在 60 分钟预算内。', '',
        '## 未完成、不确定与已知问题', '',
        '- 不宣称 D1/D3/D10/D11 人物计数校准达标；原始检出忠实呈现问题，没有调低门槛、改判或针对样本写特例。',
        '- 检测模型会合并多个相邻人物、包含反射、给背景背影面部候选；没有身份识别、反射分类或肢体归属能力。分割在 D1/D6 等有明显非主体污染，直接上传作重绘蒙版存在改错区域风险；本工单没有上传。',
        '- 只比较指定样本与 s 模型，未测试其它模型、尺度、NMS/置信度网格；默认 imgsize=640 把大图缩放推理。远景更小脸和其它画风的检出率尚未验证。',
        '- 冷启动与热推理耗时不可混用；单机短样本耗时不能代表长时间服务性能。',
        '- 分割外扩是空间膨胀，不能新增人物归属语义；框外扩或分割孔洞可能影响后续重绘，需要 P9-02 再校准接入。CLI 保存轮廓，原图形状/孔洞的精确重现应使用无损 PNG 与 hierarchy。',
        '- 按工单未接入生成/审核流程、颜色采样、重绘上传和 Qwen-Image 2.1；这些不在本工单范围。', '',
        '## 需要用户转派', '', '无。所有授权范围内操作均在 omarchy 本机工作目录完成。', '']
    (ROOT/'reports/P9-01.md').write_text('\n'.join(lines))

if __name__ == '__main__':
    main()
