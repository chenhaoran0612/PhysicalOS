"""Helpers of the tests of issue #20 (autostart): issue #19's ``InstallCase`` with a robot stand-in that has
sudo and systemd (``autostart_fixture.AutostartDevice``). Nothing here reaches a real machine."""
import json
import time

from autostart_fixture import AutostartDevice
from brainos import runtime_autostart_device as auto
from runtime_install_support import WAIT_SECONDS, InstallCase

FAST_AUTOSTART = {'autostart': 20, 'autostart_off': 20, 'confirm': 15}


def waiting(view):
    return view['state'] != 'running' or (view.get('autostart') or {}).get('state') == 'waiting'


class AutostartCase(InstallCase):
    sudo = 'password'
    systemd = True

    def setUp(self):
        super().setUp()
        self.installs.budgets.update(FAST_AUTOSTART)

    def robot_device(self, **options):
        options.setdefault('sudo', self.sudo)
        options.setdefault('systemd', self.systemd)
        device = AutostartDevice(real=self.real, **options)
        self.addCleanup(device.close)
        return device

    # --- the person's side ------------------------------------------------------------------

    def until_asked(self, connection, seconds=WAIT_SECONDS):
        """The job's view once it waits for the person (or once it ended without asking)."""
        return self.wait(connection, seconds, until=waiting)

    def answer(self, connection, view, decision='confirm', password=None, **extra):
        request = {'project_id': self.pid, 'robot_id': connection['robot_id'], 'job_id': view['job_id'],
                   'plan': view['autostart']['plan']['id'], 'decision': decision, **extra}
        if password is not None:
            request['sudo_password'] = password
        return self.installs.answer(request)

    def install_with_autostart(self, connection, password=..., **fields):
        """Install with autostart, confirm what it lists (with the robot's sudo password unless told otherwise)."""
        self.start(connection, autostart=True, **fields)
        view = self.until_asked(connection)
        self.assertEqual(view['autostart']['state'], 'waiting', json.dumps(view, ensure_ascii=False))
        self.answer(connection, view, password=self.device.sudo_password if password is ... else password)
        return self.wait(connection)

    def autostart(self, connection):
        return self.installs.autostart({'project_id': self.pid, 'robot_id': connection['robot_id']})

    def autostart_off(self, connection):
        return self.installs.autostart_off({'project_id': self.pid, 'robot_id': connection['robot_id']})

    # --- what the robot and the console have ------------------------------------------------

    def expected_unit(self, port=8790, adapter='sim_quadruped', package=None):
        return auto.render_unit(self.device.user, self.device.local(''), (package or self.package).id, port, adapter)

    def saved(self, connection):
        return self.robot(connection).get('runtime_autostart')

    def assert_autostart_on(self, connection, port=8790, adapter='sim_quadruped'):
        self.assertEqual(self.device.unit(), self.expected_unit(port, adapter))
        self.assertTrue(self.device.enabled())
        self.device.wait_service('active')
        self.assertEqual(self.device.runtime_port(), port)
        self.assertEqual(self.device.read('neuxmind/.autostart').decode().split(), [self.package.id, str(port), adapter])
        record = self.saved(connection)
        self.assertEqual((record['state'], record['unit'], record['port'], record['adapter']), ('on', auto.UNIT_PATH, port, adapter))
        return record

    def assert_autostart_off(self, connection, reason):
        self.assertIsNone(self.device.unit())
        self.assertFalse(self.device.enabled())
        self.assertIsNone(self.device.read('neuxmind/.autostart'))
        record = self.saved(connection)
        self.assertEqual((record['state'], record['reason']), ('off', reason), record)
        self.assertTrue(record['message'])
        return record

    def assert_secret_nowhere(self, secret, *views):
        """The sudo password is in no view, record, log, event or command line; only on sudo's standard input."""
        self.assertTrue(secret)
        texts = [json.dumps(self.platform.snapshot(), ensure_ascii=False, default=str),
                 json.dumps(self.installs.status({}), ensure_ascii=False, default=str),
                 json.dumps(self.store.list('robots'), ensure_ascii=False, default=str),
                 json.dumps(self.store.list('events'), ensure_ascii=False, default=str),
                 json.dumps([run for run in self.device.root_runs], default=str),
                 json.dumps([run for run in self.device.account_runs], default=str),
                 json.dumps(self.device.install_runs + self.device.refused + self.device.early, default=str),
                 *(json.dumps(view, ensure_ascii=False, default=str) for view in views), *self.logs.lines]
        for text in texts:
            self.assertNotIn(secret, text)
        for line in self.device.systemctl_calls():
            self.assertNotIn(secret, line)
        self.assertNotIn(secret, self.device.journal())

    def back_online(self, connection, seconds=30):
        """After a reboot: the console's own connection check reaches the management layer again."""
        deadline = time.monotonic() + seconds
        while True:
            self.platform.connect_robot({'robot_id': connection['robot_id']})
            if self.robot(connection)['status'] == 'connected':
                return self.robot(connection)
            if time.monotonic() > deadline:
                self.fail('the robot did not come back: %s' % self.robot(connection).get('error'))
            time.sleep(0.2)
