"""Isolated UI data; never connects a robot or invokes a paid model."""
import json,tempfile,time
from brainos.server import Application,make_handler
from test_product import Server

def seed(app):
    _,user=app.identity.authenticate({'email':'inventory@example.test','password':'inventory-test-only','name':'本体档案验收'},register=True)
    p=app.workspace(user);project=p.add_project({'name':'通用本体接入验收','objective':'查看多类硬件、软件、证据和档案版本。所有内容均为测试资料。'})
    pid=project['id'];robot=p.add_robot({'project_id':pid,'name':'复合机器人 · 测试资料','family':'composite'})
    rid=robot['id'];connection={'id':'connection-ui','project_id':pid,'robot_id':rid,'host':'fixture.invalid','port':22,'username':'robot','auth':'password','read_roots':['/etc/robot'],'created_at':'2026-11-04T08:00:00Z'}
    p.store.put('device_connections',connection)
    categories=['system','compute','sensors','interfaces','software','services']
    for i in range(1,4):
        sid=f'session-ui-{i}';eid=f'evidence-ui-{i}';stamp=f'2026-11-0{i+1}T08:00:00Z'
        components=[{'category':'system','name':'操作系统','details':'Ubuntu 22.04 · aarch64（测试记录）','evidence_ids':[eid]},
            {'category':'compute','name':'板载计算单元','details':'ARM CPU / 16 GB 内存 / GPU 加速器；具体驱动兼容性待验证。','evidence_ids':[eid]},
            {'category':'sensors','name':'深度相机','details':'USB 深度相机；设备节点 /dev/video0。'+('标定文件尚未找到。' if i<3 else '已发现标定配置；是否生效仍需核对。'),'evidence_ids':[eid]},
            {'category':'interfaces','name':'移动底盘接口','details':'发现 /cmd_vel 话题配置；未执行运动测试。','evidence_ids':[eid]},
            {'category':'software','name':'ROS 2 工作空间','details':'Humble；/opt/ros/humble；厂商包位于 /opt/vendor_ws。','evidence_ids':[eid]},
            {'category':'services','name':'底盘驱动服务','details':'robot-base.service；仅有配置文件证据，尚未验证真实控制。','evidence_ids':[eid]}]
        if i==1:components.append({'category':'models','name':'检测模型记录','details':'旧调查中记录的模型路径，后续未再列出。','evidence_ids':[eid]})
        if i==3:components.append({'category':'configuration','name':'相机标定配置','details':'/etc/robot/camera.yaml；标定时间与坐标系仍需检查。','evidence_ids':[eid]})
        evidence={'id':eid,'project_id':pid,'robot_id':rid,'connection_id':connection['id'],'session_id':sid,'created_at':stamp,'tool':'device_probe','arguments':{'probe':'system'},'result':{'label':'系统与硬件测试记录','stdout':'Ubuntu 22.04 aarch64\n/dev/video0: USB depth camera\nROS_DISTRO=humble\nrobot-base.service\n<test data only>','stderr':'fixture: vendor permissions not verified','exit_code':0,'truncated':i==2}}
        p.store.put('device_evidence',evidence)
        report={'id':f'inventory-ui-{i}','project_id':pid,'robot_id':rid,'session_id':sid,'connection_id':connection['id'],'created_at':stamp,'summary':'已整理计算单元、相机、底盘接口与机器人软件。档案来自隔离测试资料，不代表真实本体。','components':components,'unknowns':['关节执行器型号、数量与控制模式待确认。','设备配置是否与当前运行服务一致，需要继续核对。'],'recommendations':['先检查厂商控制接口与标定，再开发适配器 Harness。'],'unconfirmed_categories':[k for k in ['system','compute','sensors','actuators','interfaces','software','middleware','services','models','configuration'] if not any(c['category']==k for c in components)],'qualification':'discovery_only'}
        p.store.put('robot_inventories',report)
        p.store.put('agent_sessions',{'id':sid,'project_id':pid,'robot_id':rid,'connection_id':connection['id'],'mode':'discovery','status':'completed','model':'isolated-fixture','billing':'provider','name':'本体调查 · 测试资料','created_at':stamp,'events':[],'usage':{},'inventory_id':report['id']})
    robot.update(connection_id=connection['id'],inventory_id='inventory-ui-3');p.store.put('robots',robot)
    return pid,rid
if __name__=='__main__':
    tmp=tempfile.TemporaryDirectory(prefix='neuxmind-inventory-qa-');app=Application(tmp.name);pid,rid=seed(app);server=Server(make_handler(app))
    print(json.dumps({'url':server.url,'project_id':pid,'robot_id':rid}),flush=True)
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:pass
    finally:server.close();app.close();tmp.cleanup()
