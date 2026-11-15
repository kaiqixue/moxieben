"""Literal identity evidence and conservative name candidates; never reads answers."""
import re
from collections import Counter


def comparable_name(name):
    match=re.fullmatch(r'([\u4e00-\u9fff·]{2,40})([0-9０-９]+)',name)
    return match[1] if match else name


def name_edits(observed, expected):
    """Small literal Levenshtein alignment; edits transform OCR text to roster text."""
    costs=[[0]*(len(expected)+1) for _ in range(len(observed)+1)]
    for i in range(len(observed)+1):costs[i][0]=i
    for j in range(len(expected)+1):costs[0][j]=j
    for i,a in enumerate(observed,1):
        for j,b in enumerate(expected,1):
            costs[i][j]=min(costs[i-1][j]+1,costs[i][j-1]+1,costs[i-1][j-1]+(a!=b))
    edits=[];i,j=len(observed),len(expected)
    while i or j:
        if i and j and costs[i][j]==costs[i-1][j-1]+(observed[i-1]!=expected[j-1]):
            if observed[i-1]!=expected[j-1]:
                edits.append({'index':i-1,'observed':observed[i-1],'expected':expected[j-1]})
            i-=1;j-=1
        elif i and costs[i][j]==costs[i-1][j]+1:
            edits.append({'op':'delete','index':i-1,'observed':observed[i-1],'expected':''});i-=1
        else:
            edits.append({'op':'insert','index':i,'observed':'','expected':expected[j-1]});j-=1
    return costs[-1][-1],list(reversed(edits))


def candidate_key(candidate):
    return (candidate.get('rank',0),candidate.get('distance',0),candidate['student_code'],candidate['id'])


def primary_candidates(candidates):
    return [c for c in candidates if not c.get('supplementary',False)]


def difference_text(difference):
    op=difference.get('op','replace')
    if op=='suffix':return '忽略姓名末尾数字'
    if op=='insert':return f"第 {difference['index']+1} 字处补入“{difference['expected']}”"
    if op=='delete':return f"去掉第 {difference['index']+1} 字“{difference['observed']}”"
    return f"第 {difference['index']+1} 字不同（{difference['observed']} → {difference['expected']}）"


def identity_evidence(blocks, students, band=None):
    """Old full-code / name evidence, optionally reconciled with answer-card band evidence.

    ``band`` comes from ``student_id_band.read_band`` (None when the assignment has no card
    format). A valid band only wins when nothing on the photo points at another student.
    """
    result = _text_evidence(blocks, students)
    result.update(method='exact', band=band)
    if not band or band['state'] == 'absent':
        return result
    if band['state'] in ('template_mismatch', 'conflict'):
        result.update(blocked=True, exact_id=None, reason=band['reason'])
        for item in result['candidates']:
            item['auto_eligible'] = False
        return result
    if band['state'] == 'incomplete':
        if not result['exact_id']:
            result['reason'] = band['reason'] + result['reason']
        return result
    target = band['student_id']
    roster = {s['id']: s for s in students}
    if result['code'] and result['code'] != band['code']:
        conflict = f"完整学号 {result['code']} 与学号栏 {band['code']} 不一致，请核对原图。"
    elif result['blocked']:
        conflict = result['reason']
    elif result['exact_id'] and result['exact_id'] != target:
        conflict = f"学号与姓名指向不同学生：学号栏为 {band['code']}，姓名为“{roster[result['exact_id']]['name']}”。"
    else:
        conflict = _close_name_conflict(result['candidates'], target, band, roster)
    if conflict:
        result.update(blocked=True, exact_id=None, reason=conflict)
        for item in result['candidates']:
            item['auto_eligible'] = False
        return result
    agreed = result['exact_id'] == target
    result.update(exact_id=target, blocked=False, method='band',
                  reason=band['reason'] + ('姓名一致。' if agreed else '姓名未能对应名单，仅按学号栏归属，请抽查原图。'))
    return result


def _close_name_conflict(candidates, target, band, roster):
    """A strong (one-character) name match for another student blocks a band match."""
    own = min((c.get('distance', 0) for c in candidates if c['id'] == target), default=None)
    for c in candidates:
        if c['id'] == target or not c.get('differences'):
            continue
        observed = comparable_name(c.get('observed_name') or '')
        strong = c.get('distance', 9) <= 1 and min(len(observed), len(comparable_name(c['name']))) >= 3
        if strong and (own is None or c.get('distance', 0) < own):
            return (f"学号与姓名指向不同学生：学号栏为 {band['code']}（{roster[target]['name']}），"
                    f"姓名读作“{c['observed_name']}”，与“{c['name']}”仅差一字。")
    return ''


def _text_evidence(blocks, students):
    text='\n'.join(str(b.get('text','')).strip() for b in blocks if b.get('text')).strip()
    codes=set(re.findall(r'(?:学号|编号|学籍号)\s*[:：]?\s*([A-Za-z0-9_-]{1,80})',text,re.I))
    labelled_names=set(re.findall(r'(?:姓名|名字)\s*[:：]?\s*([\u4e00-\u9fff·]{1,40}[0-9０-９]*)',text))
    names=set(labelled_names)
    # ponytail: bare Chinese lines are name hints; require labels if real samples show title collisions.
    if not names:
        for line in text.splitlines():
            clean=line.strip().rstrip('，,。.;；').rstrip()
            if re.fullmatch(r'[\u4e00-\u9fff·]{2,5}[0-9０-９]*',clean):
                names.add(clean)
            pair=re.fullmatch(r'([A-Za-z0-9_-]{1,80})\s+([\u4e00-\u9fff·]{2,40}[0-9０-９]*)',clean)
            if pair:
                codes.add(pair[1]);names.add(pair[2])
    if not codes:
        codes={s['student_code'] for s in students if s['student_code'] in {line.strip() for line in text.splitlines()}}
    candidates=[]
    relevant_names=set()
    duplicate_names=Counter(comparable_name(s['name']) for s in students)
    for s in students:
        matches=[]
        for observed in sorted(names):
            expected=s['name']
            exact=observed==expected
            left,right=comparable_name(observed),comparable_name(expected)
            normalized=left!=observed or right!=expected
            distance,edits=name_edits(left,right)
            if exact or (all(re.fullmatch(r'[\u4e00-\u9fff·]{2,5}',n) for n in (left,right)) and distance<=2):
                relevant_names.add(observed)
                comparison='exact' if exact else 'suffix' if distance==0 else 'suffix_edit' if normalized else 'edit'
                if normalized and not exact:
                    edits=edits+[{'op':'suffix','index':len(left),'observed':observed[len(left):],'expected':expected[len(right):]}]
                eligible=(comparison=='suffix' or (comparison=='edit' and distance==1
                    and all(re.fullmatch(r'[\u4e00-\u9fff]{3,5}',n) for n in (left,right)) and min(len(left),len(right))<=4))
                rank=0 if exact else 1 if comparison=='suffix' else 2 if comparison=='edit' and distance==1 else 3
                matches.append({k:s[k] for k in ('id','student_code','name')} | {
                    'observed_name':observed,'differences':[] if exact else edits,
                    'comparison':comparison,'distance':distance,'rank':rank,
                    'ambiguous_name':duplicate_names[right]>1,
                    'auto_eligible':exact or (eligible and len(names)==1 and duplicate_names[right]==1)})
        if matches:
            candidates.append(min(matches,key=candidate_key))
    code=next(iter(codes),None) if len(codes)==1 else None
    known_codes=[s for s in students if s['student_code']==code]
    known_names=[s for s in students if s['name'] in names]
    if known_names:
        candidates=[c for c in candidates if not c['differences']]
    candidates.sort(key=candidate_key)
    best_rank=min((c['rank'] for c in candidates),default=0)
    for candidate in candidates:candidate['supplementary']=candidate['rank']>best_rank
    observed=next(iter(relevant_names),None) if len(relevant_names)==1 else (next(iter(names),None) if len(names)==1 else None)
    exact_id=None;blocked=False;reason='未找到唯一姓名，请从名单中选择学生。'
    if len(codes)>1 or len(labelled_names)>1 or len(relevant_names)>1:
        blocked=True;reason='识别到多个身份信息，请核对后选择。'
    elif code and len(known_codes)!=1:
        blocked=True;reason='识别学号不在本次名单中，不能按相似姓名自动归属。'
    elif len(known_names)>1:
        blocked=True;reason='名单中存在同名学生，请核对学号后选择。'
        if code and len(known_codes)==1 and known_codes[0]['name'] in names:
            blocked=False;exact_id=known_codes[0]['id']
    elif code and known_names and known_codes[0]['id']!=known_names[0]['id']:
        blocked=True;reason='学号和姓名指向不同学生，请人工确认。'
    elif code and ((not labelled_names and not relevant_names) or (known_names and known_codes[0]['id']==known_names[0]['id'])):
        exact_id=known_codes[0]['id']
    elif not code and len(known_names)==1:
        exact_id=known_names[0]['id']
    if code and known_codes and candidates and not exact_id and all(c['student_code']!=code for c in candidates):
        blocked=True;reason='学号和相近姓名指向不同学生，请人工确认。'
    if code and not candidates and known_codes:
        candidates=[{k:known_codes[0][k] for k in ('id','student_code','name')} | {
            'observed_name':observed,'differences':[],'auto_eligible':False}]
    # Explicit contradictory labels remain visible but cannot win by elimination.
    if code:
        for item in candidates:
            if item['student_code']!=code:
                item['auto_eligible']=False
    return {'text':text,'code':code,'name':observed,'names':sorted(names),
            'exact_id':exact_id,'blocked':blocked,'reason':reason,'candidates':candidates}
