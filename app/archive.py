#!/usr/bin/env python3
"""Safe archive, regular/adaptive time-lapse and conservative purge tools.

Original segments are never re-encoded in place. Daily archive parts are made
from temporary clips. Protected ranges remain represented by untouched source
segments; the corresponding compressed archive is split before/after them.
"""
from __future__ import annotations
import argparse, ast, hashlib, json, math, os, re, subprocess, sys
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
from naming import filename as recording_filename, parse_path as parse_recording_path
from runtime_paths import resolve_media_tools

ROOT=Path(os.environ.get('CCTV_PROJECT_ROOT', '/project' if Path('/project').exists() else Path(__file__).resolve().parents[1]))
DATA=ROOT/'data'; ORIGINAL=DATA/'recordings'/'original'; ARCHIVES=DATA/'recordings'/'archives'; TIMELAPSE=DATA/'recordings'/'timelapse'
CONTROL=ROOT/'USER_CONTROL.py'; COMMANDS=ROOT/'control'/'COMMANDS.json'; BOOKMARKS=ROOT/'config'/'bookmarks.json'; CAMERAS=ROOT/'config'/'cameras.json'


def settings():
    tree=ast.parse(CONTROL.read_text(encoding='utf-8')); out={}
    for n in tree.body:
        if isinstance(n,(ast.Assign,ast.AnnAssign)):
            try:v=ast.literal_eval(n.value)
            except Exception:continue
            for t in (n.targets if isinstance(n,ast.Assign) else [n.target]):
                if isinstance(t,ast.Name):out[t.id]=v
    return resolve_media_tools(out)

def load_json(p,default):
    try:return json.loads(p.read_text(encoding='utf-8'))
    except FileNotFoundError:return default

def save_json(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(obj,indent=2)+'\n');os.replace(tmp,p)

def log_command(args):
    import shlex
    print('EXEC '+shlex.join(str(x) for x in args),flush=True)

def sha256_file(path:Path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()

def zone_for(cfg):
    name=cfg.get('ARCHIVE_TIMEZONE','local')
    if name!='local':return ZoneInfo(name)
    try:return ZoneInfo(os.environ.get('TZ','UTC'))
    except Exception:return datetime.now().astimezone().tzinfo

def archive_window(day:str,start_text:str,timezone):
    d=datetime.strptime(day,'%Y-%m-%d').date();h,m=map(int,start_text.split(':'))
    start=datetime.combine(d,dtime(h,m),tzinfo=timezone);end=datetime.combine(d+timedelta(days=1),dtime(h,m),tzinfo=timezone)
    return start,end

def media_start(p:Path,timezone=None):
    try:
        parsed=parse_recording_path(p)
        if parsed: dt=parsed['started']
        else: dt=datetime.strptime(p.stem,'%Y-%m-%d_%H-%M-%S-%f_%z')
    except Exception:
        try: dt=datetime.strptime(p.stem,'%Y-%m-%d_%H-%M-%S-%f').astimezone()
        except Exception: dt=datetime.fromtimestamp(p.stat().st_mtime).astimezone()
    return dt.astimezone(timezone) if timezone else dt.astimezone()


def camera_code(camera:str)->str:
    m=re.fullmatch(r'cam(\d+)',str(camera).lower())
    return f'C{m.group(1)}' if m else 'C'+re.sub(r'[^A-Z0-9]','',str(camera).upper())[:12]

def ffconcat_quote(p:Path):return "file '"+str(p.resolve()).replace("'", "'\\''")+"'"
def make_concat(paths:list[Path],target:Path):
    target.parent.mkdir(parents=True,exist_ok=True);target.write_text('\n'.join(ffconcat_quote(p) for p in paths)+'\n',encoding='utf-8')

def ffprobe_duration(p:Path):
    cmd=build_args(load_json(COMMANDS,{}).get('archive_probe_duration'),{'ffprobe':str(settings().get('FFPROBE_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffprobe'))),'input':str(p)})
    log_command(cmd);r=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    if r.returncode:raise RuntimeError(f'ffprobe failed for {p.name}: {(r.stderr or r.stdout)[-600:]}')
    return float(r.stdout.strip())

def protected_ranges(start,end,camera,timezone):
    doc=load_json(BOOKMARKS,{'bookmarks':[]});spans=[]
    for b in doc.get('bookmarks',[]):
        if camera not in b.get('camera_ids', [b.get('camera_id')]) or not b.get('protected'):continue
        try:bs=datetime.fromisoformat(b['start']).astimezone(timezone);be=datetime.fromisoformat(b['end']).astimezone(timezone)
        except Exception:continue
        if bs<end and be>start:spans.append((max(start,bs),min(end,be),b))
    spans.sort(key=lambda x:x[0]);merged=[]
    for a,b,obj in spans:
        if merged and a<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(b,merged[-1][1]),merged[-1][2]+[obj])
        else:merged.append((a,b,[obj]))
    return merged

def subtract_ranges(start,end,protected):
    out=[];cursor=start
    for a,b,_ in protected:
        if a>cursor:out.append((cursor,a))
        cursor=max(cursor,b)
    if cursor<end:out.append((cursor,end))
    return out

def build_args(template,values):
    args=[]
    for item in template:
        try:args.append(item.format(**values))
        except KeyError as e:raise RuntimeError(f'Unknown command placeholder {e} in control/COMMANDS.json')
    return args

def run_media_command(args,timeout=3600):
    log_command(args);r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if r.returncode:raise RuntimeError('Media command failed:\n'+(r.stderr or r.stdout)[-5000:])
    return r.stderr or r.stdout

def verify(p:Path,expected_duration=None):
    cmd=build_args(load_json(COMMANDS,{}).get('archive_verify_probe'),{'ffprobe':str(settings().get('FFPROBE_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffprobe'))),'input':str(p)})
    log_command(cmd)
    try:r=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    except Exception as e:return False,str(e)
    if r.returncode:return False,(r.stderr or r.stdout)[-1000:]
    try:meta=json.loads(r.stdout)['format'];duration=float(meta['duration']);size=int(meta['size'])
    except Exception:return False,'ffprobe returned incomplete media metadata.'
    if size<=0 or duration<=0:return False,'Output file has zero size or duration.'
    if expected_duration and duration<max(1,expected_duration*.5):return False,'Output duration is unexpectedly short.'
    # Decode a small sample to catch a file that probes but does not decode.
    test=build_args(load_json(COMMANDS,{}).get('archive_verify_decode'),{'ffmpeg':str(settings().get('FFMPEG_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffmpeg'))),'input':str(p),'frames':str(max(1,int(settings().get('ARCHIVE_VERIFY_DECODE_FRAMES',10))))})
    log_command(test)
    dec=subprocess.run(test,capture_output=True,text=True,timeout=60)
    if dec.returncode:return False,'Decode validation failed: '+(dec.stderr or dec.stdout)[-1000:]
    return True,{'duration_seconds':round(duration,3),'size_bytes':size,'decoded_sample_frames':max(1,int(settings().get('ARCHIVE_VERIFY_DECODE_FRAMES',10)))}

def clip_template_key(audio):
    if str(audio).strip().lower() == 'none':
        raise ValueError('Video-only archive clips are disabled; choose audio passthrough or an audio encoder.')
    return 'archive_clip'

def build(camera:str,day:str):
    cfg=settings();tz=zone_for(cfg);start,end=archive_window(day,str(cfg.get('ARCHIVE_DAY_START','05:00')),tz)
    if cfg.get('ARCHIVE_WAIT_UNTIL_DAY_CLOSED',True) and end>datetime.now(tz):raise SystemExit(f'Archive day is not closed yet. Selected window: {start.isoformat()} to {end.isoformat()}.')
    source_dir=ORIGINAL/camera
    if not source_dir.is_dir():raise SystemExit(f'No original recording directory for {camera}: {source_dir}')
    paths=sorted([p for p in source_dir.rglob('*') if p.is_file() and p.suffix.lower() in ('.ts','.mp4','.mkv')],key=lambda p:media_start(p,tz))
    segments=[]
    for p in paths:
        try:
            st=media_start(p,tz);dur=ffprobe_duration(p);en=st+timedelta(seconds=dur)
        except Exception as e:print(f'SKIP {p.name}: {e}');continue
        if st<end and en>start:segments.append({'path':p,'start':max(st,start),'end':min(en,end),'file_start':st,'duration':dur})
    if not segments:raise SystemExit(f'No original segments found for {camera} in {day}.')
    protections=protected_ranges(start,end,camera,tz)
    allowed=subtract_ranges(start,end,protections)
    cmdfile=load_json(COMMANDS,{})
    work=ROOT/'cache'/'archive-work'/camera/day;work.mkdir(parents=True,exist_ok=True)
    outdir=ARCHIVES/camera/day;outdir.mkdir(parents=True,exist_ok=True)
    manifest_path=outdir/f'{camera}_{day}.manifest.json'
    previous_archive_parts=[]
    overwrite=bool(cfg.get('ARCHIVE_OVERWRITE_EXISTING',False))
    if any(outdir.iterdir()) and not overwrite:
        raise SystemExit(f'Archive output directory already contains files: {outdir}. Enable overwrite only after reviewing existing outputs.')
    if overwrite and manifest_path.is_file():
        try:
            previous=json.loads(manifest_path.read_text(encoding='utf-8'))
            previous_archive_parts=[str(x.get('file','')) for x in previous.get('archive_parts',[]) if x.get('file')]
        except Exception as exc:
            raise SystemExit(f'Existing archive manifest cannot be read; overwrite refused: {exc}')
    # Rebuilds receive a unique generation suffix. The previous verified outputs
    # stay intact until the new manifest has been written successfully.
    generation=datetime.now(tz).strftime('%H%M%S%f') if overwrite else ''
    audio=str(cfg.get('ARCHIVE_AUDIO_CODEC','copy'));clip_template=cmdfile.get(clip_template_key(audio))
    join_template=cmdfile.get('archive_join')
    if not isinstance(clip_template,list) or not isinstance(join_template,list):raise SystemExit('Missing archive_clip/archive_join templates in control/COMMANDS.json.')
    values_base={'ffmpeg':cfg.get('FFMPEG_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffmpeg')),'loglevel':cfg.get('FFMPEG_LOGLEVEL','warning'),
      'video_codec':cfg.get('ARCHIVE_VIDEO_CODEC','libx264'),'crf':str(cfg.get('ARCHIVE_VIDEO_CRF',23)),
      'preset':cfg.get('ARCHIVE_VIDEO_PRESET','medium'),'pixel_format':cfg.get('ARCHIVE_PIXEL_FORMAT','yuv420p'),
      'audio_codec':audio,'audio_bitrate':cfg.get('ARCHIVE_AUDIO_BITRATE','96k')}
    part_outputs=[];all_fragments=[];part_no=0;partial_outputs=[]
    try:
        for range_start,range_end in allowed:
            fragments=[];part_time_map=[];part_cursor=0.0
            for seg in segments:
                a=max(range_start,seg['start']);b=min(range_end,seg['end'])
                if b<=a:continue
                seek=max(0,(a-seg['file_start']).total_seconds());duration=(b-a).total_seconds()
                if duration<0.05:continue
                frag=work/f'part{len(part_outputs)+1:02d}_{len(fragments)+1:04d}.mp4'
                vals={**values_base,'input':str(seg['path']),'seek':f'{seek:.3f}','duration':f'{duration:.3f}','output':str(frag)}
                args=build_args(clip_template,vals)
                if cfg.get('ARCHIVE_EXTRA_ARGS'):args.extend(cfg['ARCHIVE_EXTRA_ARGS'])
                if cfg.get('FFMPEG_ARCHIVE_EXTRA_ARGS'):args.extend(cfg['FFMPEG_ARCHIVE_EXTRA_ARGS'])
                run_media_command(args,timeout=3600)
                ok,info=verify(frag,expected_duration=duration)
                if not ok:raise RuntimeError(f'Archive clip verification failed ({frag.name}): {info}')
                fragments.append(frag);all_fragments.append(frag)
                part_time_map.append({'output_start_seconds':round(part_cursor,4),'output_end_seconds':round(part_cursor+duration,4),
                  'source_file':str(seg['path'].relative_to(ROOT)),'source_offset_start_seconds':round(seek,4),
                  'source_offset_end_seconds':round(seek+duration,4),'source_start':a.isoformat(),'source_end':b.isoformat(),'speed':1.0})
                part_cursor+=duration
            if not fragments:continue
            part_no+=1
            concat=work/f'part{part_no:02d}.ffconcat';make_concat(fragments,concat)
            end_prefix='D+1_' if range_end.astimezone(tz).date()>range_start.astimezone(tz).date() else ''
            label=f'{range_start.astimezone(tz).strftime("%H%M")}-{end_prefix}{range_end.astimezone(tz).strftime("%H%M")}'
            generation_suffix = generation if generation else ''
            output_name=recording_filename(camera_code(camera), range_start.astimezone(tz), 'MER', 'mp4', part=part_no, generation=generation_suffix)
            filename=output_name;output=outdir/filename;partial=output.with_name(output.stem+'.partial.mp4');partial_outputs.append(partial)
            vals={'ffmpeg':cfg.get('FFMPEG_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffmpeg')),'loglevel':cfg.get('FFMPEG_LOGLEVEL','warning'),
                  'concat_list':str(concat),'output':str(partial)}
            run_media_command(build_args(join_template,vals),timeout=1800)
            ok,info=verify(partial)
            if not ok:raise RuntimeError(f'Daily archive part verification failed: {info}')
            os.replace(partial,output);part_outputs.append({'file':str(output.relative_to(ROOT)),'start':part_time_map[0]['source_start'] if part_time_map else range_start.isoformat(),'end':part_time_map[-1]['source_end'] if part_time_map else range_end.isoformat(),'duration_seconds':info['duration_seconds'],'time_map':part_time_map,'size_bytes':info['size_bytes'],'sha256':sha256_file(output)})
        if not part_outputs:raise RuntimeError('No unprotected archive output could be created.')
    except Exception:
        for p in all_fragments + partial_outputs:p.unlink(missing_ok=True)
        for item in part_outputs:(ROOT/item['file']).unlink(missing_ok=True)
        raise
    source_records=[]
    for seg in segments:
        full_start=seg['file_start'];full_end=full_start+timedelta(seconds=seg['duration'])
        covered=0.0
        for ra,rb in allowed:
            covered+=max(0.0,(min(full_end,rb)-max(full_start,ra)).total_seconds())
        protected_hit=any(a<full_end and b>full_start for a,b,_ in protections)
        complete=(not protected_hit and full_start>=start and full_end<=end and covered>=seg['duration']-1.0)
        source_records.append({'file':str(seg['path'].relative_to(ROOT)),'start':full_start.isoformat(),'end':full_end.isoformat(),
          'size_bytes':seg['path'].stat().st_size,'sha256':sha256_file(seg['path']),'complete_coverage':complete,'protected_overlap':protected_hit})
    manifest={'verified':True,'camera_id':camera,'archive_day':day,'archive_day_start':start.isoformat(),'archive_day_end':end.isoformat(),
      'source_files':[x['file'] for x in source_records],'source_segment_records':source_records,
      'protected_intervals':[{'start':a.isoformat(),'end':b.isoformat(),'labels':[z.get('label','Protected') for z in objs]} for a,b,objs in protections],
      'protected_source_files':[str(x['path'].relative_to(ROOT)) for x in segments if any(a<x['end'] and b>x['start'] for a,b,_ in protections)],
      'archive_parts':part_outputs,'created':datetime.now(tz).isoformat(timespec='seconds'),
      'compression':{'codec':cfg.get('ARCHIVE_VIDEO_CODEC'),'crf':cfg.get('ARCHIVE_VIDEO_CRF'),'preset':cfg.get('ARCHIVE_VIDEO_PRESET'),'audio':audio},
      'source_originals_modified':False,'source_originals_deleted':False,'verification':'ffprobe + 10-frame decode sample'}
    try:
        save_json(manifest_path,manifest)
    except Exception:
        for item in part_outputs:
            (ROOT/item['file']).unlink(missing_ok=True)
        for p in all_fragments:p.unlink(missing_ok=True)
        for p in partial_outputs:p.unlink(missing_ok=True)
        raise
    # Only after the new manifest and every new archive part are committed do we
    # remove the old parts referenced by the previous manifest.
    for old_rel in previous_archive_parts:
        try:
            old_path=(ROOT/old_rel).resolve()
            if old_path.is_relative_to(outdir.resolve()) and old_path.suffix.lower()=='.mp4' and old_path.is_file():
                if old_rel not in {item['file'] for item in part_outputs}:
                    old_path.unlink()
        except OSError as exc:
            print(f'WARNING: previous archive part could not be removed after successful replacement: {old_rel}: {exc}',flush=True)
    for p in all_fragments:p.unlink(missing_ok=True)
    total=sum(x['size_bytes'] for x in part_outputs)
    print(json.dumps({'ok':True,'manifest':str(manifest_path.relative_to(ROOT)),'parts':part_outputs,'protected_intervals':manifest['protected_intervals'],'source_segments':len(segments),'total_output_bytes':total},indent=2))
    return manifest



def estimate(camera:str,day:str):
    cfg=settings();tz=zone_for(cfg);start,end=archive_window(day,str(cfg.get('ARCHIVE_DAY_START','05:00')),tz)
    srcdir=ORIGINAL/camera;paths=sorted([p for p in srcdir.rglob('*') if p.is_file() and p.suffix.lower() in ('.ts','.mp4','.mkv')],key=lambda p:media_start(p,tz))
    selected=[];total_bytes=0;protected_bytes=0
    spans=protected_ranges(start,end,camera,tz)
    for p in paths:
        try:st=media_start(p,tz);dur=ffprobe_duration(p);en=st+timedelta(seconds=dur)
        except Exception:continue
        if st<end and en>start:
            selected.append((p,st,en,dur));total_bytes+=p.stat().st_size
            if any(a<en and b>st for a,b,_ in spans):protected_bytes+=p.stat().st_size
    if not selected:raise SystemExit('No source segments found for the selected archive day.')
    sample_path,st,en,dur=selected[0];sample_seconds=min(max(float(cfg.get('ARCHIVE_ESTIMATE_MIN_SAMPLE_SECONDS',60)),float(cfg.get('ARCHIVE_ESTIMATE_SAMPLE_SECONDS',300))),float(cfg.get('ARCHIVE_ESTIMATE_MAX_SAMPLE_SECONDS',900)),dur)
    work=ROOT/'cache'/'estimate';work.mkdir(parents=True,exist_ok=True);sample_out=work/f'{camera}_{day}_sample.mp4';sample_out.unlink(missing_ok=True)
    audio=str(cfg.get('ARCHIVE_AUDIO_CODEC','copy'));template=load_json(COMMANDS,{}).get(clip_template_key(audio))
    vals={'ffmpeg':cfg.get('FFMPEG_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffmpeg')),'loglevel':'error','input':str(sample_path),'seek':'0','duration':f'{sample_seconds:.3f}',
      'video_codec':cfg.get('ARCHIVE_VIDEO_CODEC','libx264'),'crf':str(cfg.get('ARCHIVE_VIDEO_CRF',23)),'preset':cfg.get('ARCHIVE_VIDEO_PRESET','medium'),
      'pixel_format':cfg.get('ARCHIVE_PIXEL_FORMAT','yuv420p'),'audio_codec':audio,'audio_bitrate':cfg.get('ARCHIVE_AUDIO_BITRATE','96k'),'output':str(sample_out)}
    args=build_args(template,vals);run_media_command(args,timeout=1800)
    if not sample_out.is_file():raise SystemExit('Compression sample failed; no size estimate available.')
    sample_input_est=sample_path.stat().st_size*min(1,sample_seconds/max(.001,dur));ratio=sample_out.stat().st_size/max(1,sample_input_est)
    unprotected=max(0,total_bytes-protected_bytes);estimate_bytes=round(protected_bytes+unprotected*ratio)
    report={'camera':camera,'archive_day':day,'source_bytes':total_bytes,'sample_seconds':round(sample_seconds,1),
      'sample_ratio':round(ratio,4),'protected_source_bytes_preserved':protected_bytes,'estimated_compressed_archive_bytes':estimate_bytes,
      'estimated_total_with_protected_originals_bytes':estimate_bytes,'estimated_saving_bytes':max(0,total_bytes-estimate_bytes),
      'note':'Sample-based estimate, not a guarantee. Scene complexity, GOP boundaries and protected source segments affect the result.'}
    sample_out.unlink(missing_ok=True);print(json.dumps(report,indent=2));return report

def point_inside(x,y,poly):
    inside=False;j=len(poly)-1
    for i in range(len(poly)):
        xi,yi=poly[i];xj,yj=poly[j]
        if ((yi>y)!=(yj>y)) and x<(xj-xi)*(y-yi)/(yj-yi+1e-12)+xi:inside=not inside
        j=i
    return inside

def detection_mask(camera,width,height,cfg):
    if not cfg.get('ENABLE_DETECTION_EXCLUSION_ZONES',True):return bytes([1])*(width*height)
    cams=load_json(CAMERAS,{'cameras':[]}).get('cameras',[]);cam=next((c for c in cams if c.get('id')==camera),{})
    polys=cam.get('exclude_zones',[]);mask=bytearray([1])*(width*height)
    if not polys:return bytes(mask)
    for y in range(height):
        yn=y/max(1,height-1)
        for x in range(width):
            xn=x/max(1,width-1)
            if any(point_inside(xn,yn,p) for p in polys):mask[y*width+x]=0
    return bytes(mask)

def motion_profile(paths,camera,cfg,work):
    fps=float(cfg.get('TIMELAPSE_MOTION_SAMPLE_FPS',1));w=int(cfg.get('TIMELAPSE_MOTION_SAMPLE_WIDTH',160));h=int(cfg.get('TIMELAPSE_MOTION_SAMPLE_HEIGHT',90));frame_size=w*h
    concat=work/'motion.ffconcat';make_concat(paths,concat)
    if not cfg.get('ENABLE_MOTION_DETECTION',True):
        quiet=int(cfg.get('TIMELAPSE_INTELLIGENT_QUIET_SPEED',120));duration=sum(ffprobe_duration(p) for p in paths)
        return {'camera_id':camera,'sample_fps':0,'sample_size':[w,h],'sample_count':0,'zones_applied':False,
                'ranges':[{'start':0,'end':duration,'speed':quiet,'max_motion_percent':0}],
                'created':datetime.now().astimezone().isoformat(timespec='seconds'),'note':'Motion detection disabled; intelligent time-lapse uses quiet-period speed throughout.'}
    cmd=build_args(load_json(COMMANDS,{}).get('timelapse_motion_scan'),{'ffmpeg':str(cfg.get('FFMPEG_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffmpeg'))),'loglevel':'error','concat_list':str(concat),'fps':str(fps),'width':str(w),'height':str(h)})
    log_command(cmd);proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    mask=detection_mask(camera,w,h,cfg);prev=None;bins=[];index=0
    threshold=int(cfg.get('TIMELAPSE_MOTION_PIXEL_DIFFERENCE',22));changed_min=float(cfg.get('TIMELAPSE_MOTION_CHANGED_PIXEL_PERCENT',1.5))
    try:
        while True:
            frame=proc.stdout.read(frame_size)
            if len(frame)<frame_size:break
            if prev is not None:
                changed=sum(1 for i,(a,b) in enumerate(zip(frame,prev)) if mask[i] and abs(a-b)>=threshold)
                pct=100.0*changed/max(1,sum(mask));bins.append({'start':(index-1)/fps,'end':index/fps,'motion_percent':round(pct,3)})
            prev=frame;index+=1
    finally:
        if proc.stdout:proc.stdout.close()
        try:err=proc.stderr.read().decode('utf-8','replace')
        except Exception:err=''
        rc=proc.wait()
    if rc:raise RuntimeError('Motion scan failed: '+err[-1500:])
    total_duration=sum(ffprobe_duration(p) for p in paths)
    if bins:
        # Each sampled frame describes the interval immediately before it.
        # Carry the final measured state to the actual source duration so the
        # time map never silently drops the first/last sampled intervals.
        if bins[0]['start']>0: bins.insert(0,{'start':0.0,'end':bins[0]['start'],'motion_percent':bins[0]['motion_percent']})
        if bins[-1]['end']<total_duration: bins[-1]['end']=total_duration
    elif total_duration>0:
        bins=[{'start':0.0,'end':total_duration,'motion_percent':0.0}]
    quiet=int(cfg.get('TIMELAPSE_INTELLIGENT_QUIET_SPEED',120));low=int(cfg.get('TIMELAPSE_INTELLIGENT_LOW_MOTION_SPEED',30));act=int(cfg.get('TIMELAPSE_INTELLIGENT_ACTIVITY_SPEED',5));high=int(cfg.get('TIMELAPSE_INTELLIGENT_HIGH_ACTIVITY_SPEED',2))
    hi=float(cfg.get('TIMELAPSE_MOTION_HIGH_PERCENT',12.0));lo=changed_min
    classified=[]
    for b in bins:
        pct=b['motion_percent'];speed=quiet if pct<lo else (low if pct<hi*.5 else (act if pct<hi else high))
        if classified and classified[-1]['speed']==speed and b['start']-classified[-1]['end']<1e-3:classified[-1]['end']=b['end'];classified[-1]['max_motion_percent']=max(classified[-1]['max_motion_percent'],pct)
        else:classified.append({'start':b['start'],'end':b['end'],'speed':speed,'max_motion_percent':pct})
    profile={'camera_id':camera,'sample_fps':fps,'sample_size':[w,h],'sample_count':index,'zones_applied':bool(cfg.get('ENABLE_DETECTION_EXCLUSION_ZONES',True)),
             'ranges':classified,'created':datetime.now().astimezone().isoformat(timespec='seconds')}
    return profile

def intelligent_filter(profile,input_fps,output_fps):
    parts=[]
    for r in profile['ranges']:
        step=max(1,round(input_fps*int(r['speed'])/output_fps));start=float(r['start']);end=float(r['end'])
        parts.append(f"if(between(t,{start:.3f},{end:.3f}),not(mod(n-floor({start:.3f}*{input_fps:.6f}),{step})),")
    expression=''.join(parts) + '0' + ')' * len(parts)
    # FFmpeg filtergraph commas inside select expressions must be escaped;
    # otherwise they are parsed as filter separators (e.g. a fake filter named '1.0').
    expression=expression.replace(',', r'\,')
    return 'select=' + expression + f',setpts=N/({output_fps}*TB)'

def build_timelapse_time_map(timeline_map, speed_ranges):
    """Map each output-time interval back to one original source segment/time."""
    result=[];output_cursor=0.0
    for rate in sorted(speed_ranges,key=lambda x:float(x.get('start',0))):
        rate_start=float(rate.get('start',0));rate_end=float(rate.get('end',0));speed=max(1.0,float(rate.get('speed',1)))
        if rate_end<=rate_start:continue
        for segment in timeline_map:
            a=max(rate_start,float(segment['concat_start_seconds']))
            b=min(rate_end,float(segment['concat_end_seconds']))
            if b<=a:continue
            seg_offset=a-float(segment['concat_start_seconds'])
            duration=b-a;out_duration=duration/speed
            source_start=datetime.fromisoformat(segment['source_start'])+timedelta(seconds=seg_offset)
            source_end=source_start+timedelta(seconds=duration)
            result.append({
                'output_start_seconds':round(output_cursor,4),
                'output_end_seconds':round(output_cursor+out_duration,4),
                'source_file':segment['source_file'],
                'source_offset_start_seconds':round(seg_offset,4),
                'source_offset_end_seconds':round(seg_offset+duration,4),
                'source_start':source_start.isoformat(),
                'source_end':source_end.isoformat(),
                'speed':speed,
            })
            output_cursor+=out_duration
    return result

def atempo_filter(speed: float) -> str:
    remaining=max(0.5,float(speed));parts=[]
    while remaining>2.0:
        parts.append('atempo=2.0');remaining/=2.0
    if remaining<0.5:
        while remaining<0.5:
            parts.append('atempo=0.5');remaining/=0.5
    parts.append(f'atempo={remaining:.6f}')
    return ','.join(parts)

def generate_timelapse(camera,day,mode):
    cfg=settings()
    if mode=='regular' and not cfg.get('ENABLE_REGULAR_TIMELAPSE',False):raise SystemExit('Regular time-lapse is disabled in USER_CONTROL.py.')
    if mode=='intelligent' and not cfg.get('ENABLE_INTELLIGENT_TIMELAPSE',False):raise SystemExit('Intelligent time-lapse is disabled in USER_CONTROL.py.')
    tz=zone_for(cfg);start,end=archive_window(day,str(cfg.get('ARCHIVE_DAY_START','05:00')),tz)
    if cfg.get('ARCHIVE_WAIT_UNTIL_DAY_CLOSED',True) and end>datetime.now(tz):raise SystemExit('Archive day is not closed yet; time-lapse generation is postponed.')
    srcdir=ORIGINAL/camera
    files=sorted([p for p in srcdir.rglob('*') if p.is_file() and p.suffix.lower() in ('.ts','.mp4','.mkv')],key=lambda p:media_start(p,tz))
    files=[p for p in files if start<=media_start(p,tz)<end]
    if not files:raise SystemExit('No original segments found for this archive day.')
    timeline_map=[];cursor=0.0
    for src in files:
        src_start=media_start(src,tz);src_duration=ffprobe_duration(src)
        timeline_map.append({'concat_start_seconds':round(cursor,3),'concat_end_seconds':round(cursor+src_duration,3),
          'source_start':src_start.isoformat(),'source_end':(src_start+timedelta(seconds=src_duration)).isoformat(),
          'source_file':str(src.relative_to(ROOT))})
        cursor+=src_duration
    work=ROOT/'cache'/'timelapse-work'/camera/day;work.mkdir(parents=True,exist_ok=True);concat=work/'input.ffconcat';make_concat(files,concat)
    outdir=TIMELAPSE/camera;outdir.mkdir(parents=True,exist_ok=True);kind='TLR' if mode=='regular' else 'TLI';output=outdir/recording_filename(camera_code(camera), start.astimezone(tz), kind, 'mp4');partial=output.with_suffix('.partial.mp4')
    if output.exists() and not cfg.get('ARCHIVE_OVERWRITE_EXISTING',False):raise SystemExit(f'Time-lapse already exists: {output}')
    # Estimate input frame rate from first segment. Motion scanning samples exclude polygons.
    probe=build_args(load_json(COMMANDS,{}).get('timelapse_source_probe'),{'ffprobe':str(cfg.get('FFPROBE_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffprobe'))),'input':str(files[0])})
    log_command(probe);pr=subprocess.run(probe,capture_output=True,text=True,timeout=20);fps=25.0
    try:
        num,den=pr.stdout.strip().split('/');fps=float(num)/float(den)
    except Exception:pass
    outfps=float(cfg.get('TIMELAPSE_OUTPUT_FPS',25));commands=load_json(COMMANDS,{})
    values={'ffmpeg':str(cfg.get('FFMPEG_BINARY',str(ROOT/'cam3'/'CAM3_LIVE_STANDALONE'/'bin'/'ffmpeg'))),'loglevel':cfg.get('FFMPEG_LOGLEVEL','warning'),'concat_list':str(concat),
       'video_codec':cfg.get('TIMELAPSE_VIDEO_CODEC','libx264'),'crf':str(cfg.get('TIMELAPSE_CRF',24)),
       'preset':cfg.get('TIMELAPSE_PRESET','fast'),'output_fps':str(outfps),'output':str(partial),
       'sample_seconds':f"{float(cfg.get('TIMELAPSE_REGULAR_SPEED',120))/fps:.6f}",'speed':str(cfg.get('TIMELAPSE_REGULAR_SPEED',120)),
       'audio_filter':atempo_filter(float(cfg.get('TIMELAPSE_REGULAR_SPEED',120))),
       'audio_bitrate':str(cfg.get('TIMELAPSE_AUDIO_BITRATE','96k')),'audio_codec':str(cfg.get('TIMELAPSE_AUDIO_CODEC','aac'))}
    if mode=='regular':
        template=commands.get('timelapse_regular');args=build_args(template,values)
        speed_ranges=[{'start':0.0,'end':cursor,'speed':float(cfg.get('TIMELAPSE_REGULAR_SPEED',120))}]
    else:
        profile=motion_profile(files,camera,cfg,work);save_json(work/'motion-profile.json',profile)
        speed_ranges=profile.get('ranges',[])
        filt=intelligent_filter(profile,fps,outfps)
        source_duration=sum(float(x.get('end',0))-float(x.get('start',0)) for x in speed_ranges)
        output_duration=sum((float(x.get('end',0))-float(x.get('start',0)))/max(1.0,float(x.get('speed',1))) for x in speed_ranges)
        effective_speed=source_duration/max(0.001,output_duration)
        values['audio_filter']=atempo_filter(effective_speed)
        values['audio_bitrate']=str(cfg.get('TIMELAPSE_AUDIO_BITRATE','96k'))
        values['audio_codec']=str(cfg.get('TIMELAPSE_AUDIO_CODEC','aac'))
        template=commands.get('timelapse_intelligent');values['select_filter']=filt;args=build_args(template,values)
        profile_path=outdir/recording_filename(camera_code(camera), start.astimezone(tz), 'TLI', 'json').replace('_TLI_', '_TLI_motion_map_');save_json(profile_path,profile)
    args.extend(cfg.get('TIMELAPSE_EXTRA_ARGS',[]));args.extend(cfg.get('FFMPEG_TIMELAPSE_EXTRA_ARGS',[]));run_media_command(args,timeout=24*3600)
    ok,info=verify(partial)
    if not ok:partial.unlink(missing_ok=True);raise SystemExit('Time-lapse verification failed: '+str(info))
    os.replace(partial,output)
    time_map=build_timelapse_time_map(timeline_map,speed_ranges) if cfg.get('TIMELAPSE_MAP_ORIGINAL_TIMESTAMPS',True) else []
    expected_map_duration=float(time_map[-1]['output_end_seconds']) if time_map else 0.0
    if expected_map_duration>0 and info['duration_seconds']>0:
        mapping_scale=float(info['duration_seconds'])/expected_map_duration
        for entry in time_map:
            entry['output_start_seconds']=round(float(entry['output_start_seconds'])*mapping_scale,4)
            entry['output_end_seconds']=round(float(entry['output_end_seconds'])*mapping_scale,4)
        map_note={'time_map_duration_scale':round(mapping_scale,6)}
    else:map_note={'time_map_duration_scale':1.0}
    meta={'verified':True,'camera_id':camera,'archive_day':day,'mode':mode,'output':str(output.relative_to(ROOT)),
          'size_bytes':info['size_bytes'],'duration_seconds':info['duration_seconds'],'source_segments':[str(p.relative_to(ROOT)) for p in files],
          'source_timeline_map':timeline_map,'time_map':time_map,**map_note,
          'motion_profile':str(profile_path.relative_to(ROOT)) if mode=='intelligent' else None}
    save_json(output.with_suffix('.manifest.json'),meta);print(json.dumps(meta,indent=2))

def _safe_old_file(path: Path, now: datetime, age: timedelta, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve(strict=True)
        if not path.is_file() or path.is_symlink():
            return False
        if not any(resolved.is_relative_to(root.resolve()) for root in roots):
            return False
        return datetime.fromtimestamp(path.stat().st_mtime, now.tzinfo) < now - age
    except (OSError, RuntimeError, ValueError):
        return False


def purge(preview=True,confirm=''):
    cfg=settings();now=datetime.now().astimezone();candidates=[];blocked=[]
    master_enabled=bool(cfg.get('PURGE_ENABLED',False))
    dry=bool(cfg.get('PURGE_DRY_RUN',True))
    original_enabled=bool(cfg.get('PURGE_ORIGINAL_AFTER_VERIFIED_ARCHIVE',False))
    cache_enabled=bool(cfg.get('PURGE_CACHE_PLAYBACK_ENABLED',False))
    temp_enabled=bool(cfg.get('PURGE_TEMP_JOB_FILES_ENABLED',False))
    logs_enabled=bool(cfg.get('PURGE_ROTATED_LOGS_ENABLED',False))
    if not master_enabled:print('PURGE DISABLED: set PURGE_ENABLED = True in USER_CONTROL.py.')
    if dry:print('DRY RUN: no files will be deleted.')
    if not any((original_enabled,cache_enabled,temp_enabled,logs_enabled)):
        print('No purge category is enabled. Review the PURGE section in USER_CONTROL.py.')
    if not bool(cfg.get('PURGE_KEEP_AT_LEAST_ONE_VERIFIED_COPY',True)):
        print('PURGE SAFETY: keeping at least one verified copy is mandatory; deletion blocked.')
        master_enabled=False
    if cfg.get('PURGE_KEEP_PROTECTED_ORIGINALS',True) is not True:
        print('PURGE SAFETY: protected originals must always be retained; deletion blocked.')
        master_enabled=False
    if cfg.get('PURGE_KEEP_BACKUPS_ALWAYS',True) is not True or cfg.get('PURGE_KEEP_BUILD_CACHE_ALWAYS',cfg.get('PURGE_KEEP_DOCKER_IMAGE_CACHE_ALWAYS',True)) is not True:
        print('PURGE SAFETY: backup and build-cache exclusions are hard requirements; deletion blocked.')
        master_enabled=False

    # ORIGINAL FOOTAGE: only segments with a verified archive copy, intact hashes,
    # complete day coverage, age beyond retention and no protected overlap qualify.
    if original_enabled:
        keep=max(0,int(cfg.get('PURGE_ORIGINAL_RETENTION_DAYS',cfg.get('RECORDING_ORIGINAL_RETENTION_DAYS',4))))
        cutoff=now-timedelta(days=keep)
        verified_by_source={}
        for manifest_path in ARCHIVES.rglob('*.manifest.json'):
            try:
                obj=json.loads(manifest_path.read_text(encoding='utf-8'))
                if not obj.get('verified') or not obj.get('archive_parts'):
                    continue
                outputs_ok=True
                for part in obj['archive_parts']:
                    rel=str(part.get('file','')); output=(ROOT/rel).resolve()
                    if not output.is_relative_to(ARCHIVES.resolve()) or not output.is_file():
                        outputs_ok=False;break
                    if output.stat().st_size!=int(part.get('size_bytes',-1)):
                        outputs_ok=False;break
                    expected=part.get('sha256')
                    if expected and sha256_file(output)!=expected:
                        outputs_ok=False;break
                    ok,_info=verify(output)
                    if not ok:
                        outputs_ok=False;break
                if not outputs_ok:
                    for rec in obj.get('source_segment_records',[]):
                        verified_by_source.pop(rec.get('file'),None)
                    blocked.append((str(manifest_path.relative_to(ROOT)),'archive output failed integrity/decode verification'))
                    continue
                for rec in obj.get('source_segment_records',[]):
                    if rec.get('complete_coverage') and not rec.get('protected_overlap'):
                        verified_by_source[rec['file']]={'manifest':str(manifest_path.relative_to(ROOT)),'source':rec}
            except Exception as exc:
                blocked.append((str(manifest_path.relative_to(ROOT)),'manifest verification failed: '+str(exc)[:150]))
        protections=load_json(BOOKMARKS,{'bookmarks':[]}).get('bookmarks',[])
        for media in ORIGINAL.rglob('*'):
            if not media.is_file() or media.suffix.lower() not in ('.ts','.mp4','.mkv'):continue
            rel=str(media.relative_to(ROOT));match=verified_by_source.get(rel)
            try:created=media_start(media).astimezone()
            except Exception:created=datetime.fromtimestamp(media.stat().st_mtime).astimezone()
            if created>=cutoff:continue
            if not match:
                blocked.append((rel,'no fully covered, verified archive copy'));continue
            rec=match['source']
            if not rec.get('complete_coverage') or rec.get('protected_overlap'):
                blocked.append((rel,'partial coverage or protected overlap'));continue
            if int(rec.get('size_bytes',-1))!=media.stat().st_size or (rec.get('sha256') and sha256_file(media)!=rec['sha256']):
                blocked.append((rel,'source size/hash changed since archive verification'));continue
            try:duration=ffprobe_duration(media)
            except Exception:
                blocked.append((rel,'source duration cannot be verified'));continue
            a=created;b=a+timedelta(seconds=duration);hit=False
            for mark in protections:
                if media.parent.name not in mark.get('camera_ids', [mark.get('camera_id')]) or not mark.get('protected'):continue
                try:
                    if datetime.fromisoformat(mark['start']).astimezone()<b and datetime.fromisoformat(mark['end']).astimezone()>a:hit=True;break
                except Exception:continue
            if hit:
                blocked.append((rel,'protected interval overlaps this segment; entire segment retained'));continue
            candidates.append((media,rel,'original','verified archive: '+match['manifest']))

    # PLAYBACK/WORK CACHE: explicit narrow allowlist. Never inspect runtime downloads,
    # build caches, config, backups, recordings, or camera credential files.
    cache_roots=[ROOT/'cache'/'playback',ROOT/'cache'/'estimate',ROOT/'cache'/'timelapse-work']
    if cache_enabled:
        age=timedelta(days=max(0,int(cfg.get('PURGE_CACHE_RETENTION_DAYS',7))))
        for base in cache_roots:
            if not base.exists():continue
            for item in base.rglob('*'):
                if _safe_old_file(item,now,age,[base]):
                    candidates.append((item,str(item.relative_to(ROOT)),'cache',f'cache older than {age}'))

    # TEMPORARY JOB FILES: only known work/cache directories and clearly partial
    # files. Runtime binaries and download caches are never in this allowlist.
    temp_roots=[ROOT/'cache'/'archive-work',ROOT/'cache'/'timelapse-work',ROOT/'cache'/'estimate',ROOT/'cache'/'playback']
    if temp_enabled:
        age=timedelta(hours=max(1,int(cfg.get('PURGE_TEMP_RETENTION_HOURS',48))))
        for base in temp_roots:
            if not base.exists():continue
            for item in base.rglob('*'):
                if not item.is_file() or item.is_symlink():continue
                if not ('.partial' in item.name or item.name.endswith('.tmp') or item.suffix=='.ffconcat' or item.name.endswith('_sample.mp4')):continue
                if _safe_old_file(item,now,age,[base]):
                    candidates.append((item,str(item.relative_to(ROOT)),'temporary',f'temporary job file older than {age}'))

    # ROTATED LOGS ONLY. Active logs and backups are intentionally excluded.
    if logs_enabled:
        import re as _re
        age=timedelta(days=max(1,int(cfg.get('PURGE_LOG_RETENTION_DAYS',30))))
        pattern=_re.compile(r'.+\.log(?:\.\d+|\.\d+\.gz|-\d{4}[-_]\d{2}[-_]\d{2}(?:[-_]\d{6})?|\.gz|\.old)$',_re.I)
        if LOGS.exists():
            for item in LOGS.iterdir():
                if item.is_file() and not item.is_symlink() and pattern.fullmatch(item.name) and _safe_old_file(item,now,age,[LOGS]):
                    candidates.append((item,str(item.relative_to(ROOT)),'rotated-log',f'rotated log older than {age}'))

    # De-duplicate a path that qualifies under more than one cleanup category.
    unique={}
    for entry in candidates:unique.setdefault(str(entry[0].resolve()),entry)
    candidates=list(unique.values())
    categories=('original','cache','temporary','rotated-log')
    counts={k:sum(1 for x in candidates if x[2]==k) for k in categories}
    sizes={k:sum(x[0].stat().st_size for x in candidates if x[2]==k and x[0].is_file()) for k in categories}
    total_candidate_bytes=sum(sizes.values())
    print('Purge preview by category: '+', '.join(f'{k}={counts[k]} files ({sizes[k]} bytes)' for k in categories))
    print(f'Estimated reclaimable space: {total_candidate_bytes} bytes ({total_candidate_bytes/1024**3:.3f} GiB). This is an estimate; files may change before execution.')
    print(f'Retained/blocked items: {len(blocked)}. Backups and cache/images are excluded unconditionally.')
    for _p,rel,category,reason in candidates[:int(cfg.get('PURGE_REPORT_MAX_ITEMS',500))]:print(f'ELIGIBLE [{category}] {rel} — {reason}')
    for rel,reason in blocked[:int(cfg.get('PURGE_REPORT_MAX_ITEMS',500))]:print('KEEP '+rel+' — '+reason)
    if preview:return
    required=cfg.get('PURGE_CONFIRMATION_TEXT','PURGE CCTV DATA')
    if not master_enabled or dry:raise SystemExit('PURGE STOP: master authorization is disabled or dry-run remains enabled.')
    if not any((original_enabled,cache_enabled,temp_enabled,logs_enabled)):raise SystemExit('PURGE STOP: no cleanup category is enabled.')
    if cfg.get('PURGE_REQUIRE_TYPED_CONFIRMATION',True) and confirm!=required:raise SystemExit('PURGE STOP: exact confirmation phrase was not supplied.')
    limit=max(1,min(int(cfg.get('PURGE_MAX_TOTAL_FILES_PER_RUN',cfg.get('PURGE_MAX_FILES_PER_RUN',500))),int(cfg.get('PURGE_MAX_FILES_PER_RUN',500))))
    deleted=0;failures=0
    for media,rel,category,_reason in candidates[:limit]:
        try:
            # Re-check containment, symlink status and file existence immediately before unlink.
            target=media.resolve(strict=True)
            roots=([ORIGINAL.resolve()] if category=='original' else
                   [x.resolve() for x in cache_roots if x.exists()] if category in ('cache','temporary') else
                   [LOGS.resolve()])
            if media.is_symlink() or not any(target.is_relative_to(root) for root in roots):
                raise RuntimeError('path escaped the category allowlist')
            if target.is_file():target.unlink();deleted+=1;print(f'DELETED [{category}] {rel}')
        except Exception as exc:
            failures+=1;print(f'FAILED {rel}: {exc}')
    if cfg.get('PURGE_REMOVE_EMPTY_JOB_DIRECTORIES',True):
        for base in (ROOT/'cache'/'archive-work',ROOT/'cache'/'timelapse-work',ROOT/'cache'/'estimate',ROOT/'cache'/'playback'):
            if base.exists():
                for d in sorted((x for x in base.rglob('*') if x.is_dir()),key=lambda x:len(x.parts),reverse=True):
                    try:d.rmdir()
                    except OSError:pass
    print(f'Purge complete: deleted={deleted}, failures={failures}, batch_limit={limit}. Backups, config and cache/images were not touched.')


def main():
    p=argparse.ArgumentParser(description='CCTV Portable archive and purge tools');sub=p.add_subparsers(dest='action',required=True)
    b=sub.add_parser('build');b.add_argument('--camera',required=True);b.add_argument('--day',required=True)
    e=sub.add_parser('estimate');e.add_argument('--camera',required=True);e.add_argument('--day',required=True)
    t=sub.add_parser('timelapse');t.add_argument('--camera',required=True);t.add_argument('--day',required=True);t.add_argument('--mode',choices=['regular','intelligent'],required=True)
    pu=sub.add_parser('purge');pu.add_argument('--preview',action='store_true');pu.add_argument('--execute',action='store_true');pu.add_argument('--confirm',default='')
    a=p.parse_args()
    if a.action=='build':build(a.camera,a.day)
    elif a.action=='estimate':estimate(a.camera,a.day)
    elif a.action=='timelapse':generate_timelapse(a.camera,a.day,a.mode)
    else:purge(preview=not a.execute,confirm=a.confirm)

if __name__=='__main__':main()
