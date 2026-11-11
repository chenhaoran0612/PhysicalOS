"""Managed-resource catalog and prepaid credit ledger (not a payment processor).

Credits are integer platform units. No exchange rate to currency is assumed.
Reservations bound exposure; settlement must come from an authenticated operator.
"""
from . import connector
from .contracts import required_text
from .service import uid, now
from .store import Store


class Commerce:
    def __init__(self,root):
        self.store=Store(root)

    def publish(self,p):
        kind=p.get('kind')
        if kind not in ('training','inference','agent','evaluation','simulation','reasoning'):
            raise ValueError('资源类型无效')
        price=p.get('credits_per_unit')
        if isinstance(price,bool) or not isinstance(price,int) or not 1<=price<=1000000:
            raise ValueError('每计量单位需要 1–1000000 个整数额度点')
        # Publication requires a real compatible service handshake.
        endpoint=connector.endpoint_url(p.get('endpoint'))
        token=p.get('token','')
        if not isinstance(token,str) or len(token)>2048 or '\n' in token or '\r' in token:
            raise ValueError('服务令牌无效')
        manifest=connector.request(endpoint,'/v1/manifest',token)
        if manifest.get('schema')!='platform.service.v1' or manifest.get('kind')!=kind:
            raise ValueError('资源服务未通过协议检查，不能上架')
        from .recipes import validate_recipes
        validate_recipes(manifest)
        if kind=='reasoning':
            models=manifest.get('models')
            if not isinstance(models,list) or not models or any(not isinstance(m,str) or not m or len(m)>200 for m in models):
                raise ValueError('工程模型网关必须返回模型目录')
            if manifest.get('protocol')!='responses' or manifest.get('metered_sessions') is not True or p.get('unit')!='1k_tokens':
                raise ValueError('工程模型网关需支持 Responses、硬限额会话与 1k_tokens 计量')
        obj={'id':uid('resource'),'name':required_text(p.get('name'),'资源名称',100),'kind':kind,
             'description':required_text(p.get('description'),'资源说明',1000),
             'unit':required_text(p.get('unit'),'计量单位',50),'credits_per_unit':price,
             'endpoint':endpoint,'token':token,'manifest':manifest,'status':'available','created_at':now()}
        self.store.put('resources',obj)
        return self.public_resource(obj)

    @staticmethod
    def public_resource(obj):
        return {k:v for k,v in obj.items() if k not in ('endpoint','token')}

    def catalog(self):
        return [self.public_resource(r) for r in self.store.list('resources')]

    def publish_model(self,p):
        source=p.get('source')
        if source not in ('native','open','closed'):
            raise ValueError('模型来源须为自研、开源或闭源服务')
        obj={'id':uid('catalog-model'),'name':required_text(p.get('name'),'模型名称',100),'source':source,
             'version':required_text(p.get('version'),'版本'),'model_ref':required_text(p.get('model_ref'),'模型版本引用',500),
             'task_type':required_text(p.get('task_type'),'能力类型'),
             'input_contract':required_text(p.get('input_contract'),'输入契约',1000),
             'output_contract':required_text(p.get('output_contract'),'输出契约',1000),
             'license':required_text(p.get('license'),'许可与使用范围',1000),'created_at':now(),'status':'published'}
        if p.get('resource_id'):
            resource=self.store.get('resources',p['resource_id'])
            if resource['kind']!='inference':raise ValueError('模型调用需绑定推理资源')
            obj['resource_id']=resource['id']
        if source=='closed' and not obj.get('resource_id'):
            raise ValueError('闭源模型需要绑定平台推理网关')
        if source in ('native','open'):
            from .contracts import FAMILIES,string_list
            obj['package_ref']=required_text(p.get('package_ref'),'模型部署包引用',1000)
            sha=required_text(p.get('package_digest'),'模型包 SHA256').lower()
            if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('模型包摘要须为 64 位 SHA256')
            obj['package_digest']=sha
            obj['runtime']=required_text(p.get('runtime'),'端侧推理运行时',100)
            obj['families']=string_list(p.get('families'),'已声明兼容形态')
            if any(f not in FAMILIES for f in obj['families']):raise ValueError('兼容形态无效')
        self.store.put('model_catalog',obj)
        return obj

    def model_catalog(self):
        return self.store.list('model_catalog')

    def entries(self,user_id):
        return [e for e in self.store.list('ledger') if e['user_id']==user_id]

    def balance(self,user_id):
        entries=self.entries(user_id)
        credits=sum(e['delta'] for e in entries)
        reserved=sum(r['amount'] for r in self.store.list('reservations') if r['user_id']==user_id and r['status']=='held')
        return {'credits':credits,'reserved':reserved,'available':credits-reserved}

    def grant(self,p,actor):
        amount=p.get('credits')
        if isinstance(amount,bool) or not isinstance(amount,int) or not 1<=amount<=100000000:
            raise ValueError('调整额度须为 1–100000000 的整数')
        obj={'id':uid('ledger'),'user_id':required_text(p.get('user_id'),'用户 ID'),'delta':amount,
             'kind':'operator_grant','reason':required_text(p.get('reason'),'调整原因',500),'actor':actor,'created_at':now()}
        self.store.put('ledger',obj)
        return obj

    def reserve(self,user_id,resource_id,units,job_id):
        if isinstance(units,bool) or not isinstance(units,int) or not 1<=units<=1000000:
            raise ValueError('用量上限须为正整数计量单位')
        with self.store.atomic():
            resource=self.store.get('resources',resource_id)
            if resource['status']!='available':
                raise ValueError('资源未上架')
            amount=units*resource['credits_per_unit']
            if self.balance(user_id)['available']<amount:
                raise ValueError('可用额度不足；请先开通额度，任务未提交')
            obj={'id':uid('reservation'),'user_id':user_id,'resource_id':resource_id,'job_id':job_id,
                 'max_units':units,'price':resource['credits_per_unit'],'unit':resource['unit'],'amount':amount,
                 'status':'held','created_at':now()}
            self.store.put('reservations',obj)
            return obj

    def settle(self,p,actor):
        # An operator can reconcile a trusted provider's metering record; the
        # client cannot self-report usage or mint credits.
        with self.store.atomic():
            reservation=self.store.get('reservations',p.get('reservation_id'))
            if reservation['status']!='held':
                raise ValueError('该预留已结算')
            units=p.get('actual_units')
            if isinstance(units,bool) or not isinstance(units,int) or not 0<=units<=reservation['max_units']:
                raise ValueError('实际用量须为预留上限内的整数；超额用量需单独核对')
            reference=required_text(p.get('evidence_ref'),'计量证据引用',1000)
            # Stable ID makes the debit idempotent even if a process stops
            # between writing the ledger entry and updating the reservation.
            entry={'id':'settlement-'+reservation['id'],'user_id':reservation['user_id'],
                   'delta':-units*reservation['price'],'kind':'usage','resource_id':reservation['resource_id'],
                   'job_id':reservation['job_id'],'units':units,'unit':reservation['unit'],
                   'evidence_ref':reference,'actor':actor,'created_at':now()}
            previous=next((e for e in self.store.list('ledger') if e['id']==entry['id']),None)
            if previous and (previous['units']!=units or previous['evidence_ref']!=reference):
                raise ValueError('存在不同的已记录结算，请核对账本')
            if not previous:
                self.store.put('ledger',entry)
            reservation.update(status='settled',actual_units=units,evidence_ref=reference,settled_at=now())
            self.store.put('reservations',reservation)
            return reservation

    def purchase_request(self,user_id,p):
        credits=p.get('credits')
        if isinstance(credits,bool) or not isinstance(credits,int) or not 1<=credits<=100000000:
            raise ValueError('额度申请须为正整数')
        # No fabricated checkout or payment-success state.
        obj={'id':uid('order'),'user_id':user_id,'credits':credits,'status':'awaiting_payment_channel',
             'created_at':now(),'message':'支付渠道与货币定价尚未配置，申请未扣款、未增加额度。'}
        self.store.put('orders',obj)
        return obj

    def account(self,user_id):
        return {'balance':self.balance(user_id),'ledger':self.entries(user_id),
                'reservations':[r for r in self.store.list('reservations') if r['user_id']==user_id],
                'orders':[o for o in self.store.list('orders') if o['user_id']==user_id],
                'payment_status':'not_configured','currency_pricing':None}

    def close(self):
        self.store.close()
