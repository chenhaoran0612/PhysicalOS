"""Shared fixtures for the round-04 "本体适配" template: an isolated inventory
(test data only, no robot is contacted) and a local Responses double that acts
as the engineering assistant for stages 1 and 2 through native commands."""
import json
from pathlib import Path
import shlex
import time

from test_engineering import ResponsesFixture

DOUBLE=Path(__file__).resolve().with_name('runtime_double.py')
# Stage-2 command that breaks the system.health payload. Codex runs it in the
# host shell, so it must work with BSD sed (macOS) as well as GNU sed: BSD
# ``sed -i`` takes the backup suffix as a required argument (``sed -i -e …``
# reads ``-e`` as the suffix and fails before HANDOFF.json is written), and an
# attached suffix is the one spelling both accept. The backup is removed so
# the adapter package stays exactly what the scaffold plus this edit produce.
BREAK_HEALTH=("sed -i.orig -e 's/\"ok\": not errors/\"ok\": \"yes\"/' -e \"s/'ok': True/'ok': 'yes'/\" adapter/*/adapter.py && "
              'rm adapter/*/adapter.py.orig && ')
CATEGORIES=['system','compute','sensors','actuators','interfaces','software','middleware','services','models','configuration']


def seed_inventory(platform,pid,name='移动底盘 · 测试资料'):
    robot=platform.add_robot({'project_id':pid,'name':name,'family':'mobile'})
    rid=robot['id'];store=platform.store
    connection={'id':'connection-adapt-'+rid[-6:],'project_id':pid,'robot_id':rid,'host':'fixture.invalid','port':22,'username':'robot','auth':'password','read_roots':['/etc/robot'],'created_at':'2026-11-08T08:00:00Z'}
    store.put('device_connections',connection)
    ids={'ros':'evidence-adapt-ros-'+rid[-6:],'camera':'evidence-adapt-cam-'+rid[-6:]}
    texts={'ros':'ROS_DISTRO=humble\n/cmd_vel geometry_msgs/msg/Twist\n/diagnostics diagnostic_msgs/msg/DiagnosticArray\n<test data only>',
           'camera':'/camera/depth/image_rect_raw sensor_msgs/msg/Image 640x480\n<test data only>'}
    for key,eid in ids.items():
        store.put('device_evidence',{'id':eid,'project_id':pid,'robot_id':rid,'connection_id':connection['id'],'session_id':'session-adapt','created_at':'2026-11-08T08:00:00Z',
            'tool':'device_probe','arguments':{'probe':'software'},'result':{'label':'ROS 2 话题列表（测试资料）','stdout':texts[key],'stderr':'','exit_code':0}})
    components=[{'category':'middleware','name':'ROS 2 Humble','details':'发现 /cmd_vel（Twist）与 /diagnostics 话题；未执行运动测试。','evidence_ids':[ids['ros']]},
                {'category':'sensors','name':'深度相机','details':'/camera/depth/image_rect_raw 640×480。','evidence_ids':[ids['camera']]},
                {'category':'actuators','name':'差速底盘','details':'通过 /cmd_vel 接受速度指令（仅配置证据）。','evidence_ids':[ids['ros']]}]
    report={'id':'inventory-adapt-'+rid[-6:],'project_id':pid,'robot_id':rid,'session_id':'session-adapt','connection_id':connection['id'],'created_at':'2026-11-08T08:00:00Z',
            'summary':'差速移动底盘，ROS 2 Humble，深度相机。档案来自隔离测试资料，不代表真实本体。','components':components,
            'unknowns':['电池接口与电量话题未发现。','急停是否有独立硬件回路待确认。'],'recommendations':['先按 /cmd_vel 实现底盘速度，再核对电池。'],
            'unconfirmed_categories':[k for k in CATEGORIES if not any(c['category']==k for c in components)],'qualification':'discovery_only'}
    store.put('robot_inventories',report)
    store.put('agent_sessions',{'id':'session-adapt','project_id':pid,'robot_id':rid,'connection_id':connection['id'],'mode':'discovery','status':'completed','model':'isolated-fixture','billing':'provider','name':'本体调查 · 测试资料','created_at':'2026-11-08T08:00:00Z','events':[],'usage':{},'inventory_id':report['id']})
    robot=store.get('robots',rid);robot.update(connection_id=connection['id'],inventory_id=report['id'],onboarding_status='discovered');store.put('robots',robot)
    return rid,report,ids


def mapping_for(report,ids,registry,foreign=False):
    rows=[]
    for cap in registry:
        cid=cap['id'];row={'id':cid,'implementation':'needs_confirmation','interface':'','evidence_ids':[],'notes':'档案中没有证据'}
        if cid=='safety.stop':row.update(implementation='ros2_topic',interface='/cmd_vel 零速度 + 驱动停止服务',evidence_ids=[ids['ros']],params={'hardware_estop':False},notes='')
        if cid=='system.health':row.update(implementation='ros2_topic',interface='/diagnostics',evidence_ids=[ids['ros']],notes='')
        if cid=='base.velocity':row.update(implementation='ros2_topic',interface='/cmd_vel geometry_msgs/Twist',evidence_ids=[ids['ros']],params={'max_vx':0.5,'max_vy':0.0,'max_wz':1.0,'holonomic':False},notes='')
        if cid=='sensor.camera.depth':row.update(implementation='ros2_topic',interface='/camera/depth/image_rect_raw',evidence_ids=[ids['camera']],params={'name':'depth','frame':'camera_depth','range_max':5.0},notes='')
        if cid=='base.odometry':row.update(implementation='ros2_topic',interface='/odom',evidence_ids=[ids['ros']],notes='frame 未在档案中出现')   # → missing param, downgraded
        if cid=='sensor.battery':row.update(implementation='vendor_sdk',interface='猜测的厂商电池接口',evidence_ids=[],notes='没有证据')     # → downgraded
        if cid=='arm.joint_position':row.update(implementation='unsupported',evidence_ids=[ids['ros']],notes='差速底盘，没有机械臂')
        if foreign and cid=='base.odometry':row.update(implementation='ros2_topic',interface='/odom',evidence_ids=['evidence-not-in-inventory'],params={'frame':'odom'})
        rows.append(row)
    return {'schema':'neuxmind.capability-mapping.v1','inventory_id':report['id'],'capabilities':rows}


def handoff(outputs,summary):
    return {'schema':'neuxmind.stage-result.v1','outcome':'ready_for_review','summary':summary,'artifacts':outputs,
            'checks':[{'name':'本阶段命令','status':'passed','command_id':'call_stage_work','detail':'原生命令成功退出'}]}


class AdaptationModel(ResponsesFixture):
    """Stage 1 writes mapping.json/mapping.md; stage 2 checks the scaffolded
    package, runs its tests and writes IMPLEMENTATION.md. Test data only."""
    requests=[];mapping=None;delay=0.0;break_health=False;tamper=False
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));type(self).requests.append(data)
        if type(self).delay:time.sleep(type(self).delay)
        text=json.dumps(data,ensure_ascii=False)
        done=any(x.get('type')=='function_call_output' for x in data.get('input',[]))
        if done:item={'id':'msg_stage','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'阶段交付已生成，请验收。','annotations':[]}]}
        else:
            if '本体适配分析师' in text:
                command=('printf %s '+shlex.quote(json.dumps(type(self).mapping,ensure_ascii=False))+' > mapping.json; '
                         "printf '# 映射说明\\n依据档案证据；电池等无证据项需现场确认。\\n' > mapping.md; "
                         'printf %s '+shlex.quote(json.dumps(handoff(['mapping.json','mapping.md'],'映射表已按档案证据生成；无证据项标为需确认。'),ensure_ascii=False))+' > HANDOFF.json')
            else:
                command=('ls adapter/*/adapter.py adapter/*/adapter.json adapter/README.md && grep -q NotImplementedError adapter/*/adapter.py && '
                         +(BREAK_HEALTH if type(self).break_health else '')+
                         ("printf '\\nfrom neuxmind_runtime import conformance as _c\\n' >> adapter/*/adapter.py && " if type(self).tamper else '')+
                         "printf '# 实现说明\\n替身后端：NEUXMIND_ADAPTER_BACKEND=double。真机路径未连接验证。\\n' > adapter/IMPLEMENTATION.md && "
                         'printf %s '+shlex.quote(json.dumps(handoff(['adapter/README.md','adapter/IMPLEMENTATION.md'],'已基于脚手架补充实现说明；真机路径未验证。'),ensure_ascii=False))+' > HANDOFF.json')
            item={'id':'fc_stage_work','type':'function_call','name':'exec_command','call_id':'call_stage_work','arguments':json.dumps({'cmd':command})}
        response={'id':'resp_adapt','status':'completed','output':[item],'usage':{'input_tokens':20,'output_tokens':10,'total_tokens':30}}
        events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},{'type':'response.output_item.added','output_index':0,'item':item},
                {'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':response}]
        raw=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers()
        try:self.wfile.write(raw)
        except BrokenPipeError:pass
