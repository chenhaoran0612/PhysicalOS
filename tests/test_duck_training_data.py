"""Issue #79: the Microduck's new models and what the training project recorded about them.

- the policies of the new packages without PyTorch: PyTorch's own float32 ``tanh`` (``duck_tanh``), the
  two-actor floor dance and the recharge show's parent actors (``duck_policy``);
- each model's training and acceptance records and the recordings of its accepted episodes
  (``duck_evidence``), checked against their checksum lists;
- the training dashboard's (large) answer read in gzip and trimmed, with the delivered versions and the
  training runs of each show (``duck_kit``);
- a simulated duck that was added with older packages performs the ones of this installation;
- the model library: every version, its own independent start poses, the accepted episodes.

Everything here is made up (``duck_support`` and the helpers below); the real kit is tested in
``test_duck_kit.RealKit``. The ``TORCH_TANH`` pairs were computed with PyTorch 2.5.1 on an Apple-silicon Mac."""
import csv
import gzip
import hashlib
import http.server
import io
import json
import os
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np

import duck_support as ds
from brainos import duck_evidence, duck_kit, duck_policy, duck_tanh, model_hub
from brainos.errors import CodedError
from brainos.service import Platform

F32 = np.float32
# (input bits, torch.tanh bits): inputs where numpy's float32 tanh or the correctly rounded value differ from
# PyTorch's, and a few plain ones (zeros, tiny, saturated).
TORCH_TANH = (
    (0xc0f6bed1, 0xbf7ffff9), (0x4008dd2f, 0x3f78fd16), (0xc10419d2, 0xbf7ffffe), (0x40c27eed, 0x3f7fff50),
    (0x40f4d0ec, 0x3f7ffff8), (0x41078582, 0x3f7fffff), (0xc0cfadd5, 0xbf7fffb3), (0x3fbabeca, 0x3f65bf26),
    (0x40ef4839, 0x3f7ffff5), (0x40e4e7ec, 0x3f7fffeb), (0x3fab4b1f, 0x3f5f0a02), (0xc10f43ca, 0xbf800000),
    (0x410f7103, 0x3f800000), (0x410e79b4, 0x3f800000), (0xc10afcf4, 0xbf800000), (0xc10e564e, 0xbf800000),
    (0xbe2a410b, 0xbe28b3d1), (0xc10e9c90, 0xbf800000), (0xc10e3319, 0xbf800000), (0x410e0c8f, 0x3f800000),
    (0x410b97ac, 0x3f800000), (0x410af7b3, 0x3f800000), (0x410ba01d, 0x3f800000), (0x00000000, 0x00000000),
    (0x80000000, 0x80000000), (0x0da24260, 0x0da24260), (0xb3d6bf95, 0xb3d6bf95), (0x3f000000, 0x3eec9a9f),
    (0xbf000000, 0xbeec9a9f), (0x3f800000, 0x3f42f7d6), (0x40800000, 0x3f7fd40c), (0x41080000, 0x3f7fffff),
    (0x410b3333, 0x3f800000), (0xc1100000, 0xbf800000), (0x41a00000, 0x3f800000),
)
# (input bits, torch.atanh bits): inputs where the correctly rounded atanh is not PyTorch's.
TORCH_ATANH = ((0xbe2bbfd0, 0xbe2d6335), (0x3e4b7c0b, 0x3e4e3a6e), (0xbdf41b70, 0xbdf545ed), (0xbdc91bed, 0xbdc9c260),
               (0xbe3fea50, 0xbe4235fd), (0xbdfd5c3d, 0xbdfeaa32), (0x3ee13926, 0x3ef1b638), (0x3e351c04, 0x3e3708bd))


def bits(values):
    return np.asarray(values, dtype=np.uint32).view(F32)


class Tanh(unittest.TestCase):
    def test_it_is_pytorchs_float32_tanh_bit_for_bit(self):
        inputs, expected = bits([p[0] for p in TORCH_TANH]), bits([p[1] for p in TORCH_TANH])
        got = duck_tanh.tanh(inputs)
        self.assertEqual(got.dtype, F32)
        self.assertEqual(got.view(np.uint32).tolist(), expected.view(np.uint32).tolist())
        # each element alone, as a 14-wide layer computes it, gives the same
        self.assertEqual([int(duck_tanh.tanh(inputs[i:i + 1]).view(np.uint32)[0]) for i in range(len(inputs))], [p[1] for p in TORCH_TANH])
        self.assertTrue(any(np.tanh(inputs) != expected))                      # numpy's own tanh is not it

    def test_it_is_odd_within_one_unit_of_the_exact_value_and_saturates(self):
        x = np.linspace(-12, 12, 200_001, dtype=F32)
        y = duck_tanh.tanh(x)
        exact = np.tanh(x.astype(np.float64))
        self.assertTrue(np.all(np.abs(y.astype(np.float64) - exact) <= np.spacing(np.abs(exact).astype(F32)).astype(np.float64) * 1.0001))
        self.assertTrue(np.array_equal(duck_tanh.tanh(-x), -y))
        self.assertTrue(np.all(y[np.abs(x) > F32(8.664339742)] == np.sign(x[np.abs(x) > F32(8.664339742)])))
        self.assertTrue(np.isnan(duck_tanh.tanh(np.array([np.nan], dtype=F32))[0]))

    def test_atanh_is_pytorchs_too_and_says_what_it_should_at_and_beyond_one(self):
        inputs, expected = bits([p[0] for p in TORCH_ATANH]), bits([p[1] for p in TORCH_ATANH])
        self.assertEqual(duck_tanh.atanh(inputs).view(np.uint32).tolist(), expected.view(np.uint32).tolist())
        self.assertTrue(any(np.arctanh(inputs.astype(np.float64)).astype(F32) != expected))   # the correctly rounded value is not it
        edge = duck_tanh.atanh(np.array([1, -1, 1.5, np.inf, 0.0, -0.0], dtype=F32))
        self.assertEqual((edge[0], edge[1]), (np.inf, -np.inf))
        self.assertTrue(np.isnan(edge[2]) and np.isnan(edge[3]))
        self.assertEqual(edge[4:].view(np.uint32).tolist(), [0, 0x80000000])
        x = np.linspace(-0.999, 0.999, 20_001, dtype=F32)
        np.testing.assert_allclose(duck_tanh.atanh(x), np.arctanh(x.astype(np.float64)), rtol=3e-7)

    def test_a_fused_multiply_add_rounds_once_even_where_the_float64_sum_lands_on_a_tie(self):
        a = F32(1 + 2 ** -12)                          # a * a = 1 + 2^-11 + 2^-24: exactly half way between two float32
        tiny = F32(2 ** -100)
        up, down = F32(1 + 2 ** -11 + 2 ** -23), F32(1 + 2 ** -11)
        self.assertEqual(duck_tanh.fma(np.array([a]), np.array([a]), np.array([tiny]))[0], up)
        self.assertEqual(duck_tanh.fma(np.array([a]), np.array([a]), np.array([-tiny]))[0], down)
        self.assertEqual(duck_tanh.fma(np.array([a]), np.array([a]), np.array([F32(0)]))[0], down)      # a real tie: to even


def floor_policy(seed=3):
    """A made-up two-actor checkpoint of the floor dance's layout (``architecture.floor_phase_actor``)."""
    rng = np.random.default_rng(seed)
    policy = {}
    for part in ('early', 'late'):
        for index, shape in zip((0, 2, 4), [(ds.HIDDEN, ds.OBS), (ds.HIDDEN, ds.HIDDEN), (ds.ACT, ds.HIDDEN)]):
            policy[f'actor.{part}.network.{index}.weight'] = rng.normal(size=shape).astype(F32)
            policy[f'actor.{part}.network.{index}.bias'] = rng.normal(size=shape[0]).astype(F32)
        policy[f'actor.{part}.mean'] = rng.normal(size=ds.OBS).astype(F32)
        policy[f'actor.{part}.variance'] = rng.uniform(0.5, 2, size=ds.OBS).astype(F32)
    policy.update(log_std=np.full(ds.ACT, -0.65, dtype=F32), obs_mean=np.zeros(ds.OBS, dtype=F32), obs_var=np.ones(ds.OBS, dtype=F32),
                  obs_count=np.array(1e-4, dtype=np.float64))
    return policy


def net(policy, prefix, x):
    for index in (0, 2, 4):
        x = policy[f'{prefix}{index}.weight'].astype(np.float64) @ x + policy[f'{prefix}{index}.bias']
        if index < 4:
            x = np.tanh(x)
    return x


def standard(x, mean, variance):
    return np.clip((x - mean) / np.sqrt(variance.astype(np.float64) + 1e-6), -10, 10)


def smooth(v):
    v = min(1.0, max(0.0, v))
    return v * v * (3 - 2 * v)


class Policies(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_floor_dance_hands_over_from_the_early_actor_to_the_late_one_between_ticks_180_and_189(self):
        p = floor_policy()
        (self.root / 'floor_spin.pt').write_bytes(ds.checkpoint_bytes('floor_spin', p, '3.10.0', extra={
            'architecture': {'obs_size': ds.OBS, 'action_size': ds.ACT, 'hidden': ds.HIDDEN, 'floor_phase_actor': True}}))
        policy = duck_policy.load_policy(self.root / 'floor_spin.pt', self.root)
        self.assertEqual(policy.kind, 'floor_phase')
        for tick, blend in ((100, 0.0), (180, 0.0), (184, 4 / 9), (189, 1.0), (300, 1.0)):
            x = np.array([0.3, -0.2, 0.1, tick / 475], dtype=F32)
            early = np.tanh(net(p, 'actor.early.network.', standard(x, p['actor.early.mean'], p['actor.early.variance'])))
            late = np.tanh(net(p, 'actor.late.network.', standard(x, p['actor.late.mean'], p['actor.late.variance'])))
            with self.subTest(tick=tick):
                np.testing.assert_allclose(policy(x), early + blend * (late - early), atol=2e-6)

    def recharge_package(self, parents=True):
        (self.root / 'recharge.pt').write_bytes(ds.checkpoint_bytes('recharge', ds.weights(), '3.10.0'))
        (self.root / 'duck_show').mkdir()
        (self.root / 'duck_show' / 'exit_specialist.py').write_text('# the training code\n')
        rng, layers = np.random.default_rng(5), {}
        for name in ('entrance-actor.npz', 'exit-actor.npz')[:2 if parents else 1]:
            layers[name] = {f'{i}.{kind}': rng.normal(size=shape if kind == 'weight' else shape[0]).astype(F32)
                            for i, shape in zip((0, 2, 4), [(ds.HIDDEN, ds.OBS), (ds.HIDDEN, ds.HIDDEN), (ds.ACT, ds.HIDDEN)]) for kind in ('weight', 'bias')}
            np.savez(self.root / 'duck_show' / name, **layers[name])
        return layers

    def test_the_recharge_show_walks_on_its_parent_actors_and_corrects_only_in_its_opening_window(self):
        layers, p = self.recharge_package(), ds.weights()
        policy = duck_policy.load_policy(self.root / 'recharge.pt', self.root)
        self.assertEqual(policy.kind, 'exit_specialist')
        frozen = {name: {f'p.{k}': v for k, v in parts.items()} for name, parts in layers.items()}
        for seconds in (1.0, 10.0, 16.36, 18.0):
            x = np.array([0.4, -0.1, 0.2, seconds / 19.28], dtype=F32)
            n = standard(x, p['obs_mean'], p['obs_var'])
            entrance, exit_actor = (net(frozen[name], 'p.', n) for name in ('entrance-actor.npz', 'exit-actor.npz'))
            fade, brake = smooth((seconds - 16.06) / 0.60), smooth((seconds - 0.22) / 0.30) * smooth((2.40 - seconds) / 0.60)
            mean = entrance + fade * (exit_actor - entrance) + brake * (net(p, 'actor.', n) - exit_actor)
            with self.subTest(seconds=seconds):
                np.testing.assert_allclose(policy(x), np.tanh(mean), atol=5e-6)

    def test_the_parent_actors_are_used_only_for_the_recharge_show_of_a_package_that_has_them(self):
        self.recharge_package()
        self.assertEqual(duck_policy.load_policy(self.root / 'recharge.pt').kind, 'plain')          # no package given
        (self.root / 'nod.pt').write_bytes(ds.checkpoint_bytes('nod', ds.weights(), '3.10.0'))
        self.assertEqual(duck_policy.load_policy(self.root / 'nod.pt', self.root).kind, 'plain')

    def test_a_missing_or_unreadable_parent_actor_is_refused(self):
        self.recharge_package(parents=False)
        with self.assertRaises(duck_policy.CheckpointError):
            duck_policy.load_policy(self.root / 'recharge.pt', self.root)
        np.save(self.root / 'object.npy', np.array([{'code': 1}], dtype=object), allow_pickle=True)
        np.save(self.root / 'plain.npy', np.zeros(3, dtype=F32))
        for kind in ('object', 'plain'):                          # an object array is never unpickled; one array is not a network
            (self.root / 'duck_show' / 'exit-actor.npz').write_bytes((self.root / f'{kind}.npy').read_bytes())
            with self.subTest(kind=kind), self.assertRaises(duck_policy.CheckpointError):
                duck_policy.load_policy(self.root / 'recharge.pt', self.root)


# ---------------------------------------------------------------- made-up records
def episodes(seeds, failing=(), steps=900):
    return [{'seed': s, 'steps': steps + i, 'episode_return': 30.0 + i, 'success': s not in failing, 'phases': ['走'], 'reason': 'complete' if s not in failing else 'fall'}
            for i, s in enumerate(seeds)]


def evidence_files(task, independent_from, checkpoint='c' * 64, zero_fails=(), device='cpu'):
    fixed, independent = duck_kit.seed_group(duck_kit.HOME_SEEDS[0]), duck_kit.seed_group(independent_from)
    showtime = {'protocol': duck_kit.SHOWTIME, 'min_seconds': 15, 'max_seconds': 30,
                'fixed': {'outcomes': [{'seed': s, 'seconds': 18.0 + i / 100, 'in_window': True} for i, s in enumerate(fixed)]},
                'confirmation': {'outcomes': [{'seed': s, 'seconds': 18.5, 'in_window': True} for s in independent]}}
    folder = 'evidence/a100/' if device == 'cuda' else 'evidence/cpu/'
    names = ('fixed.json', 'confirmation.json', 'confirmation_zero.json') if device == 'cuda' else ('fixed-cpu.json', 'independent-cpu.json', 'independent-zero-cpu.json')
    return {
        'evidence/model-identity.json': json.dumps({'checkpoint_sha256': checkpoint, 'training_device': 'cpu',
                                                    'independent_evaluation_platform': {'device': device}}).encode(),
        'evidence/training-status.json': json.dumps({'iteration': 2, 'requested_iterations': 12, 'total_steps': 4096,
                                                     'stop_reason': 'fixed_10_of_10', 'physics': 'native MuJoCo on CPU'}).encode(),
        'evidence/training-manifest.json': json.dumps({'optimization': {'num_envs': 4, 'learning_rate': 2e-6, 'hidden': 96, 'secret': 'x'},
                                                       'torch_version': '2.5.1', 'mujoco_version': '3.10.0'}).encode(),
        'evidence/protocol.json': json.dumps({'training_device': 'Mac CPU', 'optimizer': 'residual PPO', 'selection': 'first 10/10'}).encode(),
        'evidence/showtime.json': json.dumps(showtime).encode(),
        folder + names[0]: json.dumps({'episodes': episodes(fixed)}).encode(),
        folder + names[1]: json.dumps({'episodes': episodes(independent)}).encode(),
        folder + names[2]: json.dumps({'episodes': episodes(independent, zero_fails)}).encode(),
    }


def recordings_bundle(path, rows):
    """A made-up recordings bundle: ``rows`` of (task, group, seed, weights sha256)."""
    files = {}
    index = io.StringIO()
    out = csv.writer(index)
    out.writerow(['动作', '任务', '轮次', '更新', '组别', '起姿种子', '控制步数', '原速秒', '权重 SHA-256', '场景 XML SHA-256', '录像', '逐帧核验', '录像 SHA-256'])
    for task, group, seed, weights in rows:
        video = f'{task}/{group}/{seed}.mp4'
        files[video] = b'\x00\x00\x00\x18ftypmp42' + str(seed).encode()
        out.writerow(['节目', task, 1, 1, {'fixed': '固定组', 'independent': '独立组'}[group], seed, 900, '18.00', weights, 's' * 64, video, '', hashlib.sha256(files[video]).hexdigest()])
    files[duck_evidence.INDEX] = ('﻿' + index.getvalue()).encode('utf-8')
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('SHA256SUMS', ''.join(f'{hashlib.sha256(d).hexdigest()}  {n}\n' for n, d in sorted(files.items())))
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Evidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def package(self, more):
        path = self.root / 'show.zip'
        path.write_bytes(ds.package_bytes('nod', '3.10.0', more))
        return path

    def test_the_training_run_and_every_accepted_episode_come_out_of_the_package(self):
        found = duck_evidence.read_package(self.package(evidence_files('nod', 2_000_000_000, zero_fails={2_000_000_001, 2_000_000_002})))
        self.assertEqual(found['checkpoint_sha256'], 'c' * 64)
        self.assertEqual({k: found['training'][k] for k in ('updates', 'requested_updates', 'steps', 'stop_reason', 'device', 'torch')},
                         {'updates': 2, 'requested_updates': 12, 'steps': 4096, 'stop_reason': '固定组十个起姿全部通过后停止', 'device': 'Mac CPU', 'torch': '2.5.1'})
        self.assertEqual(found['training']['config'], {'num_envs': 4, 'hidden': 96, 'learning_rate': 2e-6})         # only the known settings
        groups = {g['id']: g for g in found['groups']}
        self.assertEqual([(g['id'], g['passed'], g['total']) for g in found['groups']], [('fixed', 10, 10), ('independent', 10, 10), ('zero', 8, 10)])
        self.assertEqual((groups['fixed']['min_s'], groups['fixed']['max_s'], groups['zero']['min_s']), (18.0, 18.09, None))
        self.assertEqual(groups['independent']['cases'][0], {'seed': 2_000_000_000, 'success': True, 'steps': 900, 'reason': 'complete', 'return': 30.0,
                                                             'duration_s': 18.5, 'in_window': True})
        self.assertEqual((found['protocol']['min_s'], found['protocol']['max_s'], found['protocol']['name']), (15, 30, duck_kit.SHOWTIME))

    def test_a_model_accepted_on_a_gpu_has_its_groups_in_the_gpu_folder(self):
        found = duck_evidence.read_package(self.package(evidence_files('nod', 2_000_000_000, device='cuda')))
        self.assertEqual([g['id'] for g in found['groups']], ['fixed', 'independent', 'zero'])

    def test_a_record_that_is_not_the_listed_one_is_refused_and_a_package_without_records_has_none(self):
        files = evidence_files('nod', 2_000_000_000)
        path = self.package(files)
        data = path.read_bytes()
        tampered = self.root / 'tampered.zip'
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(tampered, 'w') as out:
            for info in source.infolist():
                body = source.read(info)
                out.writestr(info, body.replace(b'4096', b'9999') if info.filename == 'evidence/training-status.json' else body)
        with self.assertRaises(duck_evidence.EvidenceError):
            duck_evidence.read_package(tampered)
        self.assertTrue(data)
        self.assertEqual(duck_evidence.read_package(self.package({}))['groups'], [])

    def test_a_name_the_training_project_wrote_in_utf8_without_saying_so_is_read_as_written(self):
        raw = '模型包/x.zip'.encode('utf-8').decode('cp437')
        self.assertEqual(duck_evidence.member_name(types.SimpleNamespace(flag_bits=0, filename=raw)), '模型包/x.zip')
        self.assertEqual(duck_evidence.member_name(types.SimpleNamespace(flag_bits=0x800, filename='模型包/x.zip')), '模型包/x.zip')

    def test_recordings_are_found_by_model_group_and_seed_and_checked_when_read(self):
        path = self.root / 'recordings.zip'
        sha = recordings_bundle(path, [('nod', 'fixed', 1_700_003_000, 'c' * 64), ('nod', 'independent', 2_000_000_000, 'c' * 64)])
        bundle = duck_evidence.Recordings(path, sha)
        index = bundle.index()
        self.assertEqual(sorted(index), [('nod', 'fixed', 1_700_003_000), ('nod', 'independent', 2_000_000_000)])
        self.assertEqual((index[('nod', 'fixed', 1_700_003_000)]['weights'], index[('nod', 'fixed', 1_700_003_000)]['steps']), ('c' * 64, 900))
        self.assertTrue(bundle.video('nod', 'fixed', 1_700_003_000).endswith(b'1700003000'))
        self.assertIsNone(bundle.video('nod', 'fixed', 1))
        refused = duck_evidence.Recordings(path, 'f' * 64)              # not the registered bundle: nothing of it is used
        with mock.patch.object(duck_evidence.hashlib, 'sha256', wraps=hashlib.sha256) as hashed:
            for _ in range(3):
                with self.assertRaises(duck_evidence.EvidenceError):
                    refused.index()
            self.assertEqual(hashed.call_count, 1)                        # and it is not read again until it changes
            os.utime(path, ns=(1, 1))
            with self.assertRaises(duck_evidence.EvidenceError):
                refused.video('nod', 'fixed', 1_700_003_000)
            self.assertEqual(hashed.call_count, 2)
        self.assertEqual(duck_evidence.Recordings(self.root / 'none.zip', sha).index(), {})


# ---------------------------------------------------------------- the dashboard
class Dashboard(unittest.TestCase):
    RELEASE = {'round_number': 132, 'iteration': 1, 'training_device': 'Mac CPU', 'fixed': {'successes': 10, 'episodes': 10, 'seeds': [1]},
               'confirmation': {'successes': 10, 'episodes': 10, 'seeds': [3_293_174_000]}, 'zero': {'successes': 8, 'episodes': 10},
               'duration': {'qualified': True, 'fixed': {'min_s': 16.78, 'max_s': 16.78, 'outcomes': [1] * 10}, 'confirmation': {'min_s': 16.76, 'max_s': 16.78}}}
    DATA = {'tasks': [{'id': 'water', 'title': '单脚探水', 'release': RELEASE,
                       'historical_models': [{'round_number': 6, 'iteration': 4, 'device': 'NVIDIA A100', 'fixed': {'successes': 10, 'episodes': 10},
                                              'confirmation': {'successes': 10, 'episodes': 10, 'seeds': [1_826_014_600]}, 'duration': {'qualified': False}}],
                       'rounds': [{'number': 132, 'state': 'completed', 'iteration': 1, 'total_steps': 3456, 'accelerator': 'CPU', 'events': [{}]},
                                  {'number': 5, 'state': 'not_started', 'iteration': 0, 'total_steps': 0},
                                  {'number': 6, 'state': 'completed', 'iteration': 4, 'total_steps': 8192, 'device': 'cuda', 'last_update': 5.0}],
                       'events': [{'round_number': 132, 'iteration': 1, 'successes': 10, 'episodes': 10, 'report_mtime': 9.0, 'duration': {'outcomes': []}}]}],
            'source': {'sync': {'state': 'error', 'error': 'trainer@192.0.2.1: refused'}}, 'rounds': [{}, {}]}

    def test_each_show_gets_its_delivered_version_the_ones_before_it_and_the_runs_that_trained_it(self):
        water = duck_kit.trim_training(self.DATA)['tasks'][0]
        self.assertEqual(water['release'], {'round': 132, 'update': 1, 'device': 'Mac CPU', 'fixed': [10, 10], 'independent': [10, 10], 'zero': [8, 10],
                                            'independent_from': 3_293_174_000, 'seconds': [16.76, 16.78], 'timed': True})
        self.assertEqual([(h['round'], h['independent_from'], h['timed'], h['seconds']) for h in water['history']], [(6, 1_826_014_600, False, None)])
        self.assertEqual([(r['round'], r['updates'], r['steps'], r['device']) for r in water['runs']], [(6, 4, 8192, 'cuda'), (132, 1, 3456, 'CPU')])
        self.assertEqual((water['run_count'], water['run_steps']), (2, 11648))
        self.assertNotIn('trainer@', json.dumps(duck_kit.trim_training(self.DATA)))

    def answer(self, body, gzip_it=True):
        payload = gzip.compress(body) if gzip_it else body
        stream = io.BytesIO(payload)
        return types.SimpleNamespace(headers={'Content-Encoding': 'gzip'} if gzip_it else {}, read=stream.read)

    def test_the_answer_is_unpacked_in_gzip_and_a_too_large_one_is_not_read_to_the_end(self):
        body = json.dumps(self.DATA).encode()
        self.assertEqual(duck_kit._read_answer(self.answer(body)), body)
        self.assertEqual(duck_kit._read_answer(self.answer(body, gzip_it=False)), body)
        with mock.patch.object(duck_kit, 'MAX_TRAINING_BYTES', 100):
            self.assertIsNone(duck_kit._read_answer(self.answer(b' ' * 10_000 + body)))

    def test_a_dashboard_on_this_computer_is_read_whole_in_gzip_keeping_only_the_fields_used(self):
        body, asked = json.dumps({**self.DATA, 'big': 'x' * 100_000}).encode(), []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append((self.path, self.headers.get('Accept-Encoding')))
                data = gzip.compress(body)
                self.send_response(200)
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class Server(http.server.ThreadingHTTPServer):
            def server_bind(self):                 # no name lookup of 127.0.0.1 (slow on some Macs)
                self.socket.bind(self.server_address)
                self.server_address = self.socket.getsockname()
                self.server_name, self.server_port = '127.0.0.1', self.server_address[1]

        server = Server(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        found = duck_kit.fetch_training(f'http://127.0.0.1:{server.server_port}')
        self.assertEqual(asked, [('/api/evolution', 'gzip')])
        self.assertEqual(found['tasks'][0]['release']['round'], 132)
        self.assertEqual(found['tasks'][0]['series'], [[132, 1, 10, 10, 9.0]])
        self.assertLess(len(json.dumps(found)), 5_000)


# ---------------------------------------------------------------- the kit in the app
def made_shows(root, mujoco_version):
    """A made-up kit with records and recordings: ``nod`` (two versions, the old one not in the kit), ``spin``
    (no longer delivered). Returns ``(kit folder, SHOWS, recordings checksum)``."""
    (root / 'shows').mkdir(parents=True)
    weights = ds.checkpoint_bytes('nod', ds.weights(), mujoco_version)
    checkpoint = hashlib.sha256(weights).hexdigest()
    package = ds.package_bytes('nod', mujoco_version, evidence_files('nod', 2_000_000_000, checkpoint, zero_fails={2_000_000_003}))
    (root / 'shows' / 'nod.zip').write_bytes(package)
    sha = hashlib.sha256(package).hexdigest()
    recordings = recordings_bundle(root / 'recordings.zip', [('nod', 'independent', 2_000_000_000, checkpoint), ('nod', 'fixed', 1_700_003_000, 'd' * 64)])
    old = {'package': 'nod-old.zip', 'sha256': 'e' * 64, 'legacy': True, 'origin': {'round': 1, 'update': 2, 'steps': 64, 'device': 'GPU'},
           'acceptance': {'fixed': [10, 10], 'independent': [7, 10]}, 'independent_from': duck_kit.HOME_SEEDS[0] + 500}
    shows = ({'id': 'nod', 'title': '点头', 'status': duck_kit.DELIVERED, 'summary': '测试用的节目。', 'package': 'nod.zip', 'sha256': sha,
              'origin': {'round': 9, 'update': 1, 'steps': 4096, 'device': 'CPU'}, 'acceptance': {'fixed': [10, 10], 'independent': [10, 10]},
              'independent_from': 2_000_000_000, 'seconds': [18.0, 18.5], 'history': (old,)},
             {'id': 'spin', 'title': '转圈', 'status': duck_kit.ARCHIVED, 'summary': '不再交付。',
              'history': ({**old, 'package': 'spin.zip', 'sha256': 'f' * 64, 'independent_from': duck_kit.HOME_SEEDS[0] + 900},)})
    (root / 'kit.json').write_text(json.dumps({'schema': 'duck.kit.v1', 'shows': {'nod': {'package': 'nod.zip', 'sha256': sha}},
                                               'engine': {'installed': True}}), encoding='utf-8')
    return root, shows, recordings


class App(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        kit, shows, recordings = made_shows(Path(self.tmp.name) / 'kit', '3.10.0')
        self.shows = shows
        for module, name, value in ((duck_kit, 'SHOWS', shows), (duck_kit, 'BY_ID', {s['id']: s for s in shows}),
                                    (duck_kit, 'PACKAGED', shows[:1]), (duck_kit, 'RECORDINGS', {**duck_kit.RECORDINGS, 'sha256': recordings})):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        dead = mock.patch.dict(os.environ, {duck_kit.DASHBOARD_ENV: 'http://127.0.0.1:9'})
        dead.start()
        self.addCleanup(dead.stop)
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(self.app.close)
        self.app.duck.kit = kit
        self.hub = self.app.model_hub

    def test_a_models_records_say_which_accepted_episodes_have_a_recording_of_its_own_weights(self):
        found = self.app.duck.evidence({'show': 'nod'})
        groups = {g['id']: g for g in found['records']['groups']}
        self.assertEqual([c['recording'] for c in groups['independent']['cases']][:2], [True, False])
        self.assertFalse(any(c['recording'] for c in groups['fixed']['cases']))            # that video shows other weights
        self.assertEqual([(v['round'], v['current'], v['in_kit']) for v in found['versions']], [(9, True, True), (1, False, False)])
        self.assertTrue(self.app.duck.recording('nod', 'independent', 2_000_000_000).startswith(b'\x00\x00\x00\x18ftyp'))
        for args in (('nod', 'fixed', 1_700_003_000), ('nod', 'zero', 2_000_000_000), ('spin', 'fixed', 1), ('nod', 'independent', '2000000000')):
            self.assertIsNone(self.app.duck.recording(*args), args)
        archived = self.app.duck.evidence({'show': 'spin'})
        self.assertEqual((archived['records'], [v['current'] for v in archived['versions']]), (None, [False]))
        with self.assertRaises(CodedError):
            self.app.duck.evidence({'show': 'nothing'})

    def test_the_page_lists_each_models_time_its_own_independent_group_and_the_versions_before_it(self):
        catalog = self.app.duck.catalog()
        nod, spin = catalog['shows']
        self.assertEqual((nod['seconds'], nod['independent_from'], [h['round'] for h in nod['history']]), ([18.0, 18.5], 2_000_000_000, [1]))
        self.assertEqual((spin['status'], spin['status_name'], spin['packaged'], spin['skill']), ('archived', '已归档', False, None))
        self.assertTrue(catalog['recordings'])

    def test_a_duck_added_with_older_packages_gets_this_installations_before_it_starts(self):
        sims = self.app.sims.root / 'robot-old'
        sims.mkdir(parents=True)
        (sims / 'duck.json').write_text(json.dumps({'shows': {'nod': 'old' * 21 + 'o', 'drunk': 'x' * 64}, 'default_show': 'drunk', 'name': '老鸭'}), encoding='utf-8')
        self.assertTrue(self.app.duck.refresh('robot-old'))
        config = json.loads((sims / 'duck.json').read_text(encoding='utf-8'))
        self.assertEqual((config['shows'], config['default_show'], config['name']), ({'nod': self.shows[0]['sha256']}, 'nod', '老鸭'))
        self.assertTrue((Path(config['packages_root']) / self.shows[0]['sha256']).is_dir())       # unpacked and checked
        self.assertFalse(self.app.duck.refresh('robot-old'))                                         # already up to date
        self.assertFalse(self.app.duck.refresh('robot-missing'))
        with mock.patch.object(self.app.duck, '_usable', return_value=({}, 'none')):
            (sims / 'duck.json').write_text(json.dumps({'shows': {'nod': 'kept'}}), encoding='utf-8')
            self.assertFalse(self.app.duck.refresh('robot-old'))                                     # nothing usable here: kept
            self.assertEqual(json.loads((sims / 'duck.json').read_text(encoding='utf-8'))['shows'], {'nod': 'kept'})

    def test_the_library_has_every_version_with_its_own_independent_start_poses_and_the_accepted_episodes(self):
        data = self.hub.overview({})
        models = {m['id']: m for m in data['models']}
        nod, spin = models['hubm-duck-nod'], models['hubm-duck-spin']
        current, old = nod['versions']
        self.assertEqual((current['label'], current['runnable'], current['superseded'], old['label'], old['runnable'], old['superseded']),
                         ('第 9 轮 · 第 1 次更新', True, False, '第 1 轮 · 第 2 次更新', False, True))
        self.assertEqual((nod['current_version_id'], nod['status']), (current['id'], 'delivered'))
        self.assertEqual((spin['status'], spin['status_name'], spin['current_version_id'], [v['superseded'] for v in spin['versions']]),
                         ('archived', '已归档', '', [True]))
        by = {(e['version_id'], e['set_id']): e for e in nod['evaluations']}
        sets = {s['id']: s for s in data['sets']}
        independent = model_hub._independent_id(2_000_000_000)
        self.assertEqual(sets[independent]['seeds'][:2], [2_000_000_000, 2_000_000_001])
        self.assertIn('点头 第 9 轮', sets[independent]['name'])
        new = by[(current['id'], independent)]
        self.assertEqual((new['passed'], new['total'], new['metrics']['success_rate'], new['metrics']['mean_steps'], new['metrics']['mean_duration_s']),
                         (10, 10, 100.0, 904.5, 18.5))
        self.assertTrue(new['id'].endswith(f"{self.shows[0]['sha256'][:12]}-independent"))
        legacy = by[(old['id'], model_hub._independent_id(duck_kit.HOME_SEEDS[0] + 500))]
        self.assertEqual((legacy['id'], legacy['metrics'], legacy['judgement']['met']), ('hube-duck-nod-independent', {'success_rate': 70.0}, False))
        detail = self.hub.evaluation({'evaluation_id': new['id']})
        self.assertEqual((len(detail['cases']), detail['cases'][0]['seed'], detail['cases'][0]['recording']), (10, 2_000_000_000, True))
        self.assertIn(model_hub.INDEPENDENT, sets)                         # the id issue #71 gave the independent set still resolves

    def test_an_old_version_is_never_run_in_the_simulation(self):
        self.hub.runnable_show = lambda show: True
        nod = next(m for m in self.hub.overview({})['models'] if m['id'] == 'hubm-duck-nod')
        old = nod['versions'][1]
        with self.assertRaises(CodedError) as caught:
            self.hub.run_evaluation({'model_id': nod['id'], 'version_id': old['id'], 'set_id': model_hub.FIXED, 'robot_id': 'robot-x'})
        self.assertEqual(caught.exception.code, 'hub_eval_not_runnable')
        self.assertEqual([v['runnable'] for v in next(m for m in self.hub.overview({})['models'] if m['id'] == 'hubm-duck-nod')['versions']], [True, False])


if __name__ == '__main__':
    unittest.main()
