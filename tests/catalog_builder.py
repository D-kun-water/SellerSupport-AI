"""Versioned, local local catalog; never rewrites frozen fixtures or old sources."""
from copy import deepcopy
from hashlib import sha256
import json

from app.domain import now, timestamp, DomainError
from app.store import ROOT, encode
from app.catalog_api import normalize, regression

RELEASE = 'CAT-ZH-20261013'
VERSION = '中文资料-20261013'
CATEGORIES = {'charger':'充电器','cable':'线材','stand':'支架','case':'保护壳',
              'hub':'扩展坞','powerbank':'移动电源','storage':'存储设备'}
USES = {'charging':'充电','data':'传输数据','video':'视频输出'}
RESULTS = {'conditional':'满足条件时支持','compatible':'支持','incompatible':'不支持'}
TRANSLATIONS = {
    'Use a verified USB-C to USB-C cable.':'请使用已核对规格的 USB-C 转 USB-C 线材。',
    '30W is the adapter maximum; actual device draw can be lower.':'30W 是充电器的额定上限，设备实际取电功率可能更低。',
    'Use a verified USB-C PD power adapter.':'请使用已核对规格的 USB-C PD 电源适配器。',
    'This cable does not provide video output.':'此线材不支持视频输出。',
    'This USB 2.0 cable has no video capability.':'此 USB 2.0 线材不支持视频传输。',
    'Data rate: 480 Mbps.':'数据传输速率上限为 480 Mbps。',
}
CONNECTORS = {'USB-C to USB-C':'USB-C 转 USB-C','USB-A to USB-C':'USB-A 转 USB-C',
              '2 x USB-C':'双 USB-C 接口'}
ORIGINAL_NOTES = {
    'TEST-C4':'单口使用时最高 45W；双口同时使用时按 25W + 20W 分配，总功率不超过 45W。',
    'TEST-L5':'视频输出需要信号源支持 DP 替代模式，并连接兼容的显示器；接口相同不能证明支持视频。',
    'TEST-S1':'适用屏幕尺寸为 4.7–12.9 英寸，设备重量不超过 800g；支架不提供充电功能。',
    'TEST-K1':'仅适用于 TEST-D1；其他型号的开孔、尺寸和适配情况尚未确认。',
}


def rule(device, use, result, *pairs):
    return dict(device_id=device,use_case=use,result=result,
                conditions=[zh for zh,en in pairs],conditions_en=[en for zh,en in pairs])


def additions():
    """18 authored products. Product limits are dataset settings, not lab results."""
    records=[]
    def add(sku,category,name,power,connector,rate,video,specs,rules):
        records.append(dict(id=sku,sku=sku,shop_id='TEST-SHOP',source_type='local',
            category=category,name=name,region='US',version=VERSION,
            valid_from='2026-10-13T00:00:00+08:00',valid_to=None,power_w=power,
            connector=connector,data_rate_mbps=rate,video_support=video,
            specifications=specs,rules=rules))
    add('TEST-H1','hub','四口 USB-A 数据扩展坞',None,'USB-C 上行接口；四个 USB-A 下行接口',5000,False,
        {'接口数量':'四个 USB-A 接口','供电方式':'由主机供电','功能限制':'不支持视频输出和主机充电'},
        [rule('TEST-D1','data','conditional',
            ('主机 USB-C 接口须支持数据传输；四个接口共享最高 5Gbps 的上行带宽。','The host USB-C port must support data; all four ports share an upstream bandwidth of up to 5 Gbps.'),
            ('不支持视频输出，也不能通过此扩展坞给主机充电。','Video output and host charging are not supported.'))])
    add('TEST-H2','hub','七合一 USB-C 多功能扩展坞',100,'USB-C；USB-A；HDMI；SD 卡槽；存储卡槽',5000,True,
        {'视频规格':'单路 HDMI，最高 4K / 30Hz','供电限制':'PD 输入最高 100W；为主机供电最高 85W','读卡限制':'两种卡槽不能同时读写'},
        [rule('TEST-D1','video','conditional',
            ('主机须支持 DP 替代模式，显示器和 HDMI 线材须支持目标分辨率。','The host must support DP Alt Mode; the display and HDMI cable must support the target resolution.'),
            ('单路 HDMI 输出最高为 4K / 30Hz；不保证所有主机达到此上限。','Single HDMI output is limited to 4K at 30 Hz; this maximum is not guaranteed for every host.')),
         rule('TEST-D1','charging','conditional',
            ('须连接 USB-C PD 电源；PD 输入最高 100W，提供给主机的功率最高 85W。','Connect a USB-C PD power supply; PD input is up to 100W and host power delivery is up to 85W.'),
            ('实际充电功率还取决于电源、线材和主机。','Actual charging power also depends on the power supply, cable and host.'))])
    add('TEST-H3','hub','USB-C 千兆网口扩展器',None,'USB-C 转 RJ45 网口',1000,False,
        {'网络速率':'最高 1000Mbps','系统条件':'须有兼容的网络驱动','功能限制':'不提供无线网络或视频输出'},
        [rule('TEST-D1','data','conditional',
            ('主机须支持 USB 数据连接，并使用兼容的网络驱动。','The host must support USB data connections and use a compatible network driver.'),
            ('有线网络速率最高 1000Mbps；实际速率受路由器、网线和网络服务限制。','Wired networking is limited to 1000 Mbps; actual speed depends on the router, Ethernet cable and network service.'))])
    add('TEST-P1','powerbank','10000mAh 20W 移动电源',20,'USB-C 输入与输出；USB-A 输出',None,False,
        {'电芯容量':'10000mAh / 3.7V，37Wh','额定输出容量':'6000mAh / 5V','充电协议':'USB-C PD','功能限制':'不支持无线充电或边充边放'},
        [rule('TEST-D1','charging','conditional',
            ('请通过 USB-C PD 输出口和兼容线材连接；单口输出最高 20W。','Use the USB-C PD output and a compatible cable; single-port output is up to 20W.'),
            ('电芯容量不等于手机端可用容量，不承诺固定的充电次数。','Cell capacity differs from usable capacity at the phone; no fixed number of charges is guaranteed.'))])
    add('TEST-P2','powerbank','20000mAh 65W 移动电源',65,'双 USB-C 接口；USB-A 输出',None,False,
        {'电芯容量':'20000mAh / 3.7V，74Wh','额定输出容量':'12000mAh / 5V','功率分配':'单口最高 65W；双 USB-C 输出按 45W + 20W 分配','功能限制':'不保证支持所有笔记本电脑'},
        [rule('TEST-D1','charging','conditional',
            ('单个 USB-C 输出口最高 65W；双 USB-C 同时输出按 45W + 20W 分配。','One USB-C output supports up to 65W; simultaneous dual USB-C output is split as 45W plus 20W.'),
            ('须核对设备所需的 PD 电压档位与线材承载能力。','Verify the device-required PD voltage profile and cable power rating.'))])
    add('TEST-P3','powerbank','5000mAh 15W 便携移动电源',15,'USB-C 输入与输出',None,False,
        {'电芯容量':'5000mAh / 3.7V，18.5Wh','额定输出容量':'3000mAh / 5V','输出档位':'5V / 3A','功能限制':'不提供高电压 PD 快充或无线充电'},
        [rule('TEST-D1','charging','conditional',
            ('仅提供 5V / 3A 有线输出，功率最高 15W。','Only wired 5V at 3A output is provided, with power up to 15W.'),
            ('设备须接受 5V USB-C 供电，不承诺高电压快充。','The device must accept 5V USB-C power; higher-voltage fast charging is not promised.'))])
    for sku,capacity,rate,connector in [('TEST-U1','128GB',5000,'USB-A'),('TEST-U2','256GB',5000,'USB-A 与 USB-C 双接口'),('TEST-U3','1TB',10000,'USB-C')]:
        add(sku,'storage',capacity+(' 便携固态硬盘' if sku=='TEST-U3' else ' 闪存盘'),None,connector,rate,False,
            {'标称容量':capacity,'文件系统':'出厂为 exFAT','速率说明':'接口理论上限不等于实际读写速度','数据处理':'格式化会清除文件，操作前须自行备份'},
            [rule('TEST-D1','data','conditional',
                ('主机须支持 USB 数据连接，并识别 exFAT 文件系统。','The host must support USB data connections and recognize the exFAT file system.'),
                (f'标称容量为 {capacity}；可用容量会因文件系统和容量计算方式有所不同。',f'Nominal capacity is {capacity}; available capacity varies with the file system and capacity calculation method.'),
                ('接口速率上限不代表实际读写速度，不承诺固定的传输耗时。','The interface speed limit does not represent actual read or write speed; no fixed transfer time is guaranteed.'))])
    stand_specs=[('TEST-S2','折叠式手机桌面支架','4–7 英寸','300g','不可用于平板电脑'),
                 ('TEST-S3','可调节平板桌面支架','7–13 英寸','900g','仅用于平整桌面'),
                 ('TEST-S4','铝合金笔记本增高支架','11–16 英寸','3kg','散热效果随环境变化，不保证降温幅度')]
    for sku,name,size,mass,limit in stand_specs:
        add(sku,'stand',name,None,None,None,False,{'适用尺寸':size,'最大承重':mass,'使用条件':limit,'功能限制':'不提供充电、数据传输或视频输出'},[])
    for sku,device,name in [('TEST-K2','TEST-D2','手机透明保护壳'),('TEST-K3','TEST-T1','平板折叠保护套'),('TEST-K4','TEST-N1','笔记本收纳内胆包')]:
        add(sku,'case',device+' '+name,None,None,None,False,
            {'适用型号':device,'适配要求':'下单前核对完整型号与尺寸','防护边界':'不承诺防水、抗摔等级或绝对防损','功能限制':'不提供充电或数据传输'},[])
    add('TEST-L6','cable','USB-C 转 HDMI 视频线 / 2m',None,'USB-C 转 HDMI',None,True,
        {'视频规格':'最高 4K / 60Hz','方向限制':'仅支持 USB-C 信号源到 HDMI 显示器','功能限制':'不提供充电或反向视频转换'},
        [rule('TEST-D1','video','conditional',
            ('USB-C 信号源须支持 DP 替代模式；HDMI 显示器须支持目标分辨率。','The USB-C source must support DP Alt Mode; the HDMI display must support the target resolution.'),
            ('仅支持 USB-C 到 HDMI 的单向输出，最高 4K / 60Hz，不支持反向使用或充电。','Output is one-way from USB-C to HDMI, up to 4K at 60 Hz; reverse use and charging are not supported.'))])
    add('TEST-L7','cable','240W 高速 USB-C 数据线 / 1m',240,'USB-C 转 USB-C',20000,True,
        {'线长':'1m','充电条件':'须配合支持相应 PD 档位的电源和设备','视频条件':'须核对信号源和显示器协议'},
        [rule('TEST-D1','data','conditional',
            ('设备和主机均须支持相应 USB 数据协议；接口传输速率上限为 20Gbps。','Both devices must support the relevant USB data protocol; the interface data rate is limited to 20 Gbps.'),
            ('实际速率取决于连接设备，不能由线材额定功率推断传输速率。','Actual speed depends on the connected devices; cable power rating does not determine data speed.'))])
    add('TEST-L8','cable','USB-A 转 USB-C 数据线 / 2m',15,'USB-A 转 USB-C',480,False,
        {'线长':'2m','输出档位':'最高 5V / 3A','功能限制':'不支持 USB-C PD 高电压快充或视频输出'},
        [rule('TEST-D1','charging','conditional',
            ('请连接规格匹配的 USB-A 电源；输出上限为 5V / 3A，即 15W。','Connect a compatible USB-A power supply; output is limited to 5V at 3A, or 15W.'),
            ('不支持 USB-C PD 高电压快充，也不支持视频输出。','Higher-voltage USB-C PD fast charging and video output are not supported.'))])
    return records


def chinese_rule_header(r):
    return f"适配规则：{r['device_id']} / {USES[r['use_case']]}：{RESULTS[r['result']]}。"


def product_text(p):
    lines=[f"商品：{p['name']}。",f"商品编码：{p['sku']}；适用市场：{'美国' if p['region']=='US' else '欧洲'}。",
           f"商品类别：{CATEGORIES[p['category']]}。"]
    if p.get('power_w') is not None:lines.append(f"额定功率上限：{p['power_w']} W，实际功率取决于连接设备和使用条件。")
    if p.get('connector'):lines.append('接口：'+p['connector']+'。')
    if p.get('data_rate_mbps') is not None:lines.append(f"接口数据速率上限：{p['data_rate_mbps']} Mbps，不等于实际读写速度。")
    if p.get('video_support') is not None:lines.append('视频输出：'+('支持，须核对信号源与显示器条件。' if p['video_support'] else '不支持。'))
    for k,v in p.get('specifications',{}).items():lines.append(k+'：'+v+'。')
    if p['sku'] in ORIGINAL_NOTES:lines.append(ORIGINAL_NOTES[p['sku']])
    for r in p['rules']:lines.append(chinese_rule_header(r)+' '+' '.join(r['conditions']))
    if not p['rules']:lines.append('未列出准确设备与用途的适配规则；仅依据外形、接口或相似型号不能确认兼容。')
    lines.append('出现故障请联系人工客服；当前资料未授权拆机、重复试充或通用重置步骤。')
    return '\n'.join(lines)


def policy_text(p):
    market='美国' if p['region']=='US' else '欧洲'
    if p['kind']=='returns':
        return (f'{market}店铺直销渠道退货政策。\n'
                f"退货申请时间窗口为确认签收后 {p['window_days']} × 24 小时，恰好达到窗口上限仍在期限内。\n"
                '须核对可靠的签收时间和带时区的申请时间，统一按协调世界时计算，不按自然日取整。\n'
                '满足时间窗口仅代表时间条件符合；商品状态、原订单凭据和剩余可退款金额仍需人工审核。\n'
                '退款申请由主管审批，批准与执行分别记录；没有执行凭据不得表示已退款。\n'
                '未记录实际操作前，不承诺已提供退货标签、已联系承运商或已安排调查。\n'
                '出现焦味、冒烟或异常发热时，停止普通排查并交人工安全处理，不要求客户继续充电或拆机。')
    if p['kind']=='reships':
        return (f'{market}店铺直销渠道补发政策。\n'
                '仅可按原订单行补发原商品编码；已补发数量及待批、已批、结果未知的预留数量合计不得超过原订单行数量。\n'
                '已取消或已全额退款的订单需要人工例外审核，不直接补发。\n'
                '客服提交补发申请，由主管审批；批准不代表已经发出。\n'
                '仅在记录实际执行及发运凭据后确认执行状态；执行记录不代表商品已经送达。\n'
                '执行结果未知时保留数量预留，先对账，再决定是否重试，避免重复补发。')
    raise DomainError('VALIDATION_ERROR','未知政策类型，不能自动替换来源')


def localized_product(p):
    p=deepcopy(p)
    p['connector']=CONNECTORS.get(p.get('connector'),p.get('connector'))
    p['name']=p['name'].replace(' / US',' / 美规').replace(' / EU',' / 欧规')
    p['specifications']=p.get('specifications',{})
    for r in p['rules']:
        if r.get('conditions_en'):continue
        try:translated=[TRANSLATIONS[c] for c in r['conditions']]
        except KeyError as error:raise DomainError('VALIDATION_ERROR','未登记的适配条件需要先补齐中文翻译：'+str(error))
        r['conditions_en']=list(r['conditions']);r['conditions']=translated
    return p


def install_catalog(store):
    """Atomic and idempotent release; retain historical rows and immutable runs."""
    actor=dict(id='local-catalog-upgrade',role='supervisor')
    with store.transaction() as db:
        cfg=store.all(db,'merchant_settings')
        if cfg and cfg[0].get('shop_id')!='TEST-SHOP':return dict(applied=False,reason='其他商家不导入默认商品')
        if any(x['id']==RELEASE for x in store.all(db,'catalog_releases')):return dict(applied=False,reason='此版本已安装')
        effective=now()
        candidates=[]
        for kind in ('products','policies'):
            for old in store.all(db,kind):
                if old['shop_id']!='TEST-SHOP' or old.get('review_status')!='published':continue
                if old.get('valid_to') and timestamp(old['valid_to'])<=timestamp(effective):continue
                if timestamp(old['valid_from'])>timestamp(effective):continue
                p=localized_product(old) if kind=='products' else deepcopy(old)
                p.update(version=VERSION,valid_from=effective)
                candidates.append((kind,old,p))
        existing={p['id'] for p in store.all(db,'products')}
        for p in additions():
            if p['id'] in existing:raise DomainError('VERSION_CONFLICT','新增商品编码已存在：'+p['id'])
            p['valid_from']=effective;candidates.append(('products',None,p))
        if not any(kind=='products' and old for kind,old,p in candidates):raise DomainError('VALIDATION_ERROR','需要先初始化默认商家资料')
        checks=[]
        for kind,old,p in candidates:
            # Normalize known executable fields; historical immutable data is preserved separately.
            allowed={'id','sku','shop_id','source_type','category','name','region','version','valid_from','valid_to','power_w','connector','data_rate_mbps','video_support','rules','specifications'} if kind=='products' else {'id','shop_id','source_type','region','channel','kind','version','valid_from','valid_to','window_days','timezone','original_sku_only','quantity_limit'}
            obj=normalize(kind,{k:v for k,v in p.items() if k in allowed})
            source_id='SRC-ZH-'+p['id']+'-20261013'
            text=product_text(obj) if kind=='products' else policy_text(obj)
            row=dict(id=RELEASE+'-'+p['id'],kind=kind,object=obj,source_facts=deepcopy(obj),
                     source_text=text,source_id=source_id)
            results,_,published=regression(store,db,row)
            if not all(c['passed'] for c in results):raise DomainError('INVALID_STATE','中文商品与政策回归失败：'+p['id'])
            checks.append(dict(object_id=p['id'],checks=results))
            if kind=='products':published['validation']='generated_local_regression_pending_human'
            if old:
                history=deepcopy(old);history['id']='HIST-ZH-'+old['id']+'-20261013';history['valid_to']=effective
                store.put(db,kind,history['id'],history)
            source=dict(id=source_id,version=VERSION,source_type='local',status='published',
                text=text,checksum=sha256(text.encode()).hexdigest(),captured_at=effective,
                locator='output/test-catalog/manuals/'+p['id']+'.md',
                origin='项目自建商品与政策资料；参数为数据集设定，非品牌检测结果',
                review_status='machine_checked_pending_human',structured_facts=deepcopy(obj))
            store.put(db,'sources',source_id,source);store.put(db,kind,p['id'],published)
        release=dict(id=RELEASE,state='published',version=1,source_type='local',
            review_mode='generated_local_regression',human_review='pending',
            reason='中文商品资料、售后政策与多品类商品扩充',published_at=effective,
            objects=[p['id'] for _,_,p in candidates],checks=checks,passed=True)
        store.put(db,'catalog_releases',RELEASE,release)
        store.audit(db,actor,'chinese_catalog_installed',RELEASE,dict(products=sum(k=='products' for k,_,p in candidates),policies=sum(k=='policies' for k,_,p in candidates),review_mode=release['review_mode']))
    return dict(applied=True,release_id=RELEASE,products=sum(k=='products' for k,_,p in candidates),policies=sum(k=='policies' for k,_,p in candidates),effective_at=effective)


def export_catalog(store):
    folder=ROOT/'output/test-catalog'
    folder.mkdir(parents=True,exist_ok=True);(folder/'manuals').mkdir(exist_ok=True)
    with store.transaction() as db:
        at=timestamp(now())
        current=lambda table:[p for p in store.all(db,table) if p.get('review_status')=='published' and timestamp(p['valid_from'])<=at and (not p.get('valid_to') or at<timestamp(p['valid_to']))]
        products=current('products');policies=current('policies')
        ids={x['source_id'] for x in products+policies}
        sources=[x for x in store.all(db,'sources') if x['id'] in ids]
    bundle=dict(dataset_version=VERSION,source_type='local',human_review='pending',products=products,policies=policies,sources=sources)
    (folder/'catalog.json').write_text(json.dumps(bundle,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    by_id={s['id']:s for s in sources}
    for p in products+policies:
        title=p.get('name') or ('退货政策' if p['kind']=='returns' else '补发政策')+' / '+p['region']
        (folder/'manuals'/(p['id']+'.md')).write_text('# '+title+'\n\n'+by_id[p['source_id']]['text']+'\n',encoding='utf-8')
    return dict(products=len(products),policies=len(policies),categories={k:sum(p['category']==k for p in products) for k in CATEGORIES})
