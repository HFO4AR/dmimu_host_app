import base64
import json
import math
import tempfile
import threading
import unittest

import numpy as np

from dmimu.allan import analyze_recording, compute
from dmimu.protocol import encode_frame
from dmimu.recordings import Recordings


class AllanTests(unittest.TestCase):
    def test_hand_calculated_overlapping_rate_variance(self):
        # x=[1,2,4,8], m=1: (1²+2²+4²)/(2*3)=3.5.
        # m=2 has one pair of means: 6-1.5=4.5; variance=4.5²/2.
        result = compute([[v, 2*v, -v] for v in (1, 2, 4, 8)], 10, m_values=[1, 2])
        self.assertAlmostEqual(result['points'][0]['deviation']['x']**2, 3.5)
        self.assertAlmostEqual(result['points'][1]['deviation']['x']**2, 10.125)
        self.assertAlmostEqual(result['points'][1]['deviation']['y']**2, 40.5)
        self.assertEqual(result['points'][0]['pairs'], 3)
        self.assertEqual(result['points'][1]['pairs'], 1)
        self.assertEqual([p['tau_s'] for p in result['points']], [.1, .2])

    def test_every_overlapping_pair_matches_independent_window_means(self):
        rng = np.random.default_rng(12)
        values = rng.normal(size=(31, 3))
        result = compute(values, 50, m_values=[1, 2, 3, 7, 15])
        for point in result['points']:
            m = point['m']
            differences = [values[i+m:i+2*m].mean(axis=0)-values[i:i+m].mean(axis=0)
                           for i in range(len(values)-2*m+1)]
            expected = np.sqrt(np.mean(np.square(differences), axis=0)/2)
            for index, axis in enumerate(('x','y','z')):
                self.assertAlmostEqual(point['deviation'][axis], expected[index], places=12)

    def test_constant_acceleration_and_large_dc_offset(self):
        result = compute([[1e6, 9.80665, -2.5]] * 1000, 100, channel='acceleration')
        self.assertTrue(all(abs(p['deviation']['x']) < 1e-9 for p in result['points']))
        self.assertFalse(result['metrics']['x']['white_noise']['identified'])
        self.assertEqual(result['metrics']['x']['white_noise']['value'], None)
        self.assertFalse(result['detrended'])
        self.assertFalse(result['interpolated'])

    def test_white_noise_known_slope_and_coefficient(self):
        rate, sigma = 100., .2
        values = np.random.default_rng(123).normal(scale=sigma, size=(100000, 3))
        result = compute(values, rate)
        first = result['points'][0]['deviation']['x']
        self.assertAlmostEqual(first, sigma, delta=sigma*.02)
        estimate = result['metrics']['x']['white_noise']
        self.assertTrue(estimate['identified'])
        self.assertAlmostEqual(estimate['value'], sigma/math.sqrt(rate), delta=.003)
        self.assertAlmostEqual(estimate['slope'], -.5, delta=.16)
        self.assertIn('assumption', result['metrics']['x']['bias_instability'])

    def test_chunk_boundary_matches_scalar_reference(self):
        # Force both prefix and squared-difference chunks across BLOCK boundaries.
        values = np.random.default_rng(7).normal(size=(65580, 3))
        result = compute(values, 1000, m_values=[3, 300])
        for point in result['points']:
            m = point['m']
            prefix = np.vstack([np.zeros(3), np.cumsum(values, axis=0)])
            diff = (prefix[2*m:]-2*prefix[m:-m]+prefix[:-2*m])/m
            expected = np.sqrt(np.sum(diff*diff, axis=0)/(2*len(diff)))
            self.assertAlmostEqual(point['deviation']['z'], expected[2], places=11)

    def test_uneven_and_duplicate_host_times_do_not_retime_samples(self):
        values = [[v, 0, 0] for v in range(8)]
        plain = compute(values, 100, m_values=[1, 2])
        host = compute(values, 100, timestamps=[1, 1, 1, 1.1, 1.2, 1.19, 5, 5], m_values=[1, 2])
        self.assertEqual(plain['points'], host['points'])
        self.assertEqual(host['quality']['duplicate_host_timestamps'], 3)
        self.assertEqual(host['quality']['backwards_host_timestamps'], 1)
        self.assertEqual(host['quality']['large_host_gaps'], 1)
        self.assertFalse(host['quality']['host_time_is_device_clock'])

    def test_invalid_input_and_scales(self):
        for rate in (0, -1, True, float('nan'), 10001):
            with self.assertRaises(ValueError):
                compute([[1,2,3]]*8, rate)
        for values in ([[1,2]], [[1,2,3]]*3, [[float('inf'),0,0]]*8):
            with self.assertRaises(ValueError):
                compute(values, 100)
        with self.assertRaises(ValueError):
            compute([[1,2,3]]*8, 100, m_values=[0])
        with self.assertRaises(ValueError):
            compute([[1,2,3]]*8, 100, timestamps=[0])

    def test_cancellation_during_vector_blocks(self):
        cancel = threading.Event()
        progress = []
        def update(item):
            progress.append(item)
            if item['stage'] == 'deviation':
                cancel.set()
        with self.assertRaisesRegex(ValueError, '取消'):
            compute(np.ones((100000,3)), 1000, cancel=cancel, progress=update)
        self.assertIn('deviation', [p['stage'] for p in progress])
        self.assertNotIn('complete', [p['stage'] for p in progress])

    def test_recording_uses_only_selected_channel_and_rejects_multi_device(self):
        with tempfile.TemporaryDirectory() as directory:
            recordings = Recordings(directory)
            identifier = 'a'*32
            path = recordings.path(identifier)
            def write(slave=1):
                with path.open('w', encoding='utf-8') as out:
                    out.write(json.dumps({'format':'dmimu.raw','version':1})+'\n')
                    for i in range(8):
                        raw = encode_frame(1,[0,0,9.8],1)+encode_frame(2,[i,0,0],slave if i==7 else 1)
                        out.write(json.dumps({'elapsed':i*.01,'time':1000+i*.01,'data':base64.b64encode(raw).decode()})+'\n')
            write()
            result = analyze_recording(recordings, identifier, 'angular_velocity', 100)
            self.assertEqual(result['sample_count'], 8)
            self.assertEqual(result['slave_id'], 1)
            self.assertAlmostEqual(result['points'][0]['deviation']['x']**2, .5)
            write(2)
            with self.assertRaisesRegex(ValueError, '多个设备'):
                analyze_recording(recordings, identifier, 'angular_velocity', 100)


if __name__ == '__main__':
    unittest.main()
