"""Generic face-relative iris band with measured hair/skin palette exclusion."""
import numpy as np
import cv2
from storycg import detect

def sample(image,face,cfg):
 from storycg.detection_review import evidence
 if face is None:return evidence('not_visible',reason='face_not_detected',hue=None,saturation=None,pixel_count=0,sampled_pixels=[])
 image=detect._image(image);x0,y0,x1,y1=detect.expand_box(face['bbox'],image.size,0);w,h=x1-x0,y1-y0
 rgb=np.asarray(image.convert('RGB'));hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV).astype(float);lab=cv2.cvtColor(rgb,cv2.COLOR_RGB2LAB).astype(float)
 def box(v):return [int(x0+w*v[0]),int(y0+h*v[1]),int(x0+w*v[2]),int(y0+h*v[3])]
 def palette(regions):
  values=np.concatenate([lab[b[1]:b[3],b[0]:b[2]].reshape(-1,3) for b in regions],axis=0)
  if not len(values):return []
  bins=(values//cfg['palette_bin']).astype(int);unique,counts=np.unique(bins,axis=0,return_counts=True)
  return [values[(bins==unique[i]).all(axis=1)].mean(axis=0).tolist() for i in np.argsort(counts)[-cfg['palette_clusters']:]]
 hair_regions=[box(b) for b in cfg['hair_regions']];skin_regions=[box(b) for b in cfg['skin_regions']]
 hair=palette(hair_regions);skin=palette(skin_regions);roi=box(cfg['eye_region']);l,t,r,b=roi
 local=hsv[t:b,l:r];hue=local[:,:,0]*2;sat=local[:,:,1]/255;val=local[:,:,2]/255
 selected=(sat>=cfg['min_saturation'])&(val>=cfg['min_value'])&(val<=cfg['max_value'])
 for color in hair+skin:selected &= np.linalg.norm(lab[t:b,l:r]-color,axis=2)>cfg['palette_distance']
 # Skin shading varies in lightness; also exclude its measured hue family.
 for color in skin:
  rgb_color=cv2.cvtColor(np.uint8([[color]]),cv2.COLOR_LAB2RGB)
  skin_hsv=cv2.cvtColor(rgb_color,cv2.COLOR_RGB2HSV)[0,0]
  if skin_hsv[1]/255>=.1:
   distance=np.minimum(abs(hue-float(skin_hsv[0])*2),360-abs(hue-float(skin_hsv[0])*2))
   selected &= distance>cfg['skin_hue_distance']
 # Reject large connected color masses such as bangs; iris candidates are compact.
 n,labels,stats,_=cv2.connectedComponentsWithStats(selected.astype('uint8'),8)
 for i in range(1,n):
  if stats[i,cv2.CC_STAT_WIDTH]>w*cfg['max_component_width'] or stats[i,cv2.CC_STAT_HEIGHT]>h*cfg['max_component_height']:selected[labels==i]=False
 meta={'roi':roi,'hair_regions':hair_regions,'skin_regions':skin_regions,'hair_palette':hair,'skin_palette':skin,'candidate_pixels':int(selected.sum()),'sampled_pixels':[]}
 if not selected.any():return evidence('not_visible',reason='no isolated iris pixels',hue=None,saturation=None,pixel_count=0,**meta)
 candidates=hue[selected];centers=np.arange(0,360,cfg['hue_bin_degrees']);counts=[int((np.minimum(abs(candidates-c),360-abs(candidates-c))<=cfg['cluster_radius']).sum()) for c in centers];center=centers[int(np.argmax(counts))]
 chosen=selected&(np.minimum(abs(hue-center),360-abs(hue-center))<=cfg['cluster_radius']);count=int(chosen.sum())
 if count<cfg['min_pixels'] or count/selected.sum()<cfg['min_dominant_fraction']:return evidence('not_visible',reason='insufficient or ambiguous iris clusters',hue=None,saturation=None,pixel_count=count,**meta)
 yy,xx=np.where(chosen);meta['sampled_pixels']=np.column_stack([xx+l,yy+t]).tolist()
 angles=np.deg2rad(hue[chosen]);mean=float(np.rad2deg(np.arctan2(np.sin(angles).mean(),np.cos(angles).mean()))%360)
 return evidence('sampled',hue=mean,saturation=float(sat[chosen].mean()),pixel_count=count,**meta)
