"""Explicit opt-in paid-model acceptance against an inert, synthetic SSH device.

Usage: PYTHONPATH=.:tests python tests/validate_live_discovery.py PRIVATE_CONFIG REPORT
The config contains base_url, model, api_key. Neither config nor credentials are
copied to the report. The fixture never executes remote commands on the host.
"""
import json
from pathlib import Path
import sys
import tempfile
import time
from brainos.service import Platform
from brainos.device_access import PROBES
from ssh_fixture import SSHFixture

def run(config,report_path):
    root='/home/robot/vendor_ws'
    facts={
      'system':'SYNTHETIC QA DEVICE (not a real robot)\nLinux fixture 5.15 aarch64\nUbuntu 22.04\nCPU: 8 ARM cores',
      'memory_storage':'MemTotal: 16384000 kB\n/dev/mmcblk0p1 64G 12G 52G /',
      'accelerators':'NVIDIA Jetson Orin NX 16GB (synthetic fixture)\n/dev/nvhost-gpu',
      'buses_sensors':'USB 8086:0b07 Intel RealSense D435\n/dev/video0\n/dev/ttyUSB0\ncan0 eth0',
      'network':'eth0 UP 192.0.2.10/24\ncan0 UP\nLISTEN 0 128 0.0.0.0:22',
      'services':'robot-stack.service loaded active running Vendor robot service\nsshd.service loaded active running SSH\nrobot_driver',
      'software':'/usr/bin/python3\n/opt/ros/humble/bin/ros2\nPython 3.10.12\nros-humble-ros-base 0.10.0',
      'robotics':'/opt/ros/humble\n/home/robot/vendor_ws (vendor workspace)\n/etc/robot\ncan_raw 20480 0',
      'containers':'No containers configured in this synthetic device',
    }
    outputs={PROBES[k][1]:v.encode() for k,v in facts.items()}
    service='ExecStart=/usr/bin/python3 /home/robot/vendor_ws/driver.py\nFragmentPath=/etc/systemd/system/robot-stack.service\n'
    for cmd in ['systemctl show robot-stack --property=ExecStart --no-pager','systemctl cat robot-stack.service','systemctl show robot-stack.service','systemctl show robot-stack.service --property=ExecStart --no-pager']:
        outputs[cmd]=service.encode()
    files={root+'/README.md':b'Synthetic QA device. Read config/bus.yaml for the transport and driver.py for the firmware handshake. Do not move hardware.',
           root+'/driver.py':b'# Synthetic read-only acceptance fixture\nFIRMWARE_HANDSHAKE = "NX_CAN_V3"\nCONFIG = "config/bus.yaml"\n',
           root+'/config/bus.yaml':b'bus: can0\nprotocol: vendor_can_v3\nbitrate: 500000\nactuator_ids: [11, 12, 13, 14]\n',
           '/etc/robot/sensors.yaml':b'camera: RealSense D435\nserial_port: /dev/ttyUSB0\ncalibration: unavailable\n',
           '/opt/ros/humble/README.txt':b'ROS 2 Humble synthetic fixture'}
    with tempfile.TemporaryDirectory(prefix='neuxmind-live-qa-') as tmp:
        p=Platform(tmp);ssh=SSHFixture(files=files,command_outputs=outputs)
        try:
            pid=p.add_project({'name':'Synthetic model acceptance','objective':'Discover existing configuration without controlling hardware'})['id']
            e=p.engineering
            scan=e.scan({'project_id':pid,'host':'127.0.0.1','port':ssh.port,'username':'robot'})
            conn=e.connect({'project_id':pid,'host_check_id':scan['id'],'trust_fingerprint':scan['fingerprint'],
                'auth':'password','password':'fixture-password','name':'Synthetic vendor robot','read_roots':['/opt/ros','/etc/robot']})
            model=e.model_add({'project_id':pid,'name':'Live model acceptance',**config})
            s=e.start({'robot_id':conn['robot_id'],'model_id':model['id'],'data_authorized':True,'max_tokens':200000,'max_minutes':6,
                'objective':'这是隔离的合成设备验收。请调查可见软硬件，沿厂商工作空间追查驱动使用的协议、bitrate、actuator_ids 与 FIRMWARE_HANDSHAKE，读取真实文件作为证据。不要假设存在实物或执行动作。保存档案，清楚说明哪些未验证。'})
            sid=s['id'];approvals=[];last=None
            while True:
                s=p.store.get('agent_sessions',sid)
                marker=(s['status'],len(s.get('events',[])))
                if marker!=last:
                    print(json.dumps({'status':s['status'],'events':marker[1],'tokens':s.get('usage',{}).get('totalTokens',0)},ensure_ascii=False),flush=True);last=marker
                if s.get('pending_approval'):
                    a=s['pending_approval'];accept=a['method'] in ('device_exec/requestApproval','device_request_scope/requestApproval')
                    # The SSH server only returns static fixture bytes. It does
                    # not execute these commands, irrespective of their content.
                    approvals.append({'method':a['method'],'accepted':accept,'request':a['request']})
                    e.approve({'session_id':sid,'approval_id':a['id'],'decision':'accept' if accept else 'decline'})
                if s['status'] not in ('starting','running','waiting_input','cancelling'):break
                time.sleep(.2)
            report=p.store.get('robot_inventories',s['inventory_id']) if s.get('inventory_id') else None
            evidence=p.store.list('device_evidence')
            serialized=json.dumps(report,ensure_ascii=False)
            checks={value:value in serialized for value in ['NX_CAN_V3','vendor_can_v3','500000']}
            result={'model':config['model'],'base_url':config['base_url'],'status':s['status'],'usage':s.get('usage'),
                    'error':s.get('error'),'checks':checks,'inventory':report,'evidence':evidence,'approvals':approvals,
                    'messages':[x['message'] for x in s['events'] if x['kind']=='message'],
                    'scope':'Real selected model and bundled runtime; synthetic SSH/SFTP device, no robot actions.'}
            raw=json.dumps(result,ensure_ascii=False,indent=2)
            if config['api_key'] in raw:raise RuntimeError('Secret found in report; refusing export')
            Path(report_path).write_text(raw)
            print(json.dumps({'status':s['status'],'checks':checks,'report':str(report_path)},ensure_ascii=False),flush=True)
            return s['status']=='completed' and all(checks.values())
        finally:p.close();ssh.close()

if __name__=='__main__':
    config=json.loads(Path(sys.argv[1]).read_text())
    config={k:config[k] for k in ('base_url','model','api_key')}
    sys.exit(0 if run(config,sys.argv[2]) else 1)
