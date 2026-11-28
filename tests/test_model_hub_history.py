"""Issue #76: the candidate of a round of improvement does not have to be uploaded. It can be chosen from
the model's history: a version the model already has (an earlier upload, a candidate an earlier round did
not take, the package that came with the app), or, for the Microduck's skill models, a model file the
training project kept for an evaluated update, fetched from its dashboard on this computer.

The dashboard is made up here (``FakeDashboard``, on a loopback port); nothing is simulated."""
import gzip
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

from brainos import model_hub, model_hub_history
from test_model_hub import Case

ROOT = Path(__file__).resolve().parents[1]
FIXED = [{'seed': s} for s in model_hub.FIXED_SEEDS]


def checkpoint(show, round_number, update, content):
    return {'id': f'round{round_number}:{update}', 'round_number': round_number, 'iteration': update, 'total_steps': 2048 * update,
            'training_backend': 'cuda', 'seeds': FIXED, 'report_mtime': 1795760000.0 + round_number * 1000 + update,
            'checkpoint_url': f'/files/round{round_number}/{show}/checkpoints/update_{update:06d}.pt?v={hashlib.sha256(content).hexdigest()}'}


class FakeDashboard:
    """The training dashboard's two answers the app asks for: ``/api/evolution`` and a checkpoint file."""

    def __init__(self):
        self.files, self.events, self.asked, self.served = {}, {}, [], {}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.asked.append(self.path)
                if self.path == '/api/evolution':
                    body = json.dumps(owner.answer()).encode()
                    if 'gzip' in self.headers.get('Accept-Encoding', ''):
                        body, extra = gzip.compress(body), {'Content-Encoding': 'gzip'}
                    else:
                        extra = {}
                    return self.reply(200, body, extra)
                if self.path in owner.served:
                    return self.reply(200, owner.served[self.path])
                return self.reply(404, b'')

            def reply(self, status, body, extra=None):
                self.send_response(status)
                for key, value in {'Content-Length': str(len(body)), **(extra or {})}.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class Server(http.server.ThreadingHTTPServer):
            def server_bind(self):                      # no reverse lookup of 127.0.0.1 (slow on some Macs)
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = '127.0.0.1', self.server_address[1]

        self.server = Server(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, show, round_number, update, passed, content, **more):
        event = {**checkpoint(show, round_number, update, content), 'successes': passed, 'episodes': 10, 'duration': {'successes': 0, 'episodes': 10}, **more}
        self.events.setdefault(show, []).append(event)
        self.served[event['checkpoint_url']] = content
        return event

    def answer(self):
        return {'required_successes': 10, 'seeds': list(model_hub.FIXED_SEEDS), 'rounds': [{'big': 'x' * 100}],
                'tasks': [{'id': show, 'title': show, 'events': events, 'preview': {'url': '/x'}} for show, events in self.events.items()]}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class History(Case):
    def setUp(self):
        super().setUp()
        self.board = FakeDashboard()
        self.addCleanup(self.board.close)
        env = mock.patch.dict(os.environ, {'NEUXMIND_DUCK_DASHBOARD': self.board.url})
        env.start()
        self.addCleanup(env.stop)
        self.made = self.own()
        self.v1 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v1', 'file_ids': [self.upload(b'one')['id']]})
        self.nav = self.hub.add_set({'purpose': 'evaluation', 'name': '走廊 50 点', 'category': 'navigation', 'description': '五十个目标点。', 'count': 50})
        self.result = {'model_id': self.made['id'], 'set_id': self.nav['id'], 'source': '训练机上的评测报告'}

    def round_at_training(self, model_id=None, version_id=None, set_id=None, metrics=None):
        """A round that has its baseline and its plan: it waits for a candidate."""
        model_id, version_id, set_id = model_id or self.made['id'], version_id or self.v1['id'], set_id or self.nav['id']
        opened = self.hub.open_round({'model_id': model_id, 'version_id': version_id, 'set_id': set_id, 'goal': '提高'})
        if opened['status'] == 'baseline':
            self.hub.import_evaluation({'model_id': model_id, 'set_id': set_id, 'source': '报告', 'version_id': version_id,
                                        'round_id': opened['id'], 'metrics': metrics or {'reach_rate': 80}})
        self.hub.diagnose({'round_id': opened['id'], 'weaknesses': '短板', 'plan': '做法'})
        return opened['id']

    def round(self, model_id=None):
        return self.model(model_id or self.made['id'])['rounds'][0]


class EarlierVersions(History):
    def test_the_models_other_versions_are_offered_with_what_they_scored_on_the_rounds_set(self):
        v2 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'two')['id']]})
        self.hub.import_evaluation({**self.result, 'version_id': v2['id'], 'metrics': {'reach_rate': 91}})
        round_id = self.round_at_training(version_id=self.v1['id'])

        offered = self.hub.history.choices({'round_id': round_id})

        self.assertEqual([v['label'] for v in offered['versions']], ['v2'])                 # not the round's start
        self.assertEqual(offered['start_version_id'], self.v1['id'])
        self.assertEqual(offered['versions'][0]['evaluation']['metrics'], {'reach_rate': 91.0})
        self.assertEqual(offered['versions'][0]['files'][0]['sha256'], hashlib.sha256(b'two').hexdigest())
        self.assertIsNone(offered['training'])                                                  # a model of one's own has no training project
        self.assertEqual(self.board.asked, [])

    def test_an_earlier_version_becomes_the_candidate_without_an_upload(self):
        v2 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'two')['id']]})
        round_id = self.round_at_training()
        kept = len(self.app.store.list(model_hub_files_kind()))

        chosen = self.hub.history.pick({'round_id': round_id, 'version_id': v2['id']})

        self.assertEqual((chosen['status'], chosen['candidate_version_id'], chosen['candidate_evaluation_id']), ('candidate', v2['id'], ''))
        self.assertEqual(len(self.app.store.list(model_hub_files_kind())), kept)              # nothing was uploaded or copied
        self.assertEqual([v['label'] for v in self.model(self.made['id'])['versions']], ['v2', 'v1'])
        self.hub.import_evaluation({**self.result, 'version_id': v2['id'], 'round_id': round_id, 'metrics': {'reach_rate': 96}})
        self.assertEqual((self.round()['status'], self.round()['comparison']['primary']), ('review', 'better'))

    def test_a_version_already_measured_on_the_rounds_set_goes_straight_to_the_decision(self):
        v2 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'two')['id']]})
        measured = self.hub.import_evaluation({**self.result, 'version_id': v2['id'], 'metrics': {'reach_rate': 97, 'collision_rate': 0, 'position_error_m': 0.1}})
        round_id = self.round_at_training()

        chosen = self.hub.history.pick({'round_id': round_id, 'version_id': v2['id']})

        self.assertEqual((chosen['status'], chosen['candidate_evaluation_id'], chosen['comparison']['primary'], chosen['candidate_met']),
                         ('review', measured['id'], 'better', True))
        accepted = self.hub.close_round({'round_id': round_id, 'decision': 'accept'})
        self.assertEqual((accepted['status'], self.model(self.made['id'])['current_version_id']), ('accepted', v2['id']))

    def test_a_candidate_an_earlier_round_did_not_take_can_be_taken_now(self):
        first = self.round_at_training()
        v2 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'two')['id']], 'round_id': first})
        self.hub.close_round({'round_id': first, 'decision': 'reject'})
        second = self.round_at_training()

        self.assertEqual([v['id'] for v in self.hub.history.choices({'round_id': second})['versions']], [v2['id']])
        self.hub.history.pick({'round_id': second, 'version_id': v2['id']})
        self.hub.import_evaluation({**self.result, 'version_id': v2['id'], 'round_id': second, 'metrics': {'reach_rate': 99}})
        self.hub.close_round({'round_id': second, 'decision': 'accept'})

        shown = self.model(self.made['id'])
        self.assertEqual((shown['current_version_id'], [r['status'] for r in shown['rounds']]), (v2['id'], ['accepted', 'rejected']))

    def test_a_wrong_candidate_is_swapped_for_an_earlier_version_and_measured_anew(self):
        v2 = self.hub.add_version({'model_id': self.made['id'], 'label': 'v2', 'file_ids': [self.upload(b'two')['id']]})
        round_id = self.round_at_training()
        wrong = self.hub.add_version({'model_id': self.made['id'], 'label': '传错的', 'file_ids': [self.upload(b'wrong')['id']], 'round_id': round_id})
        self.hub.import_evaluation({**self.result, 'version_id': wrong['id'], 'round_id': round_id, 'metrics': {'reach_rate': 5}})
        self.assertEqual(self.round()['status'], 'review')

        swapped = self.hub.history.pick({'round_id': round_id, 'version_id': v2['id']})

        self.assertEqual((swapped['status'], swapped['candidate_version_id'], swapped['comparison']), ('candidate', v2['id'], None))
        offered = self.hub.history.choices({'round_id': round_id})
        self.assertEqual({v['label']: v['candidate'] for v in offered['versions']}, {'传错的': False, 'v2': True})

    def test_the_start_another_models_version_and_a_round_not_at_training_are_refused(self):
        other = self.own()
        theirs = self.hub.add_version({'model_id': other['id'], 'label': 'x', 'file_ids': [self.upload(b'theirs')['id']]})
        early = self.hub.open_round({'model_id': self.made['id'], 'version_id': self.v1['id'], 'set_id': self.nav['id'], 'goal': '提高'})
        self.refused('hub_round_state', self.hub.history.pick, {'round_id': early['id'], 'version_id': self.v1['id']})
        self.refused('hub_round_state', self.hub.history.choices, {'round_id': early['id']})
        self.hub.close_round({'round_id': early['id'], 'decision': 'reject'})
        round_id = self.round_at_training()

        self.refused('hub_candidate_start', self.hub.history.pick, {'round_id': round_id, 'version_id': self.v1['id']})
        self.refused('hub_version_unknown', self.hub.history.pick, {'round_id': round_id, 'version_id': theirs['id']})
        self.refused('hub_round_unknown', self.hub.history.pick, {'round_id': 'hubr-nothing', 'version_id': theirs['id']})
        with self.assertRaises(ValueError):
            self.hub.history.pick({'round_id': round_id})
        self.assertEqual(self.round()['status'], 'training')


class TrainingProjectFiles(History):
    def setUp(self):
        super().setUp()
        self.nod = self.model('hubm-duck-nod')
        self.packaged = self.nod['versions'][-1]                       # 第 3 轮 · 第 4 次更新, came with the app

    def duck_round(self):
        return self.round_at_training(self.nod['id'], self.packaged['id'], model_hub.FIXED)

    def test_the_evaluated_files_of_the_models_show_are_listed_newest_first(self):
        self.board.add('nod', 3, 4, 10, b'same weights as the package')
        self.board.add('nod', 5, 2, 7, b'round five')
        self.board.add('nod', 6, 8, 10, b'round six')
        self.board.add('spin', 2, 2, 4, b'another show')
        self.board.events['nod'].append({**checkpoint('nod', 7, 1, b'x'), 'successes': 3, 'episodes': 10,
                                         'checkpoint_url': '/files/../../etc/passwd?v=' + 'a' * 64})       # not a checkpoint of the dashboard
        self.board.events['nod'].append({**checkpoint('spin', 8, 1, b'y'), 'successes': 3, 'episodes': 10, 'round_number': 8})   # another show's file
        round_id = self.duck_round()

        files = self.hub.history.training_files({'round_id': round_id})

        self.assertEqual(files['available'], True)
        self.assertEqual([(f['round'], f['update'], f['passed'], f['total']) for f in files['files']], [(6, 8, 10, 10), (5, 2, 7, 10), (3, 4, 10, 10)])
        self.assertEqual([f['start'] for f in files['files']], [False, False, True])                     # same round and update as the start
        self.assertEqual(files['files'][0]['device'], 'GPU')
        self.assertEqual(files['files'][0]['steps'], 2048 * 8)

    def test_a_file_is_fetched_checked_kept_and_becomes_the_candidate(self):
        self.board.add('nod', 6, 8, 10, b'round six weights')
        round_id = self.duck_round()
        self.app.duck.training = lambda p=None: {'source': 'snapshot', 'tasks': []}          # the copy that came with the kit knows nothing newer

        chosen = self.hub.history.pick({'round_id': round_id, 'training_key': '6-8'})

        version = next(v for v in self.model(self.nod['id'])['versions'] if v['id'] == chosen['candidate_version_id'])
        self.assertEqual((chosen['status'], version['label'], version['origin']['round'], version['origin']['update'], version['round_id']),
                         ('candidate', '第 6 轮 · 第 8 次更新', 6, 8, round_id))
        self.assertEqual(version['files'][0]['sha256'], hashlib.sha256(b'round six weights').hexdigest())
        path, _, _ = self.hub.files.path(version['files'][0]['id'])
        self.assertEqual(path.read_bytes(), b'round six weights')
        imported = self.hub.import_training({'model_id': self.nod['id'], 'version_id': version['id'], 'round_id': round_id})
        self.assertEqual((imported['passed'], imported['total'], imported['set_id']), (10, 10, model_hub.FIXED))
        self.assertEqual((self.round(self.nod['id'])['status'], self.round(self.nod['id'])['comparison']['primary']), ('review', 'same'))

    def test_a_file_whose_content_is_not_what_the_dashboard_recorded_is_not_kept(self):
        event = self.board.add('nod', 6, 8, 10, b'round six weights')
        self.board.served[event['checkpoint_url']] = b'something else'
        round_id = self.duck_round()
        files = len(self.app.store.list(model_hub_files_kind()))

        self.refused('hub_history_mismatch', self.hub.history.pick, {'round_id': round_id, 'training_key': '6-8'})

        self.assertEqual(len(self.app.store.list(model_hub_files_kind())), files)
        self.assertEqual((self.round(self.nod['id'])['status'], len(self.model(self.nod['id'])['versions'])), ('training', 1))

    def test_the_same_file_chosen_in_a_later_round_is_the_version_it_already_is(self):
        self.board.add('nod', 6, 8, 10, b'round six weights')
        first = self.duck_round()
        taken = self.hub.history.pick({'round_id': first, 'training_key': '6-8'})['candidate_version_id']
        self.hub.close_round({'round_id': first, 'decision': 'reject'})
        second = self.duck_round()

        offered = self.hub.history.training_files({'round_id': second})
        again = self.hub.history.pick({'round_id': second, 'training_key': '6-8'})

        self.assertEqual(offered['files'][0]['version_id'], taken)
        self.assertEqual(again['candidate_version_id'], taken)
        self.assertEqual(len(self.model(self.nod['id'])['versions']), 2)
        self.assertEqual(len([p for p in self.board.asked if p.startswith('/files/')]), 1)

    def test_the_start_and_a_file_the_dashboard_does_not_list_are_refused(self):
        self.board.add('nod', 3, 4, 10, b'same weights as the package')
        round_id = self.duck_round()
        self.refused('hub_candidate_start', self.hub.history.pick, {'round_id': round_id, 'training_key': '3-4'})
        self.refused('hub_history_unknown', self.hub.history.pick, {'round_id': round_id, 'training_key': '9-9'})
        self.refused('hub_history_unknown', self.hub.history.pick, {'round_id': round_id, 'training_key': '../x'})
        mine = self.round_at_training()
        self.refused('hub_history_unknown', self.hub.history.pick, {'round_id': mine, 'training_key': '3-4'})

    def test_a_file_fetched_for_a_round_that_moved_on_meanwhile_is_not_kept(self):
        self.board.add('nod', 6, 8, 10, b'round six weights')
        round_id = self.duck_round()
        files = len(self.app.store.list(model_hub_files_kind()))
        fetch = model_hub_history.fetch_file

        def slow(*args):                                    # the person gives the round up while the file travels
            self.hub.close_round({'round_id': round_id, 'decision': 'reject'})
            return fetch(*args)
        with mock.patch.object(model_hub_history, 'fetch_file', slow):
            self.refused('hub_round_state', self.hub.history.pick, {'round_id': round_id, 'training_key': '6-8'})

        self.assertEqual(len(self.app.store.list(model_hub_files_kind())), files)
        self.assertEqual(list((Path(self.app.store.root) / 'model-hub' / 'uploads').glob('adopt-*')), [])
        self.assertEqual(len(self.model(self.nod['id'])['versions']), 1)

    def test_a_file_with_the_content_of_the_start_is_the_start(self):
        content = b'the uploaded start'
        start = self.hub.add_version({'model_id': self.nod['id'], 'label': '自己传的', 'file_ids': [self.upload(content)['id']]})
        self.board.add('nod', 9, 9, 10, content)
        round_id = self.round_at_training(self.nod['id'], start['id'], model_hub.FIXED, {'success_rate': 70})

        offered = self.hub.history.training_files({'round_id': round_id})['files']

        self.assertEqual([(f['key'], f['start'], f['version_id']) for f in offered], [('9-9', True, start['id'])])
        self.refused('hub_candidate_start', self.hub.history.pick, {'round_id': round_id, 'training_key': '9-9'})

    def test_the_dashboard_is_read_once_a_minute_and_again_for_a_file_it_did_not_list(self):
        self.board.add('nod', 6, 8, 10, b'round six weights')
        round_id = self.duck_round()
        self.hub.history.training_files({'round_id': round_id})
        self.hub.history.training_files({'round_id': round_id})
        self.assertEqual(self.board.asked.count('/api/evolution'), 1)
        self.board.add('nod', 6, 9, 10, b'newer')
        self.hub.history.pick({'round_id': round_id, 'training_key': '6-9'})
        self.assertEqual(self.board.asked.count('/api/evolution'), 2)

    def test_without_the_dashboard_the_list_says_why_and_nothing_is_chosen(self):
        round_id = self.duck_round()
        self.board.close()

        files = self.hub.history.training_files({'round_id': round_id})

        self.assertEqual((files['available'], files['files']), (False, []))
        self.assertIn(self.board.url, files['problem'])
        self.refused('hub_history_unavailable', self.hub.history.pick, {'round_id': round_id, 'training_key': '6-8'})
        with mock.patch.dict(os.environ, {'NEUXMIND_DUCK_DASHBOARD': 'http://192.0.2.5:8767'}):
            elsewhere = self.hub.history.training_files({'round_id': round_id})
        self.assertEqual(elsewhere['available'], False)                                 # only a dashboard on this computer is asked

    def test_what_the_dashboard_sends_is_read_slim_and_bounded(self):
        board = {'seeds': list(model_hub.FIXED_SEEDS), 'tasks': [{'id': 'nod', 'events': [
            {**checkpoint('nod', 2, 2, b'a'), 'successes': 11, 'episodes': 10},                   # more passes than runs
            {**checkpoint('nod', 2, 3, b'b'), 'successes': 4, 'episodes': 10, 'iteration': 4},   # the file is not of this update
            {**checkpoint('nod', 2, 5, b'c'), 'successes': 4, 'episodes': 10, 'seeds': [{'seed': 1}]},
            {**checkpoint('nod', 2, 6, b'd'), 'successes': True, 'episodes': 10}]}]}
        found = model_hub_history.entries(board)['nod']
        self.assertEqual([(f['update'], f['fixed']) for f in found], [(5, False)])
        self.assertEqual(model_hub_history.entries('not the data'), {})
        with self.assertRaises(ValueError):
            model_hub_history.gunzip(gzip.compress(b'x' * 2048), 1024)

    def test_a_file_from_another_set_of_start_poses_brings_no_result_with_it(self):
        self.board.add('nod', 6, 8, 10, b'round six weights', seeds=[{'seed': 1}])
        round_id = self.duck_round()
        self.app.duck.training = lambda p=None: {'source': 'snapshot', 'tasks': []}
        version_id = self.hub.history.pick({'round_id': round_id, 'training_key': '6-8'})['candidate_version_id']
        self.refused('hub_training_missing', self.hub.import_training, {'model_id': self.nod['id'], 'version_id': version_id, 'round_id': round_id})


class Pages(unittest.TestCase):
    def test_the_console_serves_the_script_after_the_model_library(self):
        html = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
        self.assertLess(html.index('/model_hub_pages.js'), html.index('/model_hub_history.js'))
        from brainos import server
        self.assertIn("'/model_hub_history.js'", Path(server.__file__).read_text(encoding='utf-8'))

    def test_every_new_error_code_has_words_for_people(self):
        codes = set(__import__('re').findall(r"'(hub_[a-z_]+)'", (ROOT / 'brainos' / 'model_hub_history.py').read_text(encoding='utf-8')))
        table = (ROOT / 'web' / 'ux04_errors.js').read_text(encoding='utf-8')
        self.assertTrue(codes)
        self.assertEqual([c for c in sorted(codes) if f'{c}:' not in table], [])

    def test_the_page_logic(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/model_hub_history_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'model_hub_history_logic: \d+ checks passed')


def model_hub_files_kind():
    from brainos import model_hub_files
    return model_hub_files.KIND


if __name__ == '__main__':
    unittest.main()
