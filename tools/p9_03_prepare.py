"""Local-only sample packaging. No generation, inference, or reviewer imports."""
import copy, hashlib, json, re, shutil, time
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw, ImageFont
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'calibration/v2';S=OUT/'samples'
def load(p):return json.loads((ROOT/p).read_text())
def sha(p):return hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def source(root,n,a=1):return f'{root}/storycg_{Path(root).name}_shot{n:02d}_a{a:03d}.png'
a='runs/p3_02_a_ipadapter';b='runs/p3_02_b_ipadapter';y='runs/p5_02_b_r1';f='jobs/cg-fc625e111558';t='jobs/cg-76d2eec57dfc'
items=[]
def add(sid,p,root,n,claim,obs,**kw):items.append(dict(id=sid,source_image=p,task_root=root,shot_number=n,claim=claim,executor_observation=obs,**kw))
add('A1',source(y,4,4),y,4,'发髻 = not_visible（待确认）','正面头部轮廓未露出发髻，发带、红瞳和耳饰可见。拒选原定妆图，因为其顶部露出发髻；此图有飘散长发，不能证明脑后实际有髻，须监工确认是否适用。',uncertainties=['单张正面无法证明隐藏发髻存在；可能本身缺髻。'])
add('A2',source(b,3),b,3,'发髻、白发带 = ok（待确认）','背面圆形发髻与白色环带/垂带清楚，适合与 A3 对照；环带是否身份发带而非帽带需监工辨认。',uncertainties=['白色环带可能与帽带混淆。'])
add('A3','calibration/samples/E4b.png',b,3,'发带 = fail','原样复制指定 E4b，发髻外的白环带及垂带不见，帽子和金耳饰保留。',uncertainties=['缺失带状物的身份继承 A2 的待确认问题。'])
add('A4','calibration/samples/E4a.png',a,2,'耳饰 = fail','原样复制指定 E4a，尖耳可见但红色垂坠耳饰缺失。')
add('A5','calibration/samples/C2.png',a,2,'靴子 = not_visible','上半身俯视近景，靴子完全位于画外；未裁切修改来源图。')
add('A6',None,a,1,'瞳色 = not_visible（缺图）','未找到合适的背影或瞳孔真正不可辨的远景。已查候选的眼部颜色仍可见，不将缩小预览造成的不可辨冒充原图不可见。',status='missing_source',uncertainties=['未产出 A6.png；需补合适来源。'])
add('A7','calibration/samples/C3.png',a,3,'耳饰 = ok','近正面上半身，左右红色金属边垂坠耳饰均清楚可见。')
add('B1',source(t,1),t,1,'重复主角 = error','桌上趴卧与右侧站立人物具有相同粉发、眼镜、上衣和裙装；两具实体主角，不是倒影。')
add('B2','calibration/samples/C3.png',a,3,'重复主角 = error（未生成）','拟用单人正面源图添加同角色同服装的第二人；工单禁止在 Windows 做任何事，而既有 Qwen 工作流仅在 Windows ComfyUI 可执行，未上传、未提交。',status='blocked',method='qwen_edit_not_executed')
add('B3',source(y,4,4),y,4,'重复主角 = error（未生成）','拟用单人正面源图添加同角色同服装的第二人；同 B2 冲突，未上传、未提交；源图没有作为编辑结果落盘。',status='blocked',method='qwen_edit_not_executed')
add('B4',source(f,2),f,2,'重复主角 = ok（镜中倒影）','右侧实体与左侧镜中人物、手部形成镜面对应，只有一个实体主角。',reflection=True,reflection_kind='mirror')
add('B5',source(t,3,2),t,3,'重复主角 = ok（玻璃倒影）','窗框右侧实体与左侧玻璃映像相对，分镜文字及 reflection tag 明确声明倒影。',reflection=True,reflection_kind='window_glass')
add('B6',source('runs/p1_02_frieren_20260930',4),'runs/p1_02_frieren_20260930',4,'重复主角 = ok（背景路人）','前景白发主角一人；后方黑发深色服装人物是他人，不应当作第二个主角。',other_people=[{'role':'background_passersby','allowed':True,'basis':'P9 design explicitly allows other people for this sample'}])
add('B7',source('runs/p5_02_a_r1',4),'runs/p5_02_a_r1',4,'重复主角 = ok（画外手另记）','只有一个完整主角；右缘西装袖与陌生人的手接触主角手掌，手的来路需另记，不能当作第二个主角。',other_people=[{'role':'offscreen_person_hand','allowed_for_protagonist_count':True,'ownership_issue':'unestablished'}])
add('B8','calibration/samples/C3.png',a,3,'重复主角 = ok（正面单人）','正面近景只有一名主角，没有背景人或倒影。')
add('B9',source(b,3),b,3,'重复主角 = ok（背影单人）','背面全身一人；无第二个实体、倒影或背景路人。')
add('B10',source(b,1),b,1,'重复主角 = ok（远景单人）','街道远景中心一名主角；两侧街道无可辨第二人。')
for sid,old,root,n,claim,obs in [
 ('C-e1','E3a',a,2,'瞳色 = error','指定绿→蓝编辑图，双眼明显蓝色。'),
 ('C-e2','E3b',b,2,'瞳色 = error','指定红→蓝编辑图，可见眼睛蓝色。'),
 ('C-e3','C1',a,1,'瞳色 = error / not_visible 待确认','指定远景图，眼部黄调；能否可靠辨瞳色由监工确定，不预先剔除。'),
 ('C-o1','C2',a,2,'瞳色 = ok','绿瞳可见；其它道具位置偏差不改变本项声称。'),
 ('C-o2','C3',a,3,'瞳色 = ok','眼睛绿/青绿，双眼描绘略不同，须监工确认。'),
 ('C-o3','Q1',a,2,'瞳色 = ok','空编辑后仍为绿色，眼形比原图圆亮；非全图正确声明。'),
 ('C-o4','C5',b,2,'瞳色 = ok','可见眼睛红色；戴帽动作偏差不改变瞳色声明。'),
 ('C-o5','Q2',b,2,'瞳色 = ok','空编辑后可见眼睛保留红色。'),
 ('C-o6','E2a',a,2,'瞳色 = ok','头发已变粉，眼睛仍绿；只针对瞳色。')]:add(sid,f'calibration/samples/{old}.png',root,n,claim,obs)
add('C-e4',source('runs/p3_02_a_none',3),'runs/p3_02_a_none',3,'瞳色 = error','原样复制指定近景，眼睛为蓝色而非锁定绿瞳。')
# Restore exact design order without changing IDs.
ids=[f'A{i}' for i in range(1,8)]+[f'B{i}' for i in range(1,11)]+[f'C-e{i}' for i in range(1,5)]+[f'C-o{i}' for i in range(1,7)]
items.sort(key=lambda x:ids.index(x['id']))
refs={a:load('calibration/spec.json')['references']['A'],b:load('calibration/spec.json')['references']['B']}
for root in [y,f,t]:refs[root]=root+'/'+load(root+'/portrait.json')['image']['path']
records=[]
for item in items:
 r=copy.deepcopy(item);root=r['task_root'];identity=load(root+'/identity.json');board=load(root+'/storyboard.json');entry=copy.deepcopy(next(x for x in board['shots'] if x['number']==r['shot_number']))
 r.update(schema_version=1,supervisor_confirmed=False,truth_status='executor_claim_pending_supervisor',method=r.get('method','copy'),status=r.get('status','prepared'),source_sha256=sha(r['source_image']) if r['source_image'] else None,source_role='edit_input_only' if r.get('status')=='blocked' else 'existing_sample',original_shot_requirement=entry,identity=identity,props=board.get('props',[]),reference_image=refs.get(root,refs[a]),uncertainties=r.get('uncertainties',[]),edit_effective=None,review_calls=0,vlm_calls=0)
 r['reference_sha256']=sha(r['reference_image'])
 r['identity_checklist']={'appearance':[x['tag'] for x in identity.get('appearance_tags',[])],'default_outfit':[x['tag'] for x in identity.get('default_outfit_tags',[])],'shot_outfit':entry['outfit'].get('tags',[]),'reference_features':['按定妆图核对；不将图像当作原设证据']}
 shot=copy.deepcopy(entry['shot']);shot.update(reflection=item.get('reflection',False),reflection_kind=item.get('reflection_kind'),other_people=item.get('other_people',[]),expected_protagonist_instances=1)
 r['shot_requirement']={**entry,'shot':shot};r['requirement_basis']='原分镜 + P9-calibration-v2-design：reflection/other_people 为本次校准显式要求，不回写原任务'
 r['claimed_truth']={'statement':r['claim'],'confirmed':False,'scope':'only designated feature or protagonist duplication'}
 if r['status']=='prepared':
  dest=S/(r['id']+'.png')
  if dest.exists():raise RuntimeError('Refuse to overwrite existing '+str(dest))
  shutil.copyfile(ROOT/r['source_image'],dest);r['image_path']=str(dest.relative_to(ROOT));r['image_sha256']=sha(r['image_path'])
 else:r['image_path']=None;r['image_sha256']=None
 save(S/(r['id']+'.json'),r);records.append(r)
save(OUT/'manifest.json',{'order':'P9-03','sample_ids':ids,'samples':records,'overview':'review/P9-03-samples.jpg','generated_images':0,'review_calls':0,'vlm_calls':0,'supervisor_confirmation_required':True,'missing_images':[r['id'] for r in records if not r['image_path']]})
fontpath='/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc';font=ImageFont.truetype(fontpath,22);small=ImageFont.truetype(fontpath,18);big=ImageFont.truetype(fontpath,30)
w,h=430,540;sheet=Image.new('RGB',(w*5,120+540+70+2*h+70+2*h+70+2*h),'#f5f4f0');d=ImageDraw.Draw(sheet)
d.text((20,15),'P9-03 第二轮校准候选 | 声称真值，全部待监工确认',font=big,fill='#172c3d');d.text((20, sixty:=65),'未调用审核器/视觉模型；B2/B3 未生成，A6 缺图；总览缩放不作为可见性判断依据。',font=font,fill='#943020')
for i,(root,p) in enumerate(refs.items()):
 x=i*w;im=ImageOps.contain(Image.open(ROOT/p).convert('RGB'),(w-20,475));sheet.paste(im,(x+(w-im.width)//2,155));d.text((x+10,120),load(root+'/identity.json')['input_name']+' 定妆参考',font=font,fill='black')
ypos=660
for group,title in [('A','A 看不见与不存在'),('B','B 单主角计数'),('C','C 瞳色（仅瞳色真值）')]:
 d.rectangle((0,ypos,w*5,ypos+60),fill='#203747');d.text((15,ypos+12),title,font=big,fill='white');ypos+=70
 rows=[r for r in records if r['id'].startswith(group)]
 for i,r in enumerate(rows):
  x=i%5*w;y0=ypos+i//5*h;d.text((x+12,y0+8),r['id'],font=big,fill='#172c3d')
  claim=r['claim'];chunks=[];part=''
  for ch in claim:
   if d.textlength(part+ch,font=small)>w-24:chunks.append(part);part=ch
   else:part+=ch
  chunks.append(part);d.multiline_text((x+12,y0+48),'\n'.join(chunks),font=small,fill='#943020',spacing=3)
  if r['image_path']:
   im=ImageOps.contain(Image.open(ROOT/r['image_path']).convert('RGB'),(w-18,h-110));sheet.paste(im,(x+(w-im.width)//2,y0+100))
  else:d.text((x+20,y0+240),'缺图：'+('远端执行冲突' if r['status']=='blocked' else '未找到合适来源'),font=font,fill='#943020')
 ypos+=2*h
sheet.save(ROOT/'review/P9-03-samples.jpg',quality=95,subsampling=0)
print(json.dumps({'json':len(records),'png':sum(bool(x['image_path']) for x in records),'missing':[x['id'] for x in records if not x['image_path']]},ensure_ascii=False))
