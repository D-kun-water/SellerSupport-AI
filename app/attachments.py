"""Preserve uploaded originals in SQLite; extracted customer text is not knowledge."""
import base64,csv,hashlib,io,json,zipfile
from pathlib import PurePath
from copy import deepcopy
from .domain import DomainError,now
from .store import uid
from . import services as svc

MAX_BYTES=5*1024*1024
MAX_TEXT=200000

def decode_file(filename,encoded):
    if not filename or '/' in filename or '\\' in filename or len(filename)>200:
        raise DomainError('VALIDATION_ERROR','文件名不得含路径')
    if len(encoded)>((MAX_BYTES+2)//3)*4:
        raise DomainError('VALIDATION_ERROR','单文件上限5MB')
    try:raw=base64.b64decode(encoded,validate=True)
    except Exception:raise DomainError('VALIDATION_ERROR','文件编码不合法')
    if not raw or len(raw)>MAX_BYTES:raise DomainError('VALIDATION_ERROR','空文件或文件超过5MB')
    ext=PurePath(filename).suffix.lower()
    if ext not in {'.txt','.md','.json','.csv','.pdf','.docx','.png','.jpg','.jpeg'}:
        raise DomainError('VALIDATION_ERROR','不支持此附件格式')
    return raw,ext

def parse(raw,ext):
    locators=[]
    try:
        if ext in {'.txt','.md','.json','.csv'}:
            text=raw.decode('utf-8-sig')
            if '\x00' in text:raise ValueError('文本包含二进制内容')
            if ext=='.json':json.loads(text)
            if ext=='.csv':list(csv.reader(io.StringIO(text)))
            locators=[dict(locator=f'line:{i}',text=line) for i,line in enumerate(text.splitlines(),1)]
        elif ext=='.pdf':
            if not raw.startswith(b'%PDF-'):raise ValueError('不是有效PDF文件')
            from pypdf import PdfReader
            reader=PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:raise ValueError('加密PDF需人工处理')
            if len(reader.pages)>100:raise ValueError('PDF超过100页限制')
            for i,page in enumerate(reader.pages,1):
                contents=page.get_contents()
                if contents and len(contents.get_data())>MAX_BYTES*4:raise ValueError('PDF页面展开内容过大')
                locators.append(dict(locator=f'page:{i}',text=page.extract_text() or ''))
            text='\n'.join(p['text'] for p in locators)
        elif ext=='.docx':
            from defusedxml.ElementTree import fromstring
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if sum(i.file_size for i in z.infolist())>20*1024*1024:raise ValueError('DOCX解压内容过大')
                root=fromstring(z.read('word/document.xml'))
                ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                paragraphs=[''.join(p.itertext()) for p in root.findall('.//w:p',ns)]
                locators=[dict(locator=f'paragraph:{i}',text=p) for i,p in enumerate(paragraphs,1)]
                text='\n'.join(paragraphs)
        else:
            if ext=='.png' and not raw.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('PNG签名不匹配')
            if ext in {'.jpg','.jpeg'} and not raw.startswith(b'\xff\xd8\xff'):raise ValueError('JPEG签名不匹配')
            return dict(parse_status='manual_required',text='',locators=[],parse_note='图片原件保留；未运行OCR，请人工填写可核对内容')
        if len(text)>MAX_TEXT:raise ValueError('提取文本超过20万字符限制，请拆分文件')
        return dict(parse_status='parsed' if text.strip() else 'manual_required',text=text,locators=locators,
                    parse_note='解析文本待人工核对；PDF仅提取可见文本，不保证表格和布局语义' if ext=='.pdf' else '解析结果待人工核对')
    except Exception as exc:
        return dict(parse_status='error',text='',locators=[],parse_note=f'解析失败：{type(exc).__name__}；原件保留，请人工核对')

def preview(store,db,actor,ticket_id,p):
    t=svc.ticket_for(store,db,ticket_id,actor)
    svc.version(t,p['version'])
    raw,ext=decode_file(p['filename'],p['content_base64'])
    sha=hashlib.sha256(raw).hexdigest()
    for old in store.all(db,'attachments'):
        if old['ticket_id']==ticket_id and old['sha256']==sha:return old
    existing=[a for a in store.all(db,'attachments') if a['ticket_id']==ticket_id]
    if len(existing)>=20 or sum(a['size_bytes'] for a in existing)+len(raw)>25*1024*1024:
        raise DomainError('VALIDATION_ERROR','每工单上限20附件、合计25MB')
    row=dict(id=uid('ATT'),ticket_id=ticket_id,source_type='local',origin='customer_provided_untrusted',filename=p['filename'],
             extension=ext,size_bytes=len(raw),sha256=sha,original_base64=base64.b64encode(raw).decode(),
             state='preview',review_status='pending',version=1,actor=actor['id'],created_at=now(),**parse(raw,ext))
    store.put(db,'attachments',row['id'],row)
    store.audit(db,actor,'attachment_previewed_not_knowledge',row['id'],{'sha256':sha,'parse_status':row['parse_status']})
    return row

def attachment_for(store,db,actor,key):
    row=store.get(db,'attachments',key)
    svc.ticket_for(store,db,row['ticket_id'],actor)
    return row

def commit(store,db,actor,key,p):
    row=attachment_for(store,db,actor,key)
    if row['state']=='committed':return row
    svc.version(row,p['version'])
    if row['parse_status']=='error':raise DomainError('VALIDATION_ERROR','解析失败附件须先人工核对或拒绝，不能直接提交')
    if row['review_status']=='rejected':raise DomainError('INVALID_STATE','已拒绝附件不能提交',409)
    t=svc.ticket_for(store,db,row['ticket_id'],actor)
    row.update(state='committed',version=row['version']+1,committed_at=now())
    t.setdefault('attachment_ids',[]).append(key)
    t.update(version=t['version']+1,context_version=t['context_version']+1,draft='',copied=False,clarification_rounds=0)
    store.put(db,'tickets',t['id'],t);store.put(db,'attachments',key,row)
    store.audit(db,actor,'attachment_committed_not_knowledge',key)
    return row

def review(store,db,actor,key,p):
    svc.role(actor,{'supervisor'})
    row=attachment_for(store,db,actor,key);svc.version(row,p['version'])
    if row['review_status']!='pending':raise DomainError('INVALID_STATE','已审核附件不可改写，请上传新版本',409)
    if p['decision']=='confirm' and not p['text'].strip():raise DomainError('VALIDATION_ERROR','人工核对必须填写可核对文本')
    row.update(review_status='confirmed' if p['decision']=='confirm' else 'rejected',reviewed_text=p['text'],review_reason=p['reason'],
               reviewed_by=actor['id'],reviewed_at=now(),version=row['version']+1)
    if p['decision']=='confirm' and row['parse_status']=='error':row['parse_status']='manually_transcribed'
    store.put(db,'attachments',key,row)
    if row['state']=='committed':
        t=svc.ticket_for(store,db,row['ticket_id'],actor)
        t.update(version=t['version']+1,context_version=t['context_version']+1,draft='',copied=False,clarification_rounds=0)
        store.put(db,'tickets',t['id'],t)
    store.audit(db,actor,'customer_attachment_reviewed_not_product_fact',key)
    return row

def summary(row):
    return {k:v for k,v in row.items() if k!='original_base64'}

def import_preview(store,db,actor,p):
    raw,ext=decode_file(p['filename'],p['content_base64'])
    if ext not in {'.json','.csv'}:raise DomainError('VALIDATION_ERROR','多消息导入使用JSON或CSV')
    try:
        if ext=='.json':messages=json.loads(raw.decode('utf-8-sig'))
        else:messages=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
        from .attachment_api import Message
        from pydantic import TypeAdapter
        messages=TypeAdapter(list[Message]).validate_python(messages)
        if not messages or len(messages)>1000 or messages[0].role!='customer':raise ValueError('需客户首条消息，最多1000条')
        messages=[m.model_dump() for m in messages]
        from .domain import timestamp
        if any(timestamp(a['received_at'])>timestamp(b['received_at']) for a,b in zip(messages,messages[1:])):raise ValueError('消息须按时间递增，不自动重排')
    except Exception as exc:raise DomainError('VALIDATION_ERROR','多消息文件不符合role/text/received_at格式或时间顺序')
    if p.get('order_id'):svc.order_for(store,db,p['order_id'],actor)
    for t in store.all(db,'tickets'):
        if t.get('external_id')==p['external_id']:
            svc.ticket_for(store,db,t['id'],actor)
            raise DomainError('VERSION_CONFLICT','该外部工单已存在，不能覆盖对话',409)
    row=dict(id=uid('CONV'),actor=actor['id'],source_type='local',state='preview',version=1,filename=p['filename'],
             original_base64=base64.b64encode(raw).decode(),sha256=hashlib.sha256(raw).hexdigest(),messages=messages,
             ticket_fields={k:p.get(k) for k in ('external_id','order_id','sku','device_id','use_case','region')},created_at=now())
    store.put(db,'conversation_imports',row['id'],row)
    return row

def import_commit(store,db,actor,key):
    row=store.get(db,'conversation_imports',key)
    if row['actor']!=actor['id']:raise DomainError('FORBIDDEN','只能提交本人预览批次',403)
    if row['state']=='committed':return row
    for old in store.all(db,'tickets'):
        if old.get('external_id')==row['ticket_fields']['external_id']:
            raise DomainError('VERSION_CONFLICT','预览后外部工单已导入，请重新核对',409)
    t=svc.create_ticket(store,db,actor,dict(**row['ticket_fields'],source_type='local',channel='direct',message=row['messages'][0]['text']))
    t['messages']=[dict(id=uid('MSG'),**m,source_import=row['id'],source_locator=f'row:{i}') for i,m in enumerate(row['messages'],1)]
    store.put(db,'tickets',t['id'],t)
    row.update(state='committed',ticket_id=t['id'],version=row['version']+1)
    store.put(db,'conversation_imports',key,row)
    store.audit(db,actor,'full_conversation_imported',key,{'messages':len(row['messages'])})
    return row
