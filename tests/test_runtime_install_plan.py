"""Issue #19: what an install does on a robot, decided from the read-only probe (``runtime_install_plan``)."""
import unittest

from brainos import runtime_install_plan as planning


class Package:
    id, runtime_version = 'runtime-0.4.0-0123456789ab', '0.4.0'


GOOD = {'os': 'Linux', 'arch': 'aarch64', 'python': '3.10', 'venv': 'yes', 'missing': '',
        'df': '/dev/root 59000000 20000000 39000000 34% /', 'root': 'none', 'console': '', 'version': '',
        'running': 'no', 'running_port': '', 'job_install': '- - no', 'job_start': '- - no', 'job_remove': '- - no'}


def plan(port_state='free', port=8790, existing=None, confirm=False, **probe):
    return planning.plan({**GOOD, **probe}, port_state, Package, port, existing, confirm)


class PlanTests(unittest.TestCase):
    def test_a_fresh_robot_is_installed_started_and_linked(self):
        for root in ('none', 'empty'):
            decision = plan(root=root)
            self.assertEqual((decision.kind, decision.steps, decision.port), ('steps', planning.FRESH, 8790))
            self.assertTrue(decision.creates_install)

    def test_what_refuses_before_anything_is_written(self):
        cases = {
            'install_forwarding_off': dict(port_state='forbidden'),
            'install_port_busy': dict(port_state='busy'),
            'install_target_taken': dict(root='foreign'),
            'install_device_unsupported': dict(os='Darwin'),
            'install_no_space': dict(df='/dev/root 1000000 990000 2048 99% /'),
        }
        for code, fields in cases.items():
            with self.subTest(code=code):
                decision = plan(**fields)
                self.assertEqual((decision.kind, decision.code), ('refuse', code))
        for fields in (dict(python='3.8'), dict(python='none'), dict(venv='no'), dict(missing='flock setsid'), dict(root='link')):
            with self.subTest(fields=fields):
                self.assertEqual(plan(**fields).kind, 'refuse')

    def test_unreadable_free_space_does_not_block(self):
        self.assertEqual(plan(df='').kind, 'steps')

    def test_an_unfinished_install_is_redone_unless_its_job_still_runs(self):
        self.assertEqual(plan(root='unfinished', job_install='0123456789abcdef - no').steps, planning.FRESH)   # killed
        self.assertEqual(plan(root='unfinished', job_install='0123456789abcdef 0 no').kind, 'choice')      # ended: not ours
        elsewhere = plan(root='unfinished')                   # not started by this package: someone else's, maybe running
        self.assertEqual((elsewhere.kind, elsewhere.options), ('choice', ('replace',)))
        self.assertEqual(plan(root='unfinished', existing='use').code, 'install_choice_invalid')
        self.assertEqual(plan(root='unfinished', existing='replace').code, 'install_replace_unconfirmed')
        self.assertEqual(plan(root='unfinished', existing='replace', confirm=True).steps, planning.FRESH)
        decision = plan(root='unfinished', job_install='0123456789abcdef - yes')
        self.assertEqual(decision.steps, ('wait_install', 'start', 'link'))
        self.assertFalse(decision.creates_install)

    def test_our_own_install_is_picked_up_without_a_question(self):
        running = plan(root='installed', console=Package.id, running='yes', running_port='8799', port_state='busy')
        self.assertEqual((running.steps, running.port), (('link',), 8799))
        stopped = plan(root='installed', console=Package.id)
        self.assertEqual(stopped.steps, ('upload', 'start', 'link'))
        self.assertEqual(plan(root='installed', console=Package.id, port_state='busy').code, 'install_port_busy')

    def test_another_install_waits_for_a_choice_and_offers_only_what_fits(self):
        def options(**fields):
            decision = plan(root='installed', version='0.4.0', **fields)
            self.assertEqual(decision.kind, 'choice')
            return list(decision.options)
        self.assertEqual(options(running='yes', running_port='8790', port_state='busy'), ['use', 'replace'])
        self.assertEqual(options(), ['start', 'replace'])
        self.assertEqual(plan(root='installed', version='0.3.9').options, ('replace',))
        self.assertEqual(plan(root='installed', console='runtime-0.3.9-aaaaaaaaaaaa', version='0.4.0').options, ('start', 'replace'))

    def test_the_choices_once_made(self):
        use = plan(root='installed', running='yes', running_port='8791', port_state='free', existing='use')
        self.assertEqual((use.steps, use.port), (('link',), 8791))
        self.assertEqual(plan(root='installed', version='0.4.0', existing='start').steps, ('upload', 'start', 'link'))
        self.assertEqual(plan(root='installed', existing='replace').code, 'install_replace_unconfirmed')
        replace = plan(root='installed', running='yes', running_port='8790', port_state='busy', existing='replace', confirm=True)
        self.assertEqual(replace.steps, ('upload', 'remove', 'install', 'start', 'link'))
        self.assertTrue(replace.creates_install)
        self.assertEqual(plan(root='installed', running='yes', running_port='8791', port_state='busy', existing='replace',
                              confirm=True).code, 'install_port_busy')
        self.assertEqual(plan(root='installed', version='0.3.9', existing='start').code, 'install_choice_invalid')
        self.assertEqual(plan(root='installed', existing='use').code, 'install_choice_invalid')

    def test_using_a_running_layer_needs_nothing_of_the_device(self):
        self.assertEqual(plan(root='installed', running='yes', running_port='8790', os='Darwin', venv='no', existing='use').steps, ('link',))

    def test_a_probe_that_was_not_understood_is_a_refusal(self):
        self.assertEqual(plan(root='').code, 'install_probe_failed')

    def test_digits_a_robot_sends_are_plain_ascii(self):
        decision = plan(root='installed', running='yes', running_port='\u00b2', version='0.4.0')
        self.assertEqual(decision.kind, 'choice')
        self.assertIsNone(decision.found['running_port'])
        self.assertIsNone(planning.free_kb('/dev/root 1 1 \u00b2\u00b2 1% /'))
        self.assertIsNone(planning.found_of({'running_port': '99999'})['running_port'])

    def test_job_state_reading(self):
        from brainos import runtime_install_device as dev
        self.assertEqual(dev.job_state('0123456789abcdef 0 no'), ('0123456789abcdef', 0, False))
        self.assertEqual(dev.job_state('- - no'), (None, None, False))
        self.assertEqual(dev.job_state('garbage'), (None, None, False))
        self.assertEqual(dev.job_state('0123456789abcdef \u00b2 no'), ('0123456789abcdef', None, False))
        self.assertEqual(dev.job_state('$(reboot) 0 yes'), (None, 0, True))
        self.assertEqual(dev.parse('banner\nos=Windows\n@@NX-INSTALL:begin@@\nos=Linux\n@@NX-INSTALL:probed@@\n'),
                         ('probed', {'os': 'Linux'}, []))
        self.assertEqual(dev.parse('@@NX-INSTALL:begin@@\njob=a 0 no\n@@NX-INSTALL:polled@@\n| line\n| @@NX-INSTALL:x@@\n'),
                         ('polled', {'job': 'a 0 no'}, ['line', '@@NX-INSTALL:x@@']))
        with self.assertRaises(ValueError):
            dev.command('probe', 'runtime-0.4.0-x; rm')


if __name__ == '__main__':
    unittest.main()
