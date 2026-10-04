#!/usr/bin/env python3
"""Render the human-reviewed module catalog against reproducible evidence counts."""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'audit'
COHORTS = ['jobs_cli', 'jobs_development', 'runs_development']

# role, feature, evidence (ticket/path), props-only branches, recommendation, reason
CATALOG = {
    '__init__': ['Python 包标记。', 'agent_record', [], '否。', '保留', '保留包标识；只有说明，没有可缩减的独立业务逻辑，也没有单独效果验证。'],
    '__main__': ['分派 Hermes 命令与旧 CLI 子命令。', 'agent_record',
                 [('P1-01', 'runs/p1_01_smoke_20260930/state.json'), ('P7-02 / Hermes T2', 'verify/hermes/t2b-approve.out')],
                 '否。', '简化', '保留 make/approve/status/resume/deliver/_worker；旧 new/run/gen-one/resolve/plan/review/detect 仅兼容或开发，宜与正式入口分离。'],
    'agent': ['任务接收、后台 worker、审批、恢复与离线交付门面。', 'agent_record',
              [('P7-02 / Hermes T2', 'verify/hermes/t2b-approve.out'), ('Hermes T3b', 'verify/hermes/t3b-resume-check.json'), ('D27 / Hermes T4', 'verify/hermes/t4-hermes-output.log')],
              '否；INTAKE_SCHEMA 的 possession_swap/prop_dependent/prop_only_narrative_items，以及 intake 中道具语义判断只服务道具范围告知。',
              '简化', '保留 worker/审批/锁/恢复；接收协议中的细分道具叙事项可缩为范围提醒，不删除人物流程。'],
    'assembly': ['拼装身份、服装、场景、镜头与道具 tag。', 'story_shot_attempt',
                 [('P1-04', 'review/P1-02-vs-P1-04.jpg'), ('P3-02', 'review/P3-02-芙莉莲-对照.jpg')],
                 '否；registry/prop_blocks/prop_bindings（19–28 行）及道具描述去重只服务道具；四块人物拼装共用。',
                 '简化', '保留 tag 拼装与景别；v2 道具仅陪衬，可去掉逐件绑定的工程要求。历史实测绑定不可靠。'],
    'budget': ['跨 tier、定妆、生成、审核累计用量并在服务边界限额。', 'cycle_record',
               [('P5-02', 'reports/P5-02.md'), ('P7-02', 'reports/P7-02.md')],
               '否。', '保留', '限额和重启持久化是核心；此处次数为留有循环记录的任务数，不是 Budget.check 调用数。'],
    'calibration': ['v1 逐项映射与可靠性统计。', None,
                    [('P4-02', 'calibration/results.json'), ('P4-02', 'calibration/summary.md')],
                    '否；mapped_status 的 props 聚合分支只服务道具，人物统计共用。', '保留', '不在生产入口，但真实校准与 v2 映射仍依赖它；离线工具不能当作死代码删除。'],
    'calibration_v2': ['v2 视觉与本机检测的固定校准映射。', None,
                       [('P9-04', 'calibration/v2/results.json'), ('P9-04', 'calibration/v2/summary.md')],
                       '否；经 calibration 继承 props 映射。', '保留', '离线校准被 v3 回归复用；统计规则不能因审核不可靠而删掉。'],
    'calibration_v3': ['v3 服装细节、额外主角、配饰放大的校准统计。', None,
                       [('P10-02', 'calibration/v3/results.json'), ('P10-03', 'calibration/v3.1/summary.md')],
                       '否。', '保留', '生产不调用，但已校准功能的发布与外部验证需要这些固定映射；误报结果同样是有效证据。'],
    'comfy': ['ComfyUI 提交、查询、下载、上传与模型元数据客户端。', 'submitted_attempt',
              [('P1-01', 'runs/p1_01_smoke_20260930/manifest.json'), ('Hermes T3b', 'verify/hermes/t3b-resume-check.json')],
              '否。', '保留', '共用基础设施；276 等数字只数独立 prompt_id，不把轮询算作生成。'],
    'config': ['加载并校验生成配置、合并固定画风参数。', 'generation_config_record',
               [('P2-01', 'config/style.json'), ('P2-01', 'review/P2-grid-A.jpg')],
               '否。', '保留', '固定资源、采样参数与预算需要统一配置；画风有效性以 P2 网格为证。'],
    'controls': ['冻结控制选项、校验和缓存上传参考、建立参考关系。', 'reference_upload_record',
                 [('P3-02', 'review/P3-02-芙莉莲-对照.jpg'), ('P9-02', 'reports/P9-02.md')],
                 '否；qwen layout 可用于人物构图，并非只为道具。', '保留', '实际 IPAdapter 参考注入和内容 hash 校验已用；上传记录不是服务调用次数。'],
    'cycle': ['冻结循环及运行时复用的诊断校验、候选审核、取舍和交付函数。', 'cycle_record',
              [('P5-02', 'review/P5-02-约尔-前后.jpg'), ('P10-05', 'reports/assets/P10-05/replay-supervisor.json')],
              '否；validate_edit_target 的 props 分支、道具 edit 枚举和旧 diagnose 提示只服务道具；当前调度通过 is_prop_item 排除道具修复。',
              '简化', '当前 pipeline 调用 runtime_gate；本文件的 run_repair_cycle/diagnose/apply_round 只由测试链可达，可消除重复调度，保留被复用的审核与 D29 比较器。D29 只有离线验证。'],
    'detect': ['本机 CPU 面部/人物检测、蒙版与像素几何。', 'group_detection_record',
               [('P9-01', 'calibration/detect/results.json'), ('P9-01', 'review/P9-01-detect.jpg'), ('P10-05', 'reports/P10-05.md')],
               '否；没有道具类别检测器。', '简化', '检测主体和参考脸是核心；crop_face 仅测试使用，与 detect_face_record 的裁切路径重复。计数阈值的离线回归不能替代新任务验证。'],
    'detection_review': ['本机主角计数、颜色观测、脸裁切、盲审缩图与修复蒙版。', 'group_detection_record',
                         [('P9-02', 'review/P9-02-inpaint.jpg'), ('P9-04', 'calibration/v2/summary.md'), ('P10-05', 'reports/P10-05.md')],
                         '否；面部/人物共用。配饰采样不是道具计数；人物分割的 spatial ownership 是人物区域归属。',
                         '简化', '保留计数/裁切/缩图；删减无可靠决策价值的颜色采样及 _sample 中早退后永远不会执行的旧 eye 分支。区域去人失败与面部局部有效须分开。'],
    'editing': ['把精确状态引用编译成指令编辑文本。', 'repair_output_instruction_edit',
                [('P5-02', 'review/P5-02-约尔-前后.jpg'), ('P9-06', 'review/P9-06-月城柳-前后.jpg')],
                '否；change_prop_state、remove_surplus、place 分支（35–63 行）只服务道具；set 与 remove_duplicate_protagonist 服务人物。',
                '简化', '保留人物属性与去重复；道具三种操作已被 D24 禁止进入新修复路径，历史记录可继续读。'],
    'eye_sampling': ['通过虹膜色团及发色/肤色排除采样瞳色。', 'observation_eye_color_sampled',
                     [('P9-04', 'calibration/v2/summary.md'), ('P9-02', 'review/P9-02-eyes.jpg')],
                     '否，服务人物。', '删除', '建议 v2 移除这一独立辅助功能并同步移除调用分支：校准误报 50%，目前只记录不否决，视觉瞳色已校准可靠。不是死代码；删除理由是价值不足，不是未调用。'],
    'fallback': ['L3 参考注入验证及 L4 诚实返回缺少的用户资料。', 'identity_final_L3',
                 [('P6-02', 'reports/P6-02.md'), ('P6-02', 'runs/p6_02/l4/unknown/needs-user.json'), ('P8-03', 'review/白雪姬-面包店打工.jpg')],
                 '否。', '保留', 'L3/L4 都有真实证据；参考用于生成/比对，不冒充原设来源。'],
    'identity': ['Danbooru 中文名消歧、来源查询与身份锁定。', 'identity_origin_record',
                 [('P6-01a', 'runs/p6_01a_rework1_resolve/05/identity.json'), ('P6-01b', 'reports/P6-01b.md')],
                 '否。', '保留', '所有正式任务的角色解析基础；歧义返回2个开发状态记录，不把候选资料解析当作新出图。'],
    'identity_enrichment': ['从 wiki 和共现资料补齐外貌并处理来源冲突与配饰归属。', 'identity_rules_record',
                            [('P6-01a', 'reports/P6-01a.md'), ('P10-01', 'reports/assets/P10-01/yor-reextracted.json')],
                            '否；服装配饰属于人物，不是道具。', '保留', '来源和冲突处理是身份保证基础；P10-01 已证明服装配饰分类流程，但当前资料不足仍未自动补全。'],
    'model_metadata': ['按远端文件指纹缓存 LoRA 元数据与缺失状态。', 'lora_metadata_inventory_record',
                       [('P9-06', 'reports/P9-06.md'), ('P9-06', 'jobs/p9_06_cake/tier/inventory.json')],
                       '否。', '保留', '解决大量无元数据 LoRA 的阻塞；inventory 内命中是保存标记，含复制的开发库存，不当作新 HTTP 调用次数。'],
    'outfits': ['从整套 wiki 服装中按实测频率选择默认服装与替代服装。', 'default_outfit_selection_record',
                [('P6-01a', 'reports/P6-01a.md'), ('P10-01', 'reports/assets/P10-01/yor-reextracted.json')],
                '否。', '保留', '人物服装范围的关键模块，避免把不同套装与统计配饰混锁为身份。'],
    'pipeline': ['角色、定妆、分镜、生成、修复与 manifest/拼图编排。', 'contact_sheet_task',
                 [('P1-02', 'review/芙莉莲-下雨后在车站发现自己拿错了伞.jpg'), ('Hermes T4', 'jobs/cg-129ca5876d7b/contact_sheet.jpg')],
                 '否；props 参数传递和 qwen edit 的物体绑定可简化，但 tier/定妆/交付均服务人物。',
                 '保留', '主流程与完整交付已实测；不要把未达到叙事效果等同于模块未用。'],
    'planning': ['请求并校验身份隔离、单瞬间、单主角的结构化分镜。', 'planning_call',
                 [('P1-04', 'review/P1-02-vs-P1-04.jpg'), ('P9-05', 'reports/P9-05.md'), ('P10-04', 'reports/P10-04.md')],
                 '否；PROP/PROP_STATE、登记去重、holder/owner/state、同类物体对照、possession_swap 锁定只为道具；单姿态/表情、景别、服装/外貌隔离为人物共用。',
                 '简化', '保留人物分镜约束；缩掉道具逐件状态协议。新 scope 每镜最多1关键道具，同类两物体对照校验只在旧分镜仍有意义。P10-04 动作解析修复生效，画面效果仍部分达标。'],
    'portrait_gate': ['D7 定妆聚合及旧原始失败记录的合规恢复。', 'd7_portrait_record',
                      [('P5-02 / D7', 'jobs/cg-9b3e05ab005b/portrait_gate_original.json'), ('Hermes T4', 'jobs/cg-129ca5876d7b/portrait.json')],
                      '否。', '保留', '保留原审核并说明无来源不可见项；门禁通过仍不证明服装长短正确，P10-04 暴露该缺口。'],
    'posture_tags': ['从有来源的姿态词表约束姿態仅出现于单瞬间块。', 'shot_contract_v2_board',
                     [('P9-05', 'reports/P9-05.md'), ('P9-05', 'cache/tag_groups/posture.provenance.json')],
                     '否；tag_fields 的 props/关系/位置遍历仅为防止道具块夹带姿态。',
                     '保留', '单瞬间人物控制有实际分镜；来源校验与固定词表可复用，不能用未验证姿态承诺可画。'],
    'reconcile': ['按 prefix 与 prompt_id 对账队列/历史，决定等待、下载或提交。', 'saved_prompt_status_transition',
                  [('P1-01', 'runs/p1_01_smoke_20260930/state.json'), ('P5-01b', 'reports/P5-01b.md'), ('Hermes T3b', 'verify/hermes/t3b-resume-check.json')],
                  '否。', '保留', '中断不重复提交有独立验收；原目录 submitted、续跑目录 completed 的同 prompt_id 也是历史证据。无精确函数调用计数。'],
    'reference': ['过审定妆、内容 hash 缓存、L3 复用及构图编辑指令。', 'portrait_origin_record',
                  [('P3-01', 'review/P3-01-pilot.jpg'), ('P3-01', 'runs/p3_01_cache_probe_20260930/portrait.json'), ('P6-02', 'reports/P6-02.md')],
                  '否；edit_instruction 中 bindings/Holder/Position 只服务道具；定妆/脸参考/缓存服务人物。',
                  '保留', '定妆与 IPAdapter 有真实改善；缓存出现6个非修复副本的命中目的地，其中1个是L3复用。'],
    'repairs': ['持久化指令编辑、面部精修、重布局、局部重绘原语及复审记录。', 'repair_output_instruction_edit',
                [('P5-01b', 'review/P5-01b-repairs.jpg'), ('P5-02', 'review/P5-02-约尔-前后.jpg'), ('P9-02', 'review/P9-02-inpaint.jpg'), ('P9-06', 'review/P9-06-月城柳-前后.jpg')],
                '否；region_queries/region_prompt、prop_ids 子集、物体移除/放置路径只服务道具；面部/人物去重、合成边界共用。',
                '简化', '保留经过实测的人物修复；移除道具实验入口与只被测试的 rewrite_composition/composite_region。inpaint 出现3次开发提交，不等于物体区域修复已验证有效。seed 原语静态可达但无历史执行记录。'],
    'review': ['三层审核、逐项可靠性、人物/服装/道具检查与图像盲审。', 'review_request',
               [('P4-02', 'calibration/summary.md'), ('P9-04', 'calibration/v2/summary.md'), ('P10-02', 'calibration/v3/summary.md')],
               '否；prop_checklist（300–307）、audit_single 的 prop_* / prop_contact 和逐件清点提示只服务道具；人物、一致性与盲审共用。',
               '简化', '保留已校准的人物项、盲审和原始结论。按 D24 现阶段仍必须记录道具，建议 v2 定稿后缩减清点协议，不能直接漏审或改判。旧 VLM bbox/crop_face 仅工具仍用。'],
    'review_gate': ['在不改原审核的前提下应用检测可靠性与人物范围交付门禁。', 'group_gate_record',
                    [('P9-05', 'reports/P9-05.md'), ('P10-01', 'reports/assets/P10-01/replay-summary.json')],
                    '否；prop_* 已知问题和纯道具 narrative 降级分支只服务道具范围；可靠人数否决服务人物。',
                    '保留', '范围门禁与冻结原审核应分开保存；旧任务缺结构化字段时保守处理。'],
    'review_v3': ['检测定位后放大头部、比对服装细节并审核额外主角候选。', 'supplement_review_request',
                  [('P10-02', 'calibration/v3/summary.md'), ('P10-03', 'calibration/v3.1/summary.md'), ('P10-04', 'review/P10-04-菲伦-前后.jpg')],
                  '否；均为人物/服装/配饰，非道具。', '简化', '服装细节部分可靠但参考可能错；小号重复主角与配饰放大已冻结验证为不可靠，可从 v2 默认热路径移出，保留研究/结果记录。'],
    'runner': ['生成状态先落盘、幂等提交、下载校验与重启恢复。', 'submitted_attempt',
               [('P1-01', 'runs/p1_01_smoke_20260930/state.json'), ('Hermes T3b', 'verify/hermes/t3b-resume-check.json')],
               '否。', '保留', '所有已提交图的持久化底座；保留 diagnosis 约束及 prefix 对账，不能用换 seed 重试代替修复。'],
    'runtime_gate': ['正式运行时人物范围修复循环、检测路由、D29 取舍与交付。', 'group_gate_record',
                     [('P9-06', 'review/P9-06-月城柳-前后.jpg'), ('P10-01', 'reports/assets/P10-01/replay-summary.json'), ('P10-05', 'reports/assets/P10-05/replay-supervisor.json')],
                     '否；D24 的 props 拒绝/过滤分支只为守住道具范围；循环主体服务人物。',
                     '简化', '与 cycle 的重复循环宜合并为一条调度路径；保留当前范围门禁。detection_improved 无调用者。D29 新向量在线痕迹0，已保存历史不会因代码更新变为新取舍。'],
    'shot_contract': ['定义并编译单姿态、表情、单主角及反射约束。', 'shot_contract_v2_board',
                      [('P9-05', 'reports/P9-05.md'), ('P10-01', 'reports/P10-01.md'), ('P10-04', 'reports/P10-04.md')],
                      '否；owner/distinguish_ownership/establishes_ownership/distinguishing_features_visible、possession_swap 及 owner 一致性只服务道具。',
                      '简化', '保留单瞬间编译与反射；D19/D25 已删掉强制交换/对照结构，现存 possession_swap 仅记录，不应误写成仍在强制交换。'],
    'state': ['prefix 校验、JSON 原子写入与状态加载。', 'state_record',
              [('P1-01', 'runs/p1_01_smoke_20260930/state.json'), ('Hermes T3b', 'verify/hermes/t3b-resume-check.json')],
              '否。', '保留', '多进程与恢复需要；文件数包含 tier/定妆/副本，不冒称写入次数。'],
    'story_rules': ['校验主角可见性、匿名其他人、手归属和物体脱离。', 'storyboard_record',
                    [('P5-02', 'review/P5-02-约尔-前后.jpg'), ('P8-03', 'reports/P8-03.md')],
                    '否；for key_props 中 detached_separation/detached_reaction 是道具规则；other_holder 同时涉及持物者与匿名人物；obscured_limit/restricted_people/浮空手规则服务人物。',
                    '简化', '保留人物可见/单主角约束；物体脱离和持有叙事协议可随 v2 陪衬道具范围缩掉。已有6条 detached=true 绑定，分支实际通过/拒绝次数未记录。'],
    'tags': ['核验真实 Danbooru tag、精确动作解析和提示词块清洗。', 'tag_validation_call_record',
             [('P1-04', 'reports/P1-04.md'), ('P10-04', 'reports/P10-04.md')],
             '否；verify_storyboard_tags 遍历道具描述/关系/位置，但核验算法与人物/场景共用。',
             '保留', '来源和精确匹配是提示词质量基础；tag 存在不能证明模型稳定会画，需结合能力摸底。'],
    'tier_gate': ['在冻结角色比对之上按 D18/D20 合法聚合身份层级。', 'tier_gate_artifact',
                  [('P9-06 / D18', 'jobs/p9_06_cake/tier_gate.json'), ('P10-01 / D20', 'reports/P10-01.md')],
                  '否，配饰否决规则属于人物身份。', '简化', '保留可靠性/配饰政策与原观察；可统一重复层级聚合接口。独立产物仅开发3个，ensure 由正式入口可达但未记逐函数调用。'],
    'tiers': ['训练 tag 匹配角色 LoRA、纯知识探针、来源比对及生成配置路由。', 'tier_comparison_request',
              [('P6-01b', 'reports/P6-01b.md'), ('P6-02', 'reports/P6-02.md'), ('P9-06', 'reports/P9-06.md')],
              '否。', '简化', '保留已用 L1/L2 探针与路由；determine_l1/prepare_l2/build_l2_workflow 仅测试，不是当前 live_inventory/ensure 路径。层级判定有效性仍受可见性和参考服装影响。'],
    'v1_scope': ['D23–D25 人物范围过滤、道具依赖告知与交付说明。', 'person_scope_gate_record',
                 [('P10-01', 'reports/assets/P10-01/replay-summary.json'), ('Hermes T4', 'jobs/cg-129ca5876d7b/result.md')],
                 '否；is_prop_item/纯道具 narrative/PROP_NOTICE 专门管理道具范围，但人物保留与 render_delivery 服务完整交付。',
                 '保留', '需要明确人物保证及已知问题；不能把范围过滤误当作放宽原审核阈值。正式2组已有 D23/D24 门禁。'],
    'workflow': ['构建 WAI、角色 LoRA、IPAdapter、img2img 和 Qwen 编辑节点图。', 'submitted_attempt',
                 [('P2-01', 'review/P2-grid-A.jpg'), ('P3-01', 'review/P3-01-pilot.jpg'), ('P9-02', 'review/P9-02-inpaint.jpg')],
                 '否；各节点链服务人物和构图，不因曾用于道具就成为道具专用。',
                 '保留', '实际 graph/seed/参数保存在 attempts；保留经过实测的节点链，不引入新模型。'],
}


def link(path, label=None):
    return f'[{label or path}](../{path})'


def main():
    static = json.loads((OUT / 'static.json').read_text())
    stats = json.loads((OUT / 'log_stats.json').read_text())
    by_stem = {Path(m['file']).stem: m for m in static['modules']}
    assert set(by_stem) == set(CATALOG)
    records = []
    evidence = []
    for stem, module in sorted(by_stem.items(), key=lambda row: row[1]['file']):
        role, feature, proofs, prop, suggestion, reason = CATALOG[stem]
        f = stats['features'].get(feature, {})
        counts = f.get('counts', {c: 0 for c in COHORTS})
        numbers = '/'.join(str(counts[c]) for c in COHORTS)
        if stem.startswith('calibration'):
            base = {'calibration': 'calibration', 'calibration_v2': 'calibration/v2', 'calibration_v3': 'calibration/v3'}[stem]
            result = json.loads((ROOT / base / 'results.json').read_text())
            usage = f'只在开发期（C/D/R=0；离线 {len(result["records"])} 条校准观测；工具入口）'
        elif stem == '__init__':
            usage = '是（包初始化；无业务函数及独立调用计数）'
        elif stem == 'reconcile':
            usage = f'是（C/D/R={numbers} 同 prompt 状态转移；另 Hermes T3b 恢复验收；调用数未知）'
        elif stem == 'tier_gate':
            usage = f'是（入口可达；C/D/R={numbers} 独立聚合产物，ensure 调用数未知）'
        else:
            usage = f'是（C/D/R={numbers}；单位 {feature}）'
        proof_text = '；'.join(f'{ticket}：{link(path)}' for ticket, path in proofs) or '无（无独立行为；没有单独验证工单）'
        for ticket, path in proofs:
            q = ROOT / path
            assert q.exists(), (stem, path)
            evidence.append({'module': module['file'], 'ticket': ticket, 'path': path,
                             'is_file': q.is_file(), 'sha256': hashlib.sha256(q.read_bytes()).hexdigest() if q.is_file() else None})
        records.append({'file': module['file'], 'lines': module['lines'], 'role': role,
                        'usage': usage, 'feature': feature, 'counts': counts,
                        'evidence': proof_text, 'props_only': prop,
                        'suggestion': suggestion, 'reason': reason,
                        'facade_reachable_definitions': module['facade_reachable_functions'],
                        'all_cli_reachable_definitions': module['all_cli_reachable_functions']})
    totals = Counter()
    for row in records:
        totals[row['suggestion']] += row['lines']
    total = sum(totals.values())
    dead = [f for f in static['functions'] if not f['all_cli_reachable']]
    test_roots = {f['symbol'] for f in static['functions'] if any(r['group'] == 'tests' for r in f['external_refs'])}
    tool_roots = {f['symbol'] for f in static['functions'] if any(r['group'] in ('tools', 'verify', 'probe', 'skill') for r in f['external_refs'])}
    adjacency = {}
    for e in static['edges']:
        adjacency.setdefault(e['caller'], set()).add(e['callee'])
    def closure(roots):
        found = set(roots);pending = list(roots)
        while pending:
            for next_symbol in adjacency.get(pending.pop(), set()) - found:
                found.add(next_symbol);pending.append(next_symbol)
        return found
    test_reached, tool_reached = closure(test_roots), closure(tool_roots)
    test_only = [f for f in dead if f['symbol'] in test_reached and f['symbol'] not in tool_reached]
    tools_only = [f for f in dead if f['symbol'] in tool_reached]
    no_ref = [f for f in dead if f['symbol'] not in test_reached | tool_reached]

    header = '''# AUD-01：v1 代码与历史证据审计

审计对象为当前 `src/storycg/*.py` 的全部41个模块。只提出 v2 删减建议；没有运行生产入口、出图、调用模型、改代码或改历史结论。优先依据 TASK-CAP §3、AGENTS 补充6/7及 PROGRESS D1–D29，尤其 D23–D25 的人物范围。

**计数口径**：C=`jobs/cg-*` 的 CLI 门面任务（包含 Hermes 验收与开发回归，不意味着全部是生产流量）；D=`jobs/p*` 的 P 工单回归；R=`runs/*` 的开发实验。核对到 Hermes T2/T3b/T4 的交付目录分别为 `cg-9b3e05ab005b`、`cg-76d2eec57dfc`、`cg-129ca5876d7b`。其他 cg 目录只归 CLI，不擅自认定 Hermes 用户调用。

表中“是”由当前入口可达和该功能历史产物交叉支持。次数是明确列出的**功能痕迹单位**：请求、任务、产物、状态或去重观察，**不是 Python 函数调用次数**；同一次运行会为多个共用模块提供证据，不能把模块行相加。历史任务没有记录执行源码 hash，不能证明现行每个分支当时执行过。无独立效果验证的项目明确标注；验证通过/部分通过/不可靠详见后文，不用“留有文件”冒充效果达标。

全部原始路径、逐条 JSON 指针/日志行号和分组任务目录在 [log_stats.json](log_stats.json)、[events.jsonl](events.jsonl)；[functions.tsv](functions.tsv) 列出每个定义的正式/全部CLI可达性及测试/工具引用；[callgraph.md](callgraph.md)、[callgraph.dot](callgraph.dot) 为调用关系。行数包含空行与注释，按 `wc -l` 等价物理行计数。

| 文件 | 行数 | 作用（一句话） | 实际任务中是否调用（次数） | 验证证据（工单与路径） | 是否只为道具 | 建议 | 理由 |
|---|---:|---|---|---|---|---|---|
'''
    lines = [header.rstrip()]
    for r in records:
        cells = [link(r['file']), str(r['lines']), r['role'], r['usage'], r['evidence'], r['props_only'], r['suggestion'], r['reason']]
        lines.append('| ' + ' | '.join(x.replace('|', '\\|').replace('\n', ' ') for x in cells) + ' |')
    lines += ['', '## 汇总', '', f'- 总计 **{len(records)} 个模块、{total:,} 行**。',
              f'- 建议整模块删除：**{totals["删除"]} 行（{totals["删除"]/total:.2%}）**，仅 `eye_sampling.py`；它仍在运行，只是校准后没有可靠决策价值。需要 v2 工单同步移除依赖，AUD-01 未实施。',
              f'- 建议简化的模块合计 **{totals["简化"]:,} 行（{totals["简化"]/total:.2%}）**；保留模块合计 **{totals["保留"]:,} 行（{totals["保留"]/total:.2%}）**。这是“受简化建议影响的整文件行数”，**不是可以删除这么多行**，不把混合模块的全部代码冒算为道具代码。',
              '- 没有整模块只服务道具；道具专用函数/分支列在表中和下文。3个 calibration 模块不在生产入口，但用于真实离线校准及相互复用，建议保留。',
              f'- 当前保守 AST 图：{len(static["functions"])} 个类/函数定义，正式门面可达 {sum(f["facade_reachable"] for f in static["functions"])} 个，全部CLI可达 {sum(f["all_cli_reachable"] for f in static["functions"])} 个；全CLI不可达 {len(dead)} 个定义（含嵌套函数）。[static.json](static.json) 记录解析方法和不确定边。',
              '', '## 只被测试引用的生产死代码', '',
              '这里“死”只指**从当前生产/全部CLI入口不可达**，不是从来没运行或可以立即删除。AST 同时追踪函数作为参数的引用和测试调用链；嵌套函数不重复计算可删除行数。普通工具/校准代码另列，不混进只测代码。', '',
              '| 定义与行号 | 当前可达性 | 测试证据/上游 | 建议 |', '|---|---|---|---|']
    for f in test_only:
        refs = sorted({r['file'] for r in f['external_refs'] if r['group'] == 'tests'})
        reference = '、'.join(link(x) for x in refs) if refs else '测试通过 ' + '、'.join('`' + x + '`' for x in f['callers'])
        lines.append(f'| `{f["symbol"]}`（{f["line"]}–{f["end"]}） | 正式/全CLI均不可达 | {reference} | 合并替代路径后移除；保留历史验证证据 |')
    lines += ['', '单独的工具/校准链（当前CLI不可达，但不能称为“只被测试引用”）：', '']
    for f in tools_only:
        refs = sorted({r['file'] for r in f['external_refs'] if r['group'] != 'tests'})
        lines.append('- `' + f['symbol'] + '`：' + ('、'.join(link(x) for x in refs) if refs else '由上述工具调用的校准辅助函数') + '。')
    lines += ['', '无入口、测试或工具调用链证据的定义：', '']
    for f in no_ref:
        lines.append(f'- `{f["symbol"]}`（{f["line"]}–{f["end"]}）：无。')
    lines += ['', '## 历史运行统计', '',
              f'扫描 jobs/runs 共 {stats["files"]:,} 个现有文件，解析 {stats["json_files"]:,} 个 JSON；JSON/JSONL 解析失败 {len(stats["parse_errors"])}。记录到 {len(stats["task_records"])} 个 `job.json` 目录（C12、D7、R28；R含修复副本）；候选解析目录另外计入身份状态，不当作完整任务。',
              '', '| 功能痕迹 | C：CLI | D：jobs开发 | R：runs开发 | 单位/限制 |', '|---|---:|---:|---:|---|']
    metric_rows = [
        ('submitted_attempt', '独立已提交图', '不同 prompt_id；包括 tier/定妆/布局；排除 inherited 与同ID副本'),
        ('review_request', '审核客户端请求', '不同 call_id；含文本诊断，不等于全部是图片审核'),
        ('vision_request', '非 cycle_llm 审核请求', '含无图叙事比较与定位；不是本工单新调用'),
        ('diagnosis_request', 'cycle 文本诊断', '不同 call_id'),
        ('planning_call', '分镜模型尝试', '日志 calls 条目，成功/失败均计；不是有效分镜数'),
        ('agent_llm_call', '接收/展示模型尝试', 'agent_llm_log calls 条目'),
        ('cycle_record', '留有 cycle 的任务', '原冻结循环与后续运行时循环均在内；R的2份P9继承旧history'),
        ('single_review_request', '单张审核', '不同 call_id'),
        ('consistency_review_request', '身份/服装/相邻一致性审核', '不同 call_id'),
        ('prop_audit_request', '带道具检查的单张请求', 'schema含 prop_contact 或 prop_n；记录不代表保证归属可读'),
        ('blind_review_request', '只看图盲审', '不同 call_id'),
        ('supplement_review_request', 'v3 补充审核', '不同 call_id；仅当前2个CLI任务留下请求'),
        ('identity_final_L1', '最终 L1', '每个非修复副本 identity.json 一个最终状态，不是判级次数'),
        ('identity_final_L2', '最终 L2', '包含开发期单角色探针目录'),
        ('identity_final_L3', '最终 L3', '包含1次正式缓存复用，不全是新L3验证'),
        ('identity_final_L4', '最终 L4', '包含两轮开发 L4 控制目录，不能把7条状态当作7个不同角色'),
        ('portrait_cache_hit_task', '定妆缓存命中目的地', '排除修复副本/缓存仓库；含1次L3复用'),
        ('portrait_cache_marker', '全部 cache_hit=true 标记', '含继承副本11份；不是17次实际命中调用'),
        ('l3_reuse_record', 'L3 复用', 'portrait_cache_source 记录'),
        ('lora_metadata_cache_hit_marker', 'LoRA 元数据缓存命中标记', 'inventory 保存条目；含复制库存，非精确HTTP次数'),
        ('shot_contract_v2_board', '单瞬间 v2 分镜', '最终保存的分镜，不包括失败的LLM响应'),
        ('scope_board_record', '人物范围分镜', 'scope_version=1 的保存分镜'),
        ('intake_possession_swap_true', '接收 possession_swap=true', '语义记录，非强制交换执行'),
        ('board_possession_swap_true', '最终分镜 possession_swap=true', '记录，不代表交换画成功'),
        ('detached_prop_binding', 'detached=true 绑定', '最终分镜状态；不是规则分支覆盖次数'),
        ('saved_prompt_status_transition', '同ID的 submitted→completed', '老状态/续跑状态对照'),
        ('worker_phase_log', 'worker 阶段日志', '阶段变更行数，不是 worker 启动数'),
        ('d29_comparison_record', 'D29 新严重度比较在线结果', '为0；离线P10-05回放不写入历史cycle'),
    ]
    for feature, label, unit in metric_rows:
        c = stats['features'].get(feature, {}).get('counts', {c: 0 for c in COHORTS})
        lines.append('| ' + label + ' | ' + ' | '.join(str(c[k]) for k in COHORTS) + ' | ' + unit + ' |')
    lines += ['', '修复按**首次持久化 stage prefix**去重；不把 relayout 的两张阶段图算成两次修复，也不把 prepared decision 冒充提交。下表为生成了 output 的独立修复请求（仍可能效果失败）：', '',
              '| 修复原语 | C output | D output | R output | C/D/R 循环选择为改善 | 结论 |', '|---|---:|---:|---:|---|---|']
    for method, note in [
        ('instruction_edit', '人物去重复有成功案例；旧道具编辑有失败，D24已禁新道具修复'),
        ('face_detail', '局部瞳色改善有实证；不可据此宣称整镜身份全通过'),
        ('relayout', '人物动作/构图有改善；道具复制问题仍在'),
        ('inpaint', '仅开发期3次；物体定位两例/人物去除未验证有效'),
        ('seed', '没有已提交/产出证据；接口分支可达，但当前循环映射不选seed')]:
        c = stats['features'].get('repair_output_' + method, {}).get('counts', {c: 0 for c in COHORTS})
        improved = stats['features'].get('cycle_accepted_' + method, {}).get('counts', {c: 0 for c in COHORTS})
        lines.append('| ' + method + ' | ' + ' | '.join(str(c[k]) for k in COHORTS) + ' | ' + '/'.join(str(improved[k]) for k in COHORTS) + ' | ' + note + ' |')
    lines += ['', '**修复统计补充**：cycle 原样保留44条历史 decision（C18/D15/R11），其中17条以 props 为问题；R中的两个P9目录继承旧cycle，不能把其全部history当作新运行。`state.repairs` 新stage去重的 output 合计53次才是本扫描采用的原语执行口径；cycle 的“改善”是当时策略的选择结果，不是人眼效果验收，更不能用 D29 事后改判。不同目录的复制/恢复差异已记录在 events 中。',
              '', '**缓存具体路径**：', '']
    for c in COHORTS:
        for p in stats['features'].get('portrait_cache_hit_task', {}).get('paths', {}).get(c, []):
            lines.append('- ' + link(p) + '。')
    lines += ['', '**inpaint 具体路径**：', '']
    for c in COHORTS:
        for p in stats['features'].get('repair_output_inpaint', {}).get('paths', {}).get(c, []):
            lines.append('- ' + link(p) + '（对应 `/repairs/*` 的 request、stage、output；全部是开发实验）。')
    lines += ['', f'请求/响应不完整：{stats["requests_without_response"]} 条 request 未找到同 call_id response；{stats["responses_without_request"]} 条 response 未找到 request。保留这些缺口，不补造成功调用。扫描的276个独立 prompt_id 均有 completed 状态，但任务目录之外的校准/实验出图没有纳入；**这不是 PROGRESS 的开发预算总计**。',
              '', '## 道具分支与删减边界', '',
              '- **登记与绑定**：planning 的 PROP_STATE/PROP、143–194行登记/同类物体区分；assembly 的 registry/prop_blocks/bindings；reference.edit_instruction 的逐件 Holder/Position。可以在 v2 改为陪衬 tag，但这不等于删身份、服装、场景或动作块。',
              '- **逐件清点**：review.prop_checklist、audit_single 的 prop_contact/prop_n 与 SINGLE_PROMPT 的逐个物体 counting/holder/state 证据。当前 D24 仍要求审核记录，不允许 AUD-01 自行省略或改判。',
              '- **归属/状态**：shot_contract 的 owner/ownership evidence flags、planning 的 owner/state/held_by 一致性与同类物体对照；editing 的 change_prop_state/remove_surplus/place；repairs 的 region_queries/region_prompt/prop_ids。新运行时已禁道具修复，但底层公共原语仍带旧分支。',
              '- **possession_swap**：agent 的语义接收、planning 的 intake 锁定、shot_contract 的 schema/提示、v1_scope 的兼容告知。D19/D25后它只作记录，强制先建立归属/交换/双物体对照三条规则已删；不能建议删除不存在的现行逻辑。',
              '- **物体脱离**：story_rules 的 detached_separation/detached_reaction 及 planning/shot_contract 必填 detached。其他人的手归属和人体计数也使用“owner/ownership”一词，不能一见该词就算道具代码。',
              '- **范围隔离**：v1_scope、review_gate、runtime_gate 对 prop_* 和纯道具叙事项的处理，承担“只保证人物，同时如实保留已知问题”的职责。v2可简化协议，但保留该责任；不修改冻结观察、可靠性阈值或历史交付。',
              '', '## 验证证据的效力与局限', '',
              '- P1/P7 的状态、恢复和 Hermes 交付说明证明流程使用；P2 的网格、P3 身份对照证明特定机制有效。第二角色P3背影对照无法证明身份改善。',
              '- P4/P9-04 的视觉发色、服装、道具、动作及后来瞳色曾达到校准门槛；瞳色采样误报50%，配饰/发型/不可见性仍不可靠或未验证。P9-04 人数小样本可靠结论后来被P10-04漏检反例修正，D28离线复验改变2/418张计数，不能仍写“任意人物100%可靠”。',
              '- P10-02 服装细节校准按O9真值勘误为10/10检出、0/10误报；它比的是所选定妆图。P10-04 定妆短外套与锁定long_coat矛盾，所选参考本身可能错，校准不保证参考正确。',
              '- P10-03 小号重复主角候选层检出75%、误报33.33%，系统检出42.86%、误报25%，仍为unreliable。配饰放大50%检出/38.89%误报，D22转接受局限；不以“代码跑过”推荐继续默认投入。',
              '- P5物体区域重绘没有效果通过；P9-02返工后的面部检测蒙版有局部瞳色改善，人物去重靠指令编辑成功。不能把面部改善移作物体inpaint有效的证据。',
              '- P10-05 D28/D29只有真实旧图/保存审核的离线检测、回放及测试。此次不复跑生产tests（会写非audit目录且部分启动后台/API）；不出图、不调用视觉模型，也不重新判图。',
              '', '## 不确定的判断及原因', '',
              '1. **模块调用次数与最新版本覆盖**：历史产物没有逐函数 trace/源码hash；共享 helper、D7 gate_accepts/recover 分支、reconcile 中不同对账分支、seed 原语、规则通过路径的次数无法从现有日志精确还原。表给功能痕迹单位，不捏造次数。当前静态可达是保守的可能执行关系。',
              '2. **动态边**：GuardedComfy.__getattr__、CycleClient 的继承、外部 httpx/PIL/YOLO 方法、函数参数回调存在动态分派。static.json 的 receiver_hint/callback_reference/manual 标签明确区别于可解析直接调用；无调用证据的定义不是数学上永远不能被动态插件调用。仓库内显式入口/引用已全部检查。',
              '3. **只在开发期的实用代码**：calibration 与 fork_repair_job、旧 crop_face/region_queries 都有工具调用或真实校准，不等于生产模块。“只测”清单已排除这些工具链。frozen cycle 调度历史上确实用过，当前生产不再选择它；删除需保留公共校验与原审核引用。',
              '4. **继承和覆写**：repair_source/inherited、portrait_cache、同 prompt_id、同修复stage、call_id已去重；job/identity/inventory等状态文件可能仍是来源副本。cycle history 和部分元数据命中保存标记无法恢复当时真实调用次数。被后来manifest/交付覆写掉的旧状态无法补回。',
              '5. **眼色采样删减建议**：该功能仍运行，D11曾要求实现；删除是基于校准价值的v2建议，需用户/监工在v2删减工单确认，不是本次未经授权修改。服装细节、主角计数与L1–L4不建议仅因有失败案例删除。',
              '6. 若需要精确到当前每个分支的覆盖，可另派工单在本地受控完整任务中记录 worker/子进程函数trace及源码hash，至少覆盖审批编辑、断点resume、缓存命中、L3/L4、一个可靠人物修复与offline deliver；运行前再给出出图/模型预算。**AUD-01没有自行运行**。',
              '', '## 复跑命令与辅助产物', '',
              '```bash', 'cd /work/workspace/manga_skill',
              'python3 -B audit/static_scan.py', 'python3 -B audit/log_stats.py', 'python3 -B audit/render_audit.py',
              'python3 -B audit/verify_audit.py', 'python3 -B audit/static_scan.py --verify-protected', '```', '',
              '全部脚本只用Python标准库，不导入storycg，不访问网络/ComfyUI/视觉服务，不生成图片；输出仅在audit。`--snapshot`仅用于开工基线，不应在复核中覆盖原基线。原历史目录变动会导致输入hash复核失败，需先确认是监工后续工作还是审计写入。', '',
              '- [static_scan.py](static_scan.py)、[static.json](static.json)、[functions.tsv](functions.tsv)、[call_edges.tsv](call_edges.tsv)、[callgraph.dot](callgraph.dot)、[callgraph.md](callgraph.md)。',
              '- [log_stats.py](log_stats.py)、[log_stats.json](log_stats.json)、[feature_counts.tsv](feature_counts.tsv)、[events.jsonl](events.jsonl)、[input_files.json](input_files.json)、[text_logs.json](text_logs.json)。',
              '- [render_audit.py](render_audit.py)、[modules.tsv](modules.tsv)、[module_evidence.json](module_evidence.json)、[validation_evidence.json](validation_evidence.json)、[verification.json](verification.json)、[protected-check.json](protected-check.json)。', '']
    (OUT / 'AUDIT.md').write_text('\n'.join(lines))
    (OUT / 'module_evidence.json').write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n')
    (OUT / 'validation_evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    with (OUT / 'modules.tsv').open('w', newline='') as stream:
        columns = ['file', 'lines', 'role', 'usage', 'feature', 'counts', 'evidence', 'props_only', 'suggestion', 'reason']
        writer = csv.DictWriter(stream, delimiter='\t', fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for r in records:
            writer.writerow({**r, 'counts': json.dumps(r['counts'])})
    write_graph(static, test_only, tools_only, no_ref)
    (OUT / 'summary.json').write_text(json.dumps({'module_count': len(records), 'lines': total,
        'recommendation_lines': dict(totals), 'test_only_symbols': [f['symbol'] for f in test_only],
        'tools_only_symbols': [f['symbol'] for f in tools_only], 'unreferenced_symbols': [f['symbol'] for f in no_ref]}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'modules': len(records), 'lines': total, 'recommendation_lines': dict(totals),
                      'test_only': len(test_only), 'tools_only': len(tools_only), 'no_reference': len(no_ref)}, ensure_ascii=False))


def write_graph(static, test_only, tools_only, no_ref):
    text = '''# 入口与调用关系

## 正式入口和 worker

```mermaid
flowchart TD
  M[__main__.main] --> A[agent.main]
  A --> MAKE[make: config + controls + pipeline.new_job]
  A --> AP[approve: optional edit_board]
  A --> ST[status: read + locks + worker_alive + budget.count_usage]
  A --> RS[resume: identity inputs / targeted repair]
  A --> DE[deliver: selected saved attempts]
  MAKE --> LA[launch subprocess: python -m storycg _worker]
  AP --> LA
  AP --> ED[planning.plan_story + validate + present]
  RS --> LA
  RS --> RGRE[runtime_gate.reopen_targeted_repair]
  LA --> W[agent.execute: intake / resolve / plan / present]
  A --> FW[_worker or --foreground: execute]
  FW --> W
  W --> WAIT[awaiting_approval]
  W --> G[agent.generate: resources + guarded clients]
  G --> P[pipeline.run_story_job]
  P --> T[tier_gate.ensure_identity_tier]
  T --> TS[tiers.ensure / live_inventory / match / compare]
  TS --> META[model_metadata]
  TS --> FB[fallback.ensure_l3 / needs_user L4]
  P --> PORT[reference.ensure_portrait + portrait_gate]
  P --> AS[assembly.assemble_prompt]
  AS --> POST[posture_tags + shot_contract]
  P --> RUN[runner.run_gen_one]
  RUN --> WF[workflow.build_workflow]
  RUN --> REC[reconcile.reconcile_attempt on recovery]
  RUN --> C[ComfyClient via GuardedComfy]
  P --> RG[runtime_gate.run_repair_cycle]
  RG --> CF[cycle shared helpers: diagnose schema / validate / candidate audit / selection / delivery]
  RG --> REP[repairs.repair + editing.grounded_instruction]
  CF --> REV[review + review_v3 + detection_review + detect]
  PORT --> REV
  DE --> SHEET[pipeline.make_contact_sheet]
  DE --> GATE[review_gate.persist_group_gate + v1_scope.render_delivery]
  RG --> GATE
```

`make/approve/resume` 的 foreground 路径直接调用 execute；后台 `launch` 通过 subprocess 重回 `__main__` 的 `_worker`，不是静态Python调用，已在 static.json 标为manual边。status/deliver 不主动启动生成或模型审核；此处是源码审计描述，AUD-01没有运行这些命令。

## 旧 CLI 和开发入口

`__main__.main` 还可执行 new/run/gen-one/resolve/plan/review/detect；旧状态无agent.json时 status/resume 走 runner 与 pipeline。只从这个旧入口新增可达的定义包括 __main__._summary、detect.cli/draw_overlay、pipeline.resolve_and_validate_job、runner.resume_job（__main__.main本身是所有入口的引导层）。正式门面的最上层__main__分派也实际会执行；为隔离旧分支，AST“facade”根从agent.main开始。

3个 calibration 模块仅由tools/校准入口调用，互相复用 mapped_status/summarize，不是 worker 热路径。它们在callgraph.dot中无活跃调用边，但有真实离线验证证据。

## 保守 AST 规则

AST不导入生产包；解析直接名称、别名、局部import、`from cycle import *`和runtime_gate同名覆盖。函数调用按**函数定义所在模块的全局环境**解析，所以cycle.repair_schedule仍调用cycle.repair_findings，而runtime_gate调用它自己的repair_findings。类构造、self方法、继承、被传入函数的引用、client/vlm类型提示、代理协议和worker边分别标记。receiver_hint/callback_reference只是可能关系；动态外部方法无法静态证明。

所有函数、类和嵌套定义的可达/不可达、所在行、调用者、外部引用在functions.tsv；全部边在call_edges.tsv。不把“import模块”当作调用它全部函数，也不把没有日志当作永远不可达。

## 当前不可达定义

| 定义 | 行 | 分类 | 引用 |
|---|---:|---|---|
'''
    groups = [(test_only, '只由测试链可达'), (tools_only, '工具/校准链可达'), (no_ref, '无入口或外部引用链')]
    for rows, category in groups:
        for f in rows:
            refs = sorted({r['file'] for r in f['external_refs']})
            text += f'| `{f["symbol"]}` | {f["line"]}–{f["end"]} | {category} | ' + ('、'.join('`' + r + '`' for r in refs) or '间接调用者：' + ', '.join(f['callers']) or '无') + ' |\n'
    text += '\n完整模块级关系见callgraph.dot；灰色是没有CLI函数调用边的模块，__init__只有被导入的包初始化，不是死模块。\n'
    (OUT / 'callgraph.md').write_text(text)


if __name__ == '__main__':
    main()
