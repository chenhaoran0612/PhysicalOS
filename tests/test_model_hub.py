"""Issue #71: the model library — models by category with the measures bound to it, model files kept on
this computer, evaluation and training sets, evaluations (run in the simulation for the Microduck's
motion models, imported for the rest) and rounds of improvement.

Nothing here starts a simulation: the evaluation runner is given a made-up duck (``FakeDuck``)."""
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from brainos import duck_kit, model_hub, model_hub_catalog as catalog, model_hub_files
from brainos.errors import CodedError
from brainos.service import Platform

SHOWS = (
    {'id': 'nod', 'title': '点头', 'status': 'delivered', 'summary': '测试用的节目。', 'package': 'nod.zip', 'sha256': 'a' * 64,
     'origin': {'round': 3, 'update': 4, 'steps': 2048, 'device': 'CPU'}, 'acceptance': {'fixed': [10, 10], 'independent': [8, 10]}},
    {'id': 'spin', 'title': '转圈', 'status': 'training', 'summary': '还没有模型包。'},
)


class FakeDuck:
    """Answers ``perform`` and ``status`` the way a simulated duck does: a seed in ``fails`` fails."""

    def __init__(self, fails=(), stop_at=None, die_at=None):
        self.fails, self.stop_at, self.die_at = set(fails), stop_at, die_at
        self.run, self.seeds = None, []

    def perform(self, p):
        self.seeds.append(p['seed'])
        self.run = {'run_id': f'run-{len(self.seeds)}', 'seed': p['seed'], 'show': p['show']}
        return {'run_id': self.run['run_id'], 'robot_id': p['robot_id'], 'show': p['show']}

    def status(self, p):
        seed, n = self.run['seed'], len(self.seeds)
        if self.die_at == n:
            return {'run_id': self.run['run_id'], 'state': 'error', 'running': False, 'result': None, 'error': '进程已经不在了'}
        if self.stop_at == n:
            return {'run_id': self.run['run_id'], 'state': 'stopped', 'running': True, 'result': None, 'error': ''}
        ok = seed not in self.fails
        return {'run_id': self.run['run_id'], 'state': 'succeeded' if ok else 'failed', 'running': True, 'error': '',
                'result': {'success': ok, 'reason': 'complete' if ok else 'fall', 'steps': 200 + seed % 10, 'duration_s': 4.0, 'phases': ['低头']}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (('SHOWS', SHOWS), ('BY_ID', {s['id']: s for s in SHOWS}), ('PACKAGED', SHOWS[:1])):
            patch = mock.patch.object(duck_kit, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(self.app.close)
        self.hub = self.app.model_hub
        self.hub.poll_s = 0
        self.hub.runnable_show = lambda show: show == 'nod'           # the kit "has" the package of nod
        self.app.store.put('robots', {'id': 'robot-1', 'project_id': 'project-robot-home', 'name': '仿真小鸭', 'sim': {'duck': True}})

    def refused(self, code, call, *args):
        with self.assertRaises(CodedError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def model(self, key):
        return next(m for m in self.hub.overview({})['models'] if m['id'] == key)

    def upload(self, data=b'weights', name='policy.pt'):
        files = self.hub.files
        started = files.start({'filename': name, 'size': len(data)})
        for index in range(started['chunk_count']):
            files.chunk(started['upload_id'], index, data[index * started['chunk_bytes']:(index + 1) * started['chunk_bytes']])
        return files.complete({'upload_id': started['upload_id']})

    def own(self, category='navigation', **more):
        return self.hub.add_model({'name': '走廊导航', 'category': category, 'summary': '在走廊里走到指定点。', **more})

    def wait(self, evaluation_id, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            record = self.hub.evaluation({'evaluation_id': evaluation_id})
            if record['status'] != 'running':
                return record
            time.sleep(0.01)
        self.fail('the evaluation did not end')


class Categories(unittest.TestCase):
    def test_every_category_binds_its_measures_and_decides_on_one_of_them(self):
        self.assertEqual([c['id'] for c in catalog.CATEGORIES][:5], ['motion_rl', 'navigation', 'intent', 'llm', 'vlm'])
        for item in catalog.CATEGORIES:
            self.assertEqual(sum(1 for m in item['measures'] if m['primary']), 1, item['id'])
            self.assertEqual(len({m['id'] for m in item['measures']}), len(item['measures']))

    def test_values_are_for_the_categorys_own_measures_and_within_their_range(self):
        self.assertEqual(catalog.clean_results('navigation', {'reach_rate': 96, 'collision_rate': 0}), {'reach_rate': 96.0, 'collision_rate': 0.0})
        for bad in ({'success_rate': 90}, {'reach_rate': 101}, {'reach_rate': 'high'}, {'reach_rate': True}, {'collision_rate': 1}):
            with self.assertRaises(ValueError, msg=bad):
                catalog.clean_results('navigation', bad)

    def test_a_result_is_judged_against_the_targets_and_a_sets_own_target_replaces_the_categorys(self):
        self.assertTrue(catalog.judge('navigation', {'reach_rate': 95, 'collision_rate': 0, 'position_error_m': 0.2})['met'])
        self.assertFalse(catalog.judge('navigation', {'reach_rate': 95, 'collision_rate': 2, 'position_error_m': 0.1})['met'])
        self.assertIsNone(catalog.judge('navigation', {'reach_rate': 99})['met'])                 # two targets have no value yet
        self.assertTrue(catalog.judge('navigation', {'reach_rate': 90, 'collision_rate': 0, 'position_error_m': 0.1}, {'reach_rate': 90})['met'])

    def test_a_comparison_knows_which_way_is_better(self):
        result = catalog.compare('navigation', {'reach_rate': 80, 'collision_rate': 5}, {'reach_rate': 90, 'collision_rate': 7})
        rows = {r['id']: r for r in result['rows']}
        self.assertEqual((result['primary'], rows['reach_rate']['direction'], rows['collision_rate']['direction'], rows['path_efficiency']['direction']),
                         ('better', 'better', 'worse', 'unknown'))


class Library(Case):
    def test_the_ducks_skill_models_are_motion_models_of_its_brand_and_model(self):
        data = self.hub.overview({})
        nod, spin = self.model('hubm-duck-nod'), self.model('hubm-duck-spin')
        self.assertEqual((nod['category'], nod['product'], nod['builtin'], nod['status']),
                         ('motion_rl', {'vendor': 'Microduck', 'model': 'AD-P102'}, True, 'delivered'))
        self.assertEqual([(v['label'], v['runnable'], v['files'][0]['sha256']) for v in nod['versions']], [('第 3 轮 · 第 4 次更新', True, 'a' * 64)])
        self.assertEqual(nod['current_version_id'], nod['versions'][0]['id'])
        self.assertEqual((spin['status'], spin['versions']), ('training', []))
        fixed = {e['set_id']: e for e in nod['evaluations']}
        self.assertEqual((fixed['hubs-duck-fixed']['metrics'], fixed['hubs-duck-fixed']['judgement']['met']), ({'success_rate': 100.0}, True))
        self.assertEqual((fixed['hubs-duck-independent']['metrics'], fixed['hubs-duck-independent']['judgement']['met']), ({'success_rate': 80.0}, False))
        sets = {s['id']: s for s in data['sets']}
        self.assertEqual((len(sets['hubs-duck-fixed']['seeds']), sets['hubs-duck-fixed']['purpose'], sets['hubs-duck-fixed']['builtin']), (10, 'evaluation', True))

    def test_a_model_is_added_in_a_category_and_a_body_model_names_its_brand_and_model(self):
        made = self.own(vendor='NeuxMind', model='CS1')
        self.assertEqual((made['category'], made['product'], made['status'], made['builtin']), ('navigation', {'vendor': 'NeuxMind', 'model': 'CS1'}, 'empty', False))
        self.assertEqual(self.model(made['id'])['name'], '走廊导航')
        with self.assertRaises(ValueError):
            self.hub.add_model({'name': '没有分类', 'category': 'nothing'})
        with self.assertRaises(ValueError):
            self.hub.add_model({'name': '只有品牌', 'category': 'llm', 'vendor': 'NeuxMind'})

    def test_a_model_file_is_uploaded_in_chunks_kept_by_content_and_becomes_a_version(self):
        made, data = self.own(), bytes(range(256)) * 40
        with mock.patch.object(model_hub_files, 'CHUNK_BYTES', 4096):
            file = self.upload(data, '../../evil/走廊 v2.onnx')
        self.assertEqual((file['filename'], file['size'], file['sha256']), ('走廊 v2.onnx', len(data), hashlib.sha256(data).hexdigest()))
        version = self.hub.add_version({'model_id': made['id'], 'label': 'v2', 'file_ids': [file['id']], 'note': '第二次训练'})
        path, name, size = self.hub.files.path(file['id'])
        self.assertEqual((path.read_bytes(), name, size), (data, '走廊 v2.onnx', len(data)))
        self.assertTrue(path.is_relative_to(Path(self.app.store.root)))
        shown = self.model(made['id'])
        self.assertEqual((shown['current_version_id'], shown['status'], shown['versions'][0]['files'][0]['id']), (version['id'], 'unrated', file['id']))

    def test_an_upload_is_refused_when_it_is_out_of_order_too_long_or_not_started(self):
        files = self.hub.files
        started = files.start({'filename': 'a.bin', 'size': 4})
        self.refused('hub_upload_order', files.chunk, started['upload_id'], 1, b'ab')
        self.refused('hub_upload_size', files.chunk, started['upload_id'], 0, b'abcdef')
        self.refused('hub_upload_unknown', files.chunk, 'upload-' + '0' * 32, 0, b'ab')
        self.refused('hub_upload_unknown', files.chunk, '../x', 0, b'ab')
        files.chunk(started['upload_id'], 0, b'ab')
        self.assertEqual(files.chunk(started['upload_id'], 0, b'ab'), {'received_count': 1, 'received_bytes': 2})      # sent again after a lost answer
        self.refused('hub_upload_size', files.complete, {'upload_id': started['upload_id']})          # two bytes are missing
        for bad in ({'filename': 'a.bin', 'size': 0}, {'filename': '', 'size': 3}, {'filename': 'a.bin', 'size': model_hub_files.MAX_FILE_BYTES + 1}):
            with self.assertRaises(ValueError, msg=bad):
                files.start(bad)

    def test_a_version_needs_a_file_that_was_uploaded_and_is_not_used_twice(self):
        made, file = self.own(), self.upload()
        self.refused('hub_file_unknown', self.hub.add_version, {'model_id': made['id'], 'label': 'v1', 'file_ids': ['file-nothing']})
        self.refused('hub_file_unknown', self.hub.add_version, {'model_id': made['id'], 'label': 'v1', 'file_ids': []})
        self.hub.add_version({'model_id': made['id'], 'label': 'v1', 'file_ids': [file['id']]})
        self.refused('hub_file_unknown', self.hub.add_version, {'model_id': made['id'], 'label': 'v2', 'file_ids': [file['id']]})
        self.refused('hub_model_unknown', self.hub.add_version, {'model_id': 'hubm-nothing', 'label': 'v1', 'file_ids': [file['id']]})

    def test_a_model_of_ones_own_is_removed_with_its_files_and_a_built_in_one_is_not(self):
        made, file = self.own(), self.upload()
        self.hub.add_version({'model_id': made['id'], 'label': 'v1', 'file_ids': [file['id']]})
        path = self.hub.files.path(file['id'])[0]
        self.refused('hub_builtin', self.hub.remove_model, {'model_id': 'hubm-duck-nod', 'confirm': True})
        with self.assertRaises(ValueError):
            self.hub.remove_model({'model_id': made['id']})
        self.hub.remove_model({'model_id': made['id'], 'confirm': True})
        self.assertFalse(path.exists())
        self.assertNotIn(made['id'], [m['id'] for m in self.hub.overview({})['models']])

    def test_a_trained_model_is_a_skill_once_it_is_bound_to_one_of_its_brand_and_model(self):
        made = self.own(vendor='NeuxMind', model='CS1')
        self.app.store.put('skills', {'id': 'skill-1', 'project_id': 'project-robot-home', 'name': '走到指定点', 'version': '1.0.0',
                                      'entrypoint': 'nav.goto', 'product': {'vendor': 'NeuxMind', 'model': 'CS1'}})
        self.app.store.put('skills', {'id': 'skill-2', 'project_id': 'project-robot-home', 'name': '别的机器人的技能', 'version': '1.0.0',
                                      'entrypoint': 'x', 'product': {'vendor': 'Other', 'model': 'X1'}})
        self.app.store.put('skills', {'id': 'skill-3', 'project_id': 'project-robot-home', 'name': '点头', 'version': '0.3.0',
                                      'entrypoint': 'duck.nod', 'product': {'vendor': 'Microduck', 'model': 'AD-P102'}})
        self.assertIsNone(self.model(made['id'])['skill'])
        self.refused('hub_skill_other_model', self.hub.bind_skill, {'model_id': made['id'], 'skill_id': 'skill-2'})
        self.hub.bind_skill({'model_id': made['id'], 'skill_id': 'skill-1'})
        self.assertEqual(self.model(made['id'])['skill'], {'id': 'skill-1', 'name': '走到指定点', 'version': '1.0.0', 'auto': False})
        self.hub.bind_skill({'model_id': made['id'], 'skill_id': ''})
        self.assertIsNone(self.model(made['id'])['skill'])
        # A skill model of the duck is the skill made from it (issue #67 registers it under this name).
        self.assertEqual(self.model('hubm-duck-nod')['skill'], {'id': 'skill-3', 'name': '点头', 'version': '0.3.0', 'auto': True})


    def test_a_chunk_whose_write_failed_is_written_again_in_its_place(self):
        files, data = self.hub.files, b'0123456789'
        with mock.patch.object(model_hub_files, 'CHUNK_BYTES', 4):
            started = files.start({'filename': 'a.bin', 'size': len(data)})
            files.chunk(started['upload_id'], 0, data[:4])
            part = files.root / 'uploads' / started['upload_id']
            with open(part, 'ab') as torn:                          # what a write that failed half way leaves behind
                torn.write(b'45')
            files.chunk(started['upload_id'], 1, data[4:8])
            files.chunk(started['upload_id'], 2, data[8:])
            kept = files.complete({'upload_id': started['upload_id']})
        self.assertEqual((files.path(kept['id'])[0].read_bytes(), kept['sha256']), (data, hashlib.sha256(data).hexdigest()))

    def test_uploads_are_limited_in_number_and_one_that_stalled_makes_room(self):
        files = self.hub.files
        ids = [files.start({'filename': f'{n}.bin', 'size': 3})['upload_id'] for n in range(model_hub_files.MAX_UPLOADS)]
        self.refused('hub_upload_busy', files.start, {'filename': 'more.bin', 'size': 3})
        files.uploads[ids[0]]['at'] -= model_hub_files.UPLOAD_IDLE_S + 1
        files.start({'filename': 'more.bin', 'size': 3})
        self.refused('hub_upload_unknown', files.chunk, ids[0], 0, b'abc')
        self.assertFalse((files.root / 'uploads' / ids[0]).exists())
        self.assertEqual(files.cancel({'upload_id': ids[1]}), {'cancelled': True})
        self.assertEqual(files.cancel({'upload_id': ids[1]}), {'cancelled': False})
        with mock.patch.object(model_hub_files.shutil, 'disk_usage', return_value=mock.Mock(free=10)):
            self.refused('hub_upload_space', files.start, {'filename': 'big.bin', 'size': 3})

    def test_the_same_content_is_kept_once_and_goes_with_its_last_file(self):
        one, two = self.upload(b'same bytes', 'a.bin'), self.upload(b'same bytes', 'b.bin')
        path = self.hub.files.path(one['id'])[0]
        self.assertEqual((one['sha256'], path), (two['sha256'], self.hub.files.path(two['id'])[0]))
        self.hub.files.remove([one['id']])
        self.assertEqual(self.hub.files.path(two['id'])[0].read_bytes(), b'same bytes')
        self.hub.files.remove([two['id']])
        self.assertFalse(path.exists())

    def test_what_nobody_took_within_a_day_is_removed_when_the_workspace_opens(self):
        made, files = self.own(), self.hub.files
        taken, fresh, stale = self.upload(b'taken'), self.upload(b'fresh'), self.upload(b'stale')
        self.hub.add_version({'model_id': made['id'], 'label': 'v1', 'file_ids': [taken['id']]})
        for key in (taken['id'], stale['id']):
            self.app.store.put(model_hub_files.KIND, {**self.app.store.get(model_hub_files.KIND, key), 'created_at': '2026-01-01T00:00:00+00:00'})
        left = files.root / 'uploads' / ('upload-' + '0' * 32)
        left.write_bytes(b'half an upload of an earlier run')
        self.assertEqual(files.sweep(self.hub._claimed()), 1)
        self.assertEqual([files.path(taken['id'])[1], files.path(fresh['id'])[1]], ['policy.pt', 'policy.pt'])
        self.refused('hub_file_unknown', files.path, stale['id'])
        self.assertFalse(left.exists())

    def test_an_upload_to_a_built_in_model_becomes_its_current_version(self):
        nod = self.model('hubm-duck-nod')
        added = self.hub.add_version({'model_id': nod['id'], 'label': '自己训练的', 'file_ids': [self.upload()['id']]})
        shown = self.model(nod['id'])
        self.assertEqual((shown['current_version_id'], shown['status'], len(shown['versions'])), (added['id'], 'unrated', 2))

    def test_values_that_are_not_what_they_should_be_are_refused_in_words(self):
        made = self.own()
        for bad in ([['x']], [{}], [1], 'file-1'):
            self.refused('hub_file_unknown', self.hub.add_version, {'model_id': made['id'], 'label': 'v1', 'file_ids': bad})
        with self.assertRaises(ValueError):
            catalog.clean_results('navigation', {'reach_rate': 10 ** 400})


class Sets(Case):
    def test_an_evaluation_set_of_start_poses_and_a_training_set_are_kept_apart(self):
        poses = self.hub.add_set({'purpose': 'evaluation', 'name': '雨天起姿', 'category': 'motion_rl', 'vendor': 'Microduck', 'model': 'AD-P102',
                                  'description': '地面更滑时的二十个起姿。', 'seeds': '5, 6\n7 7', 'targets': {'success_rate': 90}})
        self.assertEqual((poses['seeds'], poses['count'], poses['targets'], poses['runnable']), ([5, 6, 7], 3, {'success_rate': 90.0}, True))
        file = self.upload(b'{"q":1}\n', 'cases.jsonl')
        train = self.hub.add_set({'purpose': 'training', 'name': '走廊轨迹', 'category': 'navigation', 'description': '三栋楼的走廊。',
                                  'count': 1200, 'source': '2026 年 11 月现场采集', 'file_id': file['id']})
        self.assertEqual((train['purpose'], train['count'], train['file']['filename'], train['runnable']), ('training', 1200, 'cases.jsonl', False))
        kept = {s['id']: s['purpose'] for s in self.hub.overview({})['sets']}
        self.assertEqual((kept[poses['id']], kept[train['id']]), ('evaluation', 'training'))

    def test_what_a_set_cannot_be_is_refused_in_words(self):
        counted = {'purpose': 'evaluation', 'name': '集', 'category': 'navigation', 'description': '说明', 'count': 10}
        poses = {'purpose': 'evaluation', 'name': '集', 'category': 'motion_rl', 'description': '说明', 'seeds': '1 2'}
        self.hub.add_set(counted)
        self.hub.add_set(poses)
        for bad in ({**counted, 'purpose': 'other'}, {**counted, 'count': 0}, {**counted, 'targets': {'success_rate': 90}},
                    {**counted, 'seeds': '1 2'},                       # start poses are for motion models
                    {**counted, 'name': ''}, {**counted, 'purpose': 'training', 'targets': {'reach_rate': 90}},
                    {**poses, 'seeds': '1 x'}, {**poses, 'seeds': '-1'}, {**poses, 'seeds': ''}):
            with self.assertRaises(ValueError, msg=bad):
                self.hub.add_set(bad)

    def test_a_set_that_was_used_stays_and_an_unused_one_of_ones_own_can_go(self):
        made = self.own()
        version = self.hub.add_version({'model_id': made['id'], 'label': 'v1', 'file_ids': [self.upload()['id']]})
        used = self.hub.add_set({'purpose': 'evaluation', 'name': '走廊 50 点', 'category': 'navigation', 'description': '五十个目标点。', 'count': 50})
        spare = self.hub.add_set({'purpose': 'evaluation', 'name': '备用', 'category': 'navigation', 'description': '没用过。', 'count': 5})
        self.hub.import_evaluation({'model_id': made['id'], 'version_id': version['id'], 'set_id': used['id'],
                                    'metrics': {'reach_rate': 90}, 'source': '训练机上的评测报告'})
        self.refused('hub_in_use', self.hub.remove_set, {'set_id': used['id']})
        self.refused('hub_builtin', self.hub.remove_set, {'set_id': 'hubs-duck-fixed'})
        self.hub.remove_set({'set_id': spare['id']})
        self.assertNotIn(spare['id'], [s['id'] for s in self.hub.overview({})['sets']])


class Evaluations(Case):
    def test_an_imported_result_is_for_one_version_on_one_set_of_the_same_category(self):
        made = self.own()
        version = self.hub.add_version({'model_id': made['id'], 'label': 'v1', 'file_ids': [self.upload()['id']]})
        nav = self.hub.add_set({'purpose': 'evaluation', 'name': '走廊 50 点', 'category': 'navigation', 'description': '五十个目标点。', 'count': 50})
        base = {'model_id': made['id'], 'version_id': version['id'], 'set_id': nav['id'], 'source': '训练机上的评测报告'}
        done = self.hub.import_evaluation({**base, 'metrics': {'reach_rate': 96, 'collision_rate': 0, 'position_error_m': 0.15}, 'passed': 48, 'total': 50})
        self.assertEqual((done['mode'], done['status'], done['judgement']['met'], done['passed'], done['total']), ('imported', 'done', True, 48, 50))
        self.assertEqual(self.model(made['id'])['status'], 'qualified')
        self.hub.import_evaluation({**base, 'metrics': {'reach_rate': 80, 'collision_rate': 0, 'position_error_m': 0.15}})
        self.assertEqual(self.model(made['id'])['status'], 'below')                       # the newest result of the current version counts
        for bad in ({'metrics': {}}, {'metrics': {'reach_rate': 96}, 'source': ''}, {'metrics': {'reach_rate': 96}, 'passed': 51, 'total': 50}):
            with self.assertRaises(ValueError, msg=bad):
                self.hub.import_evaluation({**base, **bad})
        self.refused('hub_set_unknown', self.hub.import_evaluation, {**base, 'set_id': 'hubs-duck-fixed', 'metrics': {'reach_rate': 96}})
        self.refused('hub_version_unknown', self.hub.import_evaluation, {**base, 'version_id': 'hubv-nothing', 'metrics': {'reach_rate': 96}})

    def test_a_duck_model_is_run_once_per_start_pose_and_the_measures_come_from_the_runs(self):
        duck = FakeDuck(fails={1700003003, 1700003007})
        self.hub.perform, self.hub.status = duck.perform, duck.status
        nod = self.model('hubm-duck-nod')
        started = self.hub.run_evaluation({'model_id': nod['id'], 'version_id': nod['current_version_id'], 'set_id': 'hubs-duck-fixed', 'robot_id': 'robot-1'})
        self.assertEqual((started['mode'], started['total']), ('run', 10))
        done = self.wait(started['id'])
        self.assertEqual(duck.seeds, list(range(1700003000, 1700003010)))
        self.assertEqual((done['status'], done['passed'], done['metrics']['success_rate'], done['metrics']['mean_duration_s']), ('done', 8, 80.0, 4.0))
        self.assertEqual([(c['seed'], c['success'], c['reason']) for c in done['cases'] if not c['success']],
                         [(1700003003, False, 'fall'), (1700003007, False, 'fall')])
        self.assertFalse(done['judgement']['met'])
        listed = next(e for e in self.model(nod['id'])['evaluations'] if e['id'] == started['id'])
        self.assertNotIn('cases', listed)                                                 # the list is light; the runs are asked for apart

    def test_only_a_packaged_duck_model_on_a_set_of_start_poses_is_run(self):
        made = self.own()
        version = self.hub.add_version({'model_id': made['id'], 'label': 'v1', 'file_ids': [self.upload()['id']]})
        nav = self.hub.add_set({'purpose': 'evaluation', 'name': '走廊', 'category': 'navigation', 'description': '点。', 'count': 5})
        nod = self.model('hubm-duck-nod')
        run = {'model_id': nod['id'], 'version_id': nod['current_version_id'], 'set_id': 'hubs-duck-fixed', 'robot_id': 'robot-1'}
        self.refused('hub_eval_not_runnable', self.hub.run_evaluation, {**run, 'model_id': made['id'], 'version_id': version['id'], 'set_id': nav['id']})
        uploaded = self.hub.add_version({'model_id': nod['id'], 'label': '第 4 轮', 'file_ids': [self.upload(b'new')['id']]})
        text = self.refused('hub_eval_not_runnable', self.hub.run_evaluation, {**run, 'version_id': uploaded['id']})
        self.assertIn('导入', text)
        self.app.store.put('robots', {'id': 'robot-2', 'project_id': 'project-robot-home', 'name': '机器狗', 'sim': {'kind': 'quadruped'}})
        for robot in ('', 'robot-nothing', 'robot-2', ['robot-1']):
            self.refused('hub_duck_needed', self.hub.run_evaluation, {**run, 'robot_id': robot})
        self.assertEqual([e for e in self.model(nod['id'])['evaluations'] if e['mode'] == 'run'], [])      # nothing was recorded for them

    def test_one_evaluation_at_a_time_per_duck_and_stopping_ends_it(self):
        gate = threading.Event()
        duck = FakeDuck()
        real = duck.status
        duck.status = lambda p: (gate.wait(5), real(p))[1]
        self.hub.perform, self.hub.status = duck.perform, duck.status
        nod = self.model('hubm-duck-nod')
        run = {'model_id': nod['id'], 'version_id': nod['current_version_id'], 'set_id': 'hubs-duck-fixed', 'robot_id': 'robot-1'}
        first = self.hub.run_evaluation(run)
        self.refused('hub_eval_busy', self.hub.run_evaluation, run)
        self.hub.stop_evaluation({'evaluation_id': first['id']})
        gate.set()
        done = self.wait(first['id'])
        self.assertEqual((done['status'], len(done['cases']) < 10), ('stopped', True))
        self.assertEqual(done['metrics'], {})                                              # a run that was not finished measures nothing

    def test_a_run_that_someone_stops_on_the_duck_or_whose_duck_is_gone_is_not_a_result(self):
        nod = self.model('hubm-duck-nod')
        run = {'model_id': nod['id'], 'version_id': nod['current_version_id'], 'set_id': 'hubs-duck-fixed', 'robot_id': 'robot-1'}
        for duck, status in ((FakeDuck(stop_at=3), 'interrupted'), (FakeDuck(die_at=2), 'error')):
            self.hub.perform, self.hub.status = duck.perform, duck.status
            done = self.wait(self.hub.run_evaluation(run)['id'])
            self.assertEqual((done['status'], done['metrics']), (status, {}))
            self.assertTrue(done['error'])

    def test_a_run_that_was_going_when_the_app_closed_is_shown_as_interrupted(self):
        nod = self.model('hubm-duck-nod')
        self.app.store.put('hub_evaluations', {'id': 'hube-old', 'model_id': nod['id'], 'version_id': nod['current_version_id'], 'set_id': 'hubs-duck-fixed',
                                               'mode': 'run', 'status': 'running', 'metrics': {}, 'cases': [], 'total': 10, 'passed': 0,
                                               'robot_id': 'robot-1', 'created_at': '2026-11-27T00:00:00+00:00'})
        self.assertEqual(self.hub.evaluation({'evaluation_id': 'hube-old'})['status'], 'interrupted')

    def test_the_training_projects_own_evaluation_of_a_version_is_imported_from_its_progress(self):
        nod = self.model('hubm-duck-nod')
        self.app.duck.training = lambda p=None: {'source': 'snapshot', 'tasks': [{'id': 'nod', 'series': [[3, 4, 10, 10, 1.0], [4, 8, 9, 10, 2.0]]}]}
        version = self.hub.add_version({'model_id': nod['id'], 'label': '第 4 轮', 'file_ids': [self.upload()['id']], 'origin_round': 4, 'origin_update': 8})
        done = self.hub.import_training({'model_id': nod['id'], 'version_id': version['id']})
        self.assertEqual((done['set_id'], done['metrics'], done['passed'], done['total'], done['mode']), ('hubs-duck-fixed', {'success_rate': 90.0}, 9, 10, 'imported'))
        self.assertIn('第 4 轮', done['source'])
        other = self.hub.add_version({'model_id': nod['id'], 'label': '第 9 轮', 'file_ids': [self.upload(b'x')['id']], 'origin_round': 9, 'origin_update': 1})
        self.refused('hub_training_missing', self.hub.import_training, {'model_id': nod['id'], 'version_id': other['id']})


class Rounds(Case):
    def setUp(self):
        super().setUp()
        self.made = self.own()
        self.v1 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v1', 'file_ids': [self.upload(b'one')['id']]})
        self.nav = self.hub.add_set({'purpose': 'evaluation', 'name': '走廊 50 点', 'category': 'navigation', 'description': '五十个目标点。', 'count': 50})
        self.data = self.hub.add_set({'purpose': 'training', 'name': '走廊轨迹', 'category': 'navigation', 'description': '轨迹。', 'count': 900})
        self.result = {'model_id': self.made['id'], 'set_id': self.nav['id'], 'source': '训练机上的评测报告'}

    def round(self):
        return self.model(self.made['id'])['rounds'][0]

    def test_a_round_goes_from_the_baseline_to_a_decision(self):
        opened = self.hub.open_round({'model_id': self.made['id'], 'version_id': self.v1['id'], 'set_id': self.nav['id'], 'goal': '到达成功率到 95%'})
        self.assertEqual((opened['number'], opened['status']), (1, 'baseline'))
        self.hub.import_evaluation({**self.result, 'version_id': self.v1['id'], 'round_id': opened['id'],
                                    'metrics': {'reach_rate': 80, 'collision_rate': 4, 'position_error_m': 0.3}})
        self.assertEqual(self.round()['status'], 'diagnose')
        self.hub.diagnose({'round_id': opened['id'], 'weaknesses': '转角处定位漂移', 'plan': '加入转角轨迹重新训练', 'training_set_ids': [self.data['id']]})
        self.assertEqual(self.round()['status'], 'training')
        v2 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'two')['id']], 'round_id': opened['id']})
        self.assertEqual((self.round()['status'], self.round()['candidate_version_id']), ('candidate', v2['id']))
        self.assertEqual(self.model(self.made['id'])['current_version_id'], self.v1['id'])          # a candidate is not the current version yet
        self.hub.import_evaluation({**self.result, 'version_id': v2['id'], 'round_id': opened['id'],
                                    'metrics': {'reach_rate': 96, 'collision_rate': 0, 'position_error_m': 0.15}})
        review = self.round()
        self.assertEqual((review['status'], review['comparison']['primary'], review['candidate_met']), ('review', 'better', True))
        closed = self.hub.close_round({'round_id': opened['id'], 'decision': 'accept', 'note': '达标'})
        self.assertEqual(closed['status'], 'accepted')
        shown = self.model(self.made['id'])
        self.assertEqual((shown['current_version_id'], shown['status']), (v2['id'], 'qualified'))
        again = self.hub.open_round({'model_id': self.made['id'], 'version_id': v2['id'], 'set_id': self.nav['id'], 'goal': '再降到点误差'})
        self.assertEqual((again['number'], again['status']), (2, 'diagnose'))                    # v2 already has a result on this set

    def test_steps_are_taken_in_order_and_one_round_at_a_time(self):
        ask = {'model_id': self.made['id'], 'version_id': self.v1['id'], 'set_id': self.nav['id'], 'goal': '提高到达成功率'}
        opened = self.hub.open_round(ask)
        self.refused('hub_round_open', self.hub.open_round, ask)
        self.refused('hub_round_state', self.hub.diagnose, {'round_id': opened['id'], 'weaknesses': '短板', 'plan': '做法'})
        self.refused('hub_round_state', self.hub.close_round, {'round_id': opened['id'], 'decision': 'accept'})
        self.refused('hub_round_state', self.hub.add_version, {'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'two')['id']], 'round_id': opened['id']})
        other = self.hub.add_set({'purpose': 'evaluation', 'name': '别的集', 'category': 'navigation', 'description': '点。', 'count': 5})
        self.refused('hub_round_state', self.hub.import_evaluation, {**self.result, 'set_id': other['id'], 'version_id': self.v1['id'],
                                                                     'round_id': opened['id'], 'metrics': {'reach_rate': 80}})
        self.refused('hub_round_unknown', self.hub.diagnose, {'round_id': 'hubr-nothing', 'weaknesses': '短板', 'plan': '做法'})
        with self.assertRaises(ValueError):
            self.hub.open_round({**ask, 'goal': ''})

    def test_a_round_can_be_given_up_at_any_step_and_a_rejected_candidate_stays_a_version(self):
        opened = self.hub.open_round({'model_id': self.made['id'], 'version_id': self.v1['id'], 'set_id': self.nav['id'], 'goal': '提高'})
        self.hub.import_evaluation({**self.result, 'version_id': self.v1['id'], 'round_id': opened['id'], 'metrics': {'reach_rate': 80}})
        self.hub.diagnose({'round_id': opened['id'], 'weaknesses': '短板', 'plan': '做法'})
        v2 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'two')['id']], 'round_id': opened['id']})
        self.hub.import_evaluation({**self.result, 'version_id': v2['id'], 'round_id': opened['id'], 'metrics': {'reach_rate': 70}})
        self.assertEqual(self.round()['comparison']['primary'], 'worse')
        closed = self.hub.close_round({'round_id': opened['id'], 'decision': 'reject', 'note': '比原来差'})
        shown = self.model(self.made['id'])
        self.assertEqual((closed['status'], shown['current_version_id'], [v['label'] for v in shown['versions']]), ('rejected', self.v1['id'], ['v2', 'v1']))
        fresh = self.hub.open_round({'model_id': self.made['id'], 'version_id': self.v1['id'], 'set_id': self.nav['id'], 'goal': '再试'})
        self.assertEqual(self.hub.close_round({'round_id': fresh['id'], 'decision': 'reject'})['status'], 'rejected')

    def test_a_candidate_uploaded_by_mistake_is_replaced_and_evaluated_anew(self):
        opened = self.hub.open_round({'model_id': self.made['id'], 'version_id': self.v1['id'], 'set_id': self.nav['id'], 'goal': '提高'})
        self.hub.import_evaluation({**self.result, 'version_id': self.v1['id'], 'round_id': opened['id'], 'metrics': {'reach_rate': 80}})
        self.hub.diagnose({'round_id': opened['id'], 'weaknesses': '短板', 'plan': '做法'})
        wrong = self.hub.add_version({'model_id': self.made['id'], 'label': '传错的', 'file_ids': [self.upload(b'wrong')['id']], 'round_id': opened['id']})
        self.hub.import_evaluation({**self.result, 'version_id': wrong['id'], 'round_id': opened['id'], 'metrics': {'reach_rate': 10}})
        self.assertEqual(self.round()['status'], 'review')
        right = self.hub.add_version({'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'right')['id']], 'round_id': opened['id']})
        again = self.round()
        self.assertEqual((again['status'], again['candidate_version_id'], again['candidate_evaluation_id'], again['comparison']), ('candidate', right['id'], '', None))
        self.refused('hub_round_state', self.hub.import_evaluation, {**self.result, 'version_id': wrong['id'], 'round_id': opened['id'], 'metrics': {'reach_rate': 99}})
        self.hub.import_evaluation({**self.result, 'version_id': right['id'], 'round_id': opened['id'], 'metrics': {'reach_rate': 97}})
        self.assertEqual((self.round()['status'], self.round()['comparison']['primary']), ('review', 'better'))

    def test_the_result_of_a_step_can_be_measured_again_before_the_decision(self):
        opened = self.hub.open_round({'model_id': self.made['id'], 'version_id': self.v1['id'], 'set_id': self.nav['id'], 'goal': '提高'})
        self.hub.import_evaluation({**self.result, 'version_id': self.v1['id'], 'round_id': opened['id'], 'metrics': {'reach_rate': 80}})
        second = self.hub.import_evaluation({**self.result, 'version_id': self.v1['id'], 'round_id': opened['id'], 'metrics': {'reach_rate': 82}})
        self.assertEqual((self.round()['status'], self.round()['base_evaluation_id']), ('diagnose', second['id']))

    def test_a_model_with_a_round_going_is_not_removed(self):
        self.hub.open_round({'model_id': self.made['id'], 'version_id': self.v1['id'], 'set_id': self.nav['id'], 'goal': '提高'})
        self.refused('hub_in_use', self.hub.remove_model, {'model_id': self.made['id'], 'confirm': True})


if __name__ == '__main__':
    unittest.main()
