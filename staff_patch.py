# PC-only payout recovery endpoint. Credentials remain in Render environment.
import time
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal
from app import app, LOCK, connect, bump, require_import_key, _PC_CORS

class RestoreStatus(BaseModel):
    number: int = Field(ge=1,le=100000)
    status: Literal['대기','지급완료','미수령']
    operator: str = ''
    updated_at: float = 0

class ReconcileInput(BaseModel):
    records: list[list[int]] = Field(min_length=1,max_length=1000)
    labels: list[str] = Field(default_factory=list)
    statuses: list[RestoreStatus] = Field(default_factory=list)

@app.options('/api/reconcile')
def reconcile_preflight():
    return JSONResponse({'ok':True},headers=_PC_CORS)

@app.post('/api/reconcile')
def reconcile_from_pc(payload:ReconcileInput, request:Request):
    require_import_key(request)
    all_nums=[n for rr in payload.records for n in rr]
    if not all_nums or len(all_nums)>100000 or any(type(n)!=int or n<1 or n>100000 for n in all_nums) or len(set(all_nums))!=len(all_nums):
        raise HTTPException(400,'PC 추첨 기록이 비어 있거나 중복되어 복원을 중지했습니다.')
    permitted={'기본 추첨','일반 추첨','행운권 동상','행운권 은상','행운권 금상','행운권 대상'}
    if any(label not in permitted for label in payload.labels):
        raise HTTPException(400,'알 수 없는 추첨 항목')
    known=set(all_nums);backups={}
    for row in payload.statuses:
        if row.number not in known or row.number in backups:
            raise HTTPException(400,'잘못된 백업 지급 번호')
        backups[row.number]=row
    restored=0;kept=0
    with LOCK,connect() as db:
        db.execute('BEGIN IMMEDIATE')
        placeholders=','.join('?'*len(known))
        db.execute('DELETE FROM winners WHERE number NOT IN ('+placeholders+')',tuple(known))
        for i,nums in enumerate(payload.records,1):
            tier=payload.labels[i-1] if i<=len(payload.labels) else '기본 추첨'
            db.execute('INSERT INTO rounds(round_no,tier,created_at) VALUES(?,?,?) ON CONFLICT(round_no) DO UPDATE SET tier=excluded.tier',(i,tier,time.time()))
            for n in nums:
                old=db.execute('SELECT status,revision FROM winners WHERE number=?',(n,)).fetchone()
                back=backups.get(n)
                if old is None:
                    st=back.status if back else '대기'
                    db.execute('INSERT INTO winners(number,round_no,status,revision,operator,updated_at) VALUES(?,?,?,?,?,?)',(n,i,st,1 if st!='대기' else 0,back.operator[:30] if back and st!='대기' else '',float(back.updated_at) if back and st!='대기' else 0))
                    if st!='대기':restored+=1
                elif old['status']=='대기' and old['revision']==0 and back and back.status!='대기':
                    db.execute('UPDATE winners SET round_no=?,status=?,revision=1,operator=?,updated_at=? WHERE number=?',(i,back.status,back.operator[:30],float(back.updated_at),n));restored+=1
                else:
                    db.execute('UPDATE winners SET round_no=? WHERE number=?',(i,n))
                    if old['status']!='대기':kept+=1
        db.execute('DELETE FROM rounds WHERE round_no NOT IN ('+','.join('?'*len(payload.records))+')',tuple(range(1,len(payload.records)+1)))
        db.execute('INSERT INTO audit(number,operator,at,action) VALUES(?,?,?,?)',(None,'행사 PC',time.time(),'pc-reconcile'))
        bump(db);db.commit()
    return JSONResponse({'ok':True,'winners':len(all_nums),'rounds':len(payload.records),'restored_statuses':restored,'preserved_statuses':kept},headers=_PC_CORS)
