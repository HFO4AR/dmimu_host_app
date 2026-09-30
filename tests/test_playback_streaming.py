import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dmimu.playback import StreamingPlayback
from dmimu.protocol import encode_frame
from dmimu.recordings import Recordings
from dmimu.service import Service
from dmimu.storage import Settings


class StreamingPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.recordings = Recordings(self.directory.name)
        self.identifier = 'a' * 32

    def tearDown(self):
        self.directory.cleanup()

    def recording(self, blocks, **extra):
        path = self.recordings.path(self.identifier)
        with path.open('w') as file:
            file.write(json.dumps(dict(format='dmimu.raw', version=1, created_at=1000, source='demo', **extra)) + '\n')
            for elapsed, stamp, raw in blocks:
                file.write(json.dumps(dict(elapsed=elapsed, time=stamp, data=base64.b64encode(raw).decode())) + '\n')
        return path

    @staticmethod
    def signature(sample):
        elapsed, stamp, frame = sample
        return elapsed, stamp, frame.slave_id, frame.kind, frame.values, frame.raw

    def test_random_seek_matches_full_decoder_including_fragmented_frames(self):
        blocks = []
        for i in range(120):
            raw = encode_frame(1 + i % 4, [i, i + 1, i + 2] if i % 4 != 3 else [1, 0, 0, 0])
            # Completion time belongs to the second chunk, even at a checkpoint.
            blocks.extend([(i / 10, 10000 + i * .3, raw[:9]), (i / 10 + .01, 10000 + i * .3 + .001, raw[9:])])
        path = self.recording(blocks)
        expected = list(self.recordings.samples(self.identifier))
        stream = StreamingPlayback(path, checkpoint_bytes=512)
        self.assertGreater(len(stream.checkpoints), 10)
        self.assertEqual(stream.sample_count, len(expected))
        for position in (7.31, 0, stream.duration, .11, 4.21, 1.01):
            due = [sample for sample in expected if sample[0] <= position]
            latest = {}
            for sample in due:
                latest[sample[2].channel] = sample
            got = stream.seek(position)
            self.assertEqual({sample[2].channel: self.signature(sample) for sample in got},
                             {channel: self.signature(sample) for channel, sample in latest.items()})
            replay = []
            while True:
                batch, drained = stream.advance(stream.duration, maximum=7)
                self.assertLessEqual(len(batch), 7)
                replay.extend(batch)
                if drained:
                    break
            self.assertEqual([self.signature(s) for s in replay],
                             [self.signature(s) for s in expected if s[0] > position])

    def test_equal_elapsed_boundary_includes_all_chunks_and_preserves_host_time(self):
        path = self.recording([(0, 5000, encode_frame(1, [1, 2, 3])),
                               (1, 42, encode_frame(2, [.1, .2, .3])),
                               (1, 43, encode_frame(3, [4, 5, 6])),
                               (1, 44, encode_frame(1, [7, 8, 9])),
                               (2, 45, encode_frame(2, [.4, .5, .6]))])
        stream = StreamingPlayback(path, checkpoint_bytes=1)
        preview = stream.seek(1)
        self.assertEqual([sample[1] for sample in preview], [42, 43, 44])
        batch, drained = stream.advance(2)
        self.assertTrue(drained)
        self.assertEqual([(elapsed, stamp) for elapsed, stamp, _ in batch], [(2, 45)])

    def test_primary_slave_snapshot_respects_recorded_configuration(self):
        path = self.recording([(0, 100, encode_frame(1, [99, 99, 99], slave_id=2)),
                               (1, 101, encode_frame(1, [1, 2, 3], slave_id=7)),
                               (2, 102, encode_frame(2, [88, 88, 88], slave_id=2))],
                              device={'configuration': {'slave_id': 7, 'interval_ms': 1}})
        stream = StreamingPlayback(path, checkpoint_bytes=1)
        self.assertEqual(stream.primary_slave_id, 7)
        self.assertEqual(stream.header['device']['configuration']['interval_ms'], 1)
        samples = stream.seek(2)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][2].slave_id, 7)
        self.assertEqual(samples[0][2].values['x'], 1)

    def test_index_and_read_ahead_are_bounded_over_half_million_frames(self):
        raw = encode_frame(2, [.1, .2, .3]) * 3000
        path = self.recording((i, 1700000000 + i, raw) for i in range(168))
        stream = StreamingPlayback(path, checkpoint_bytes=1)
        self.assertEqual(stream.sample_count, 504000)
        self.assertLessEqual(len(stream.checkpoints), stream.MAX_CHECKPOINTS + 1)
        stream.seek(80)
        total = 0
        while True:
            batch, drained = stream.advance(stream.duration, maximum=1000)
            self.assertLessEqual(len(batch), 1000)
            if stream._pending:
                self.assertLessEqual(len(stream._pending[2]), 65536 // 19)
            total += len(batch)
            if drained:
                break
        self.assertEqual(total, (168 - 81) * 3000)
        self.assertFalse(hasattr(stream, 'samples'))
        self.assertFalse(hasattr(stream, 'file'))

    def test_interior_corruption_timestamp_order_and_file_change_are_rejected(self):
        path = self.recording([(1, 2, encode_frame(1, [1, 2, 3]))])
        stream = StreamingPlayback(path)
        with path.open('a') as file:
            file.write('{"interrupted')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            stream.seek(1)
        recovered = StreamingPlayback(path)
        self.assertEqual(recovered.sample_count, 1)
        with path.open('a') as file:
            file.write('\n{}\n')
        with self.assertRaisesRegex(ValueError, '中间'):
            StreamingPlayback(path)
        path = self.recording([(1, 2, encode_frame(1, [1, 2, 3])), (0, 3, encode_frame(1, [4, 5, 6]))])
        with self.assertRaisesRegex(ValueError, '顺序'):
            StreamingPlayback(path)

    def test_empty_recording_and_cancel(self):
        path = self.recording([(0, 1, b'no valid frame')])
        with self.assertRaisesRegex(ValueError, '有效帧'):
            StreamingPlayback(path)
        event = threading.Event();event.set()
        with self.assertRaisesRegex(ValueError, '取消'):
            StreamingPlayback(path, cancel=event)

    def test_service_speed_keeps_measurement_time_and_drains_final_batch(self):
        path = self.recording([(0, 7000, encode_frame(1, [0, 0, 0])),
                               (1, 7000.1, encode_frame(1, [1, 2, 3]) * 2250),
                               (1, 7000.2, encode_frame(2, [.1, .2, .3]) * 2250)])
        service = Service(Settings(self.directory.name), port_provider=lambda: [])
        service.auto_connect = False
        try:
            result = service._open_playback(self.identifier)
            self.assertTrue(result['streaming'])
            generation = service.generation
            service._seek(0)
            self.assertGreater(service.generation, generation)
            self.assertEqual(service.history[-1]['measurement_time'], 7000)
            self.assertFalse('samples' in service.playback or 'times' in service.playback)
            service.playback.update(playing=True, speed=4, last_tick=100)
            with patch('dmimu.service.time.monotonic', return_value=100.25):
                service._tick_playback()
            self.assertTrue(service.playback['playing'])  # First bounded batch is not the EOF.
            self.assertEqual(service.playback['position'], 1)
            with patch('dmimu.service.time.monotonic', return_value=100.26):
                service._tick_playback()
            self.assertFalse(service.playback['playing'])
            self.assertEqual(service.history[-1]['measurement_time'], 7000.2)
            self.assertAlmostEqual(service.latest['angular_velocity']['values']['z'], .3, places=6)
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
