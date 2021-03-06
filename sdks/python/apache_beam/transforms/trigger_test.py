#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Unit tests for the triggering classes."""

from __future__ import absolute_import

import collections
import os.path
import pickle
import unittest
from builtins import range
from builtins import zip

# patches unittest.TestCase to be python3 compatible
import future.tests.base  # pylint: disable=unused-import
import yaml

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import StandardOptions
from apache_beam.runners import pipeline_context
from apache_beam.runners.direct.clock import TestClock
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.test_stream import TestStream
from apache_beam.testing.util import assert_that
from apache_beam.testing.util import equal_to
from apache_beam.transforms import trigger
from apache_beam.transforms.core import Windowing
from apache_beam.transforms.trigger import AccumulationMode
from apache_beam.transforms.trigger import AfterAll
from apache_beam.transforms.trigger import AfterAny
from apache_beam.transforms.trigger import AfterCount
from apache_beam.transforms.trigger import AfterEach
from apache_beam.transforms.trigger import AfterProcessingTime
from apache_beam.transforms.trigger import AfterWatermark
from apache_beam.transforms.trigger import DefaultTrigger
from apache_beam.transforms.trigger import GeneralTriggerDriver
from apache_beam.transforms.trigger import InMemoryUnmergedState
from apache_beam.transforms.trigger import Repeatedly
from apache_beam.transforms.trigger import TriggerFn
from apache_beam.transforms.window import FixedWindows
from apache_beam.transforms.window import IntervalWindow
from apache_beam.transforms.window import Sessions
from apache_beam.transforms.window import TimestampCombiner
from apache_beam.transforms.window import TimestampedValue
from apache_beam.transforms.window import WindowedValue
from apache_beam.transforms.window import WindowFn
from apache_beam.utils.timestamp import MAX_TIMESTAMP
from apache_beam.utils.timestamp import MIN_TIMESTAMP
from apache_beam.utils.windowed_value import PaneInfoTiming


class CustomTimestampingFixedWindowsWindowFn(FixedWindows):
  """WindowFn for testing custom timestamping."""

  def get_transformed_output_time(self, unused_window, input_timestamp):
    return input_timestamp + 100


class TriggerTest(unittest.TestCase):

  def run_trigger_simple(self, window_fn, trigger_fn, accumulation_mode,
                         timestamped_data, expected_panes, *groupings,
                         **kwargs):
    # Groupings is a list of integers indicating the (uniform) size of bundles
    # to try. For example, if timestamped_data has elements [a, b, c, d, e]
    # then groupings=(5, 2) would first run the test with everything in the same
    # bundle, and then re-run the test with bundling [a, b], [c, d], [e].
    # A negative value will reverse the order, e.g. -2 would result in bundles
    # [e, d], [c, b], [a].  This is useful for deterministic triggers in testing
    # that the output is not a function of ordering or bundling.
    # If empty, defaults to bundles of size 1 in the given order.
    late_data = kwargs.pop('late_data', [])
    assert not kwargs

    def bundle_data(data, size):
      if size < 0:
        data = list(data)[::-1]
        size = -size
      bundle = []
      for timestamp, elem in data:
        windows = window_fn.assign(WindowFn.AssignContext(timestamp, elem))
        bundle.append(WindowedValue(elem, timestamp, windows))
        if len(bundle) == size:
          yield bundle
          bundle = []
      if bundle:
        yield bundle

    if not groupings:
      groupings = [1]
    for group_by in groupings:
      self.run_trigger(window_fn, trigger_fn, accumulation_mode,
                       bundle_data(timestamped_data, group_by),
                       bundle_data(late_data, group_by),
                       expected_panes)

  def run_trigger(self, window_fn, trigger_fn, accumulation_mode,
                  bundles, late_bundles,
                  expected_panes):
    actual_panes = collections.defaultdict(list)
    driver = GeneralTriggerDriver(
        Windowing(window_fn, trigger_fn, accumulation_mode), TestClock())
    state = InMemoryUnmergedState()

    for bundle in bundles:
      for wvalue in driver.process_elements(state, bundle, MIN_TIMESTAMP):
        window, = wvalue.windows
        self.assertEqual(window.max_timestamp(), wvalue.timestamp)
        actual_panes[window].append(set(wvalue.value))

    while state.timers:
      for timer_window, (name, time_domain, timestamp) in (
          state.get_and_clear_timers()):
        for wvalue in driver.process_timer(
            timer_window, name, time_domain, timestamp, state):
          window, = wvalue.windows
          self.assertEqual(window.max_timestamp(), wvalue.timestamp)
          actual_panes[window].append(set(wvalue.value))

    for bundle in late_bundles:
      for wvalue in driver.process_elements(state, bundle, MAX_TIMESTAMP):
        window, = wvalue.windows
        self.assertEqual(window.max_timestamp(), wvalue.timestamp)
        actual_panes[window].append(set(wvalue.value))

      while state.timers:
        for timer_window, (name, time_domain, timestamp) in (
            state.get_and_clear_timers()):
          for wvalue in driver.process_timer(
              timer_window, name, time_domain, timestamp, state):
            window, = wvalue.windows
            self.assertEqual(window.max_timestamp(), wvalue.timestamp)
            actual_panes[window].append(set(wvalue.value))

    self.assertEqual(expected_panes, actual_panes)

  def test_fixed_watermark(self):
    self.run_trigger_simple(
        FixedWindows(10),  # pyformat break
        AfterWatermark(),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (13, 'c')],
        {IntervalWindow(0, 10): [set('ab')],
         IntervalWindow(10, 20): [set('c')]},
        1,
        2,
        3,
        -3,
        -2,
        -1)

  def test_fixed_watermark_with_early(self):
    self.run_trigger_simple(
        FixedWindows(10),  # pyformat break
        AfterWatermark(early=AfterCount(2)),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (3, 'c')],
        {IntervalWindow(0, 10): [set('ab'), set('abc')]},
        2)
    self.run_trigger_simple(
        FixedWindows(10),  # pyformat break
        AfterWatermark(early=AfterCount(2)),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (3, 'c')],
        {IntervalWindow(0, 10): [set('abc'), set('abc')]},
        3)

  def test_fixed_watermark_with_early_late(self):
    self.run_trigger_simple(
        FixedWindows(100),  # pyformat break
        AfterWatermark(early=AfterCount(3),
                       late=AfterCount(2)),
        AccumulationMode.DISCARDING,
        zip(range(9), 'abcdefghi'),
        {IntervalWindow(0, 100): [
            set('abcd'), set('efgh'),  # early
            set('i'),                  # on time
            set('vw'), set('xy')       # late
            ]},
        2,
        late_data=zip(range(5), 'vwxyz'))

  def test_sessions_watermark_with_early_late(self):
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        AfterWatermark(early=AfterCount(2),
                       late=AfterCount(1)),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (15, 'b'), (7, 'c'), (30, 'd')],
        {
            IntervalWindow(1, 25): [
                set('abc'),                # early
                set('abc'),                # on time
                set('abcxy')               # late
            ],
            IntervalWindow(30, 40): [
                set('d'),                  # on time
            ],
            IntervalWindow(1, 40): [
                set('abcdxyz')             # late
            ],
        },
        2,
        late_data=[(1, 'x'), (2, 'y'), (21, 'z')])

  def test_fixed_after_count(self):
    self.run_trigger_simple(
        FixedWindows(10),  # pyformat break
        AfterCount(2),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (3, 'c'), (11, 'z')],
        {IntervalWindow(0, 10): [set('ab')]},
        1,
        2)
    self.run_trigger_simple(
        FixedWindows(10),  # pyformat break
        AfterCount(2),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (3, 'c'), (11, 'z')],
        {IntervalWindow(0, 10): [set('abc')]},
        3,
        4)

  def test_fixed_after_first(self):
    self.run_trigger_simple(
        FixedWindows(10),  # pyformat break
        AfterAny(AfterCount(2), AfterWatermark()),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (3, 'c')],
        {IntervalWindow(0, 10): [set('ab')]},
        1,
        2)
    self.run_trigger_simple(
        FixedWindows(10),  # pyformat break
        AfterAny(AfterCount(5), AfterWatermark()),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (3, 'c')],
        {IntervalWindow(0, 10): [set('abc')]},
        1,
        2,
        late_data=[(1, 'x'), (2, 'y'), (3, 'z')])

  def test_repeatedly_after_first(self):
    self.run_trigger_simple(
        FixedWindows(100),  # pyformat break
        Repeatedly(AfterAny(AfterCount(3), AfterWatermark())),
        AccumulationMode.ACCUMULATING,
        zip(range(7), 'abcdefg'),
        {IntervalWindow(0, 100): [
            set('abc'),
            set('abcdef'),
            set('abcdefg'),
            set('abcdefgx'),
            set('abcdefgxy'),
            set('abcdefgxyz')]},
        1,
        late_data=zip(range(3), 'xyz'))

  def test_sessions_after_all(self):
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        AfterAll(AfterCount(2), AfterWatermark()),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (3, 'c')],
        {IntervalWindow(1, 13): [set('abc')]},
        1,
        2)
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        AfterAll(AfterCount(5), AfterWatermark()),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (3, 'c')],
        {IntervalWindow(1, 13): [set('abcxy')]},
        1,
        2,
        late_data=[(1, 'x'), (2, 'y'), (3, 'z')])

  def test_sessions_default(self):
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        DefaultTrigger(),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b')],
        {IntervalWindow(1, 12): [set('ab')]},
        1,
        2,
        -2,
        -1)

    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        AfterWatermark(),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b'), (15, 'c'), (16, 'd'), (30, 'z'), (9, 'e'),
         (10, 'f'), (30, 'y')],
        {IntervalWindow(1, 26): [set('abcdef')],
         IntervalWindow(30, 40): [set('yz')]},
        1,
        2,
        3,
        4,
        5,
        6,
        -4,
        -2,
        -1)

  def test_sessions_watermark(self):
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        AfterWatermark(),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (2, 'b')],
        {IntervalWindow(1, 12): [set('ab')]},
        1,
        2,
        -2,
        -1)

  def test_sessions_after_count(self):
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        AfterCount(2),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (15, 'b'), (6, 'c'), (30, 's'), (31, 't'), (50, 'z'),
         (50, 'y')],
        {IntervalWindow(1, 25): [set('abc')],
         IntervalWindow(30, 41): [set('st')],
         IntervalWindow(50, 60): [set('yz')]},
        1,
        2,
        3)

  def test_sessions_repeatedly_after_count(self):
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        Repeatedly(AfterCount(2)),
        AccumulationMode.ACCUMULATING,
        [(1, 'a'), (15, 'b'), (6, 'c'), (2, 'd'), (7, 'e')],
        {IntervalWindow(1, 25): [set('abc'), set('abcde')]},
        1,
        3)
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        Repeatedly(AfterCount(2)),
        AccumulationMode.DISCARDING,
        [(1, 'a'), (15, 'b'), (6, 'c'), (2, 'd'), (7, 'e')],
        {IntervalWindow(1, 25): [set('abc'), set('de')]},
        1,
        3)

  def test_sessions_after_each(self):
    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        AfterEach(AfterCount(2), AfterCount(3)),
        AccumulationMode.ACCUMULATING,
        zip(range(10), 'abcdefghij'),
        {IntervalWindow(0, 11): [set('ab')],
         IntervalWindow(0, 15): [set('abcdef')]},
        2)

    self.run_trigger_simple(
        Sessions(10),  # pyformat break
        Repeatedly(AfterEach(AfterCount(2), AfterCount(3))),
        AccumulationMode.ACCUMULATING,
        zip(range(10), 'abcdefghij'),
        {IntervalWindow(0, 11): [set('ab')],
         IntervalWindow(0, 15): [set('abcdef')],
         IntervalWindow(0, 17): [set('abcdefgh')]},
        2)

  def test_picklable_output(self):
    global_window = (trigger.GlobalWindow(),)
    driver = trigger.BatchGlobalTriggerDriver()
    unpicklable = (WindowedValue(k, 0, global_window)
                   for k in range(10))
    with self.assertRaises(TypeError):
      pickle.dumps(unpicklable)
    for unwindowed in driver.process_elements(None, unpicklable, None):
      self.assertEqual(pickle.loads(pickle.dumps(unwindowed)).value,
                       list(range(10)))


class RunnerApiTest(unittest.TestCase):

  def test_trigger_encoding(self):
    for trigger_fn in (
        DefaultTrigger(),
        AfterAll(AfterCount(1), AfterCount(10)),
        AfterAny(AfterCount(10), AfterCount(100)),
        AfterWatermark(early=AfterCount(1000)),
        AfterWatermark(early=AfterCount(1000), late=AfterCount(1)),
        Repeatedly(AfterCount(100)),
        trigger.OrFinally(AfterCount(3), AfterCount(10))):
      context = pipeline_context.PipelineContext()
      self.assertEqual(
          trigger_fn,
          TriggerFn.from_runner_api(trigger_fn.to_runner_api(context), context))


class TriggerPipelineTest(unittest.TestCase):

  def setUp(self):
    # Use state on the TestCase class, since other references would be pickled
    # into a closure and not have the desired side effects.
    TriggerPipelineTest.all_records = []

  def record_dofn(self):
    class RecordDoFn(beam.DoFn):

      def process(self, element):
        TriggerPipelineTest.all_records.append(element)

    return RecordDoFn()

  def test_after_count(self):
    with TestPipeline() as p:
      def construct_timestamped(k_t):
        return TimestampedValue((k_t[0], k_t[1]), k_t[1])

      def format_result(k_v):
        return ('%s-%s' % (k_v[0], len(k_v[1])), set(k_v[1]))

      result = (p
                | beam.Create([1, 2, 3, 4, 5, 10, 11])
                | beam.FlatMap(lambda t: [('A', t), ('B', t + 5)])
                | beam.Map(construct_timestamped)
                | beam.WindowInto(FixedWindows(10), trigger=AfterCount(3),
                                  accumulation_mode=AccumulationMode.DISCARDING)
                | beam.GroupByKey()
                | beam.Map(format_result))
      assert_that(result, equal_to(
          list(
              {
                  'A-5': {1, 2, 3, 4, 5},
                  # A-10, A-11 never emitted due to AfterCount(3) never firing.
                  'B-4': {6, 7, 8, 9},
                  'B-3': {10, 15, 16},
              }.items()
          )))

  def test_multiple_accumulating_firings(self):
    # PCollection will contain elements from 1 to 10.
    elements = [i for i in range(1, 11)]

    ts = TestStream().advance_watermark_to(0)
    for i in elements:
      ts.add_elements([('key', str(i))])
      if i % 5 == 0:
        ts.advance_watermark_to(i)
        ts.advance_processing_time(5)

    options = PipelineOptions()
    options.view_as(StandardOptions).streaming = True
    with TestPipeline(options=options) as p:
      _ = (p
           | ts
           | beam.WindowInto(
               FixedWindows(10),
               accumulation_mode=trigger.AccumulationMode.ACCUMULATING,
               trigger=AfterWatermark(
                   early=AfterAll(
                       AfterCount(1), AfterProcessingTime(5))
               ))
           | beam.GroupByKey()
           | beam.FlatMap(lambda x: x[1])
           | beam.ParDo(self.record_dofn()))

    # The trigger should fire twice. Once after 5 seconds, and once after 10.
    # The firings should accumulate the output.
    first_firing = [str(i) for i in elements if i <= 5]
    second_firing = [str(i) for i in elements]
    self.assertListEqual(first_firing + second_firing,
                         TriggerPipelineTest.all_records)


class TranscriptTest(unittest.TestCase):

  # We must prepend an underscore to this name so that the open-source unittest
  # runner does not execute this method directly as a test.
  @classmethod
  def _create_test(cls, spec):
    counter = 0
    name = spec.get('name', 'unnamed')
    unique_name = 'test_' + name
    while hasattr(cls, unique_name):
      counter += 1
      unique_name = 'test_%s_%d' % (name, counter)
    setattr(cls, unique_name, lambda self: self._run_log_test(spec))

  # We must prepend an underscore to this name so that the open-source unittest
  # runner does not execute this method directly as a test.
  @classmethod
  def _create_tests(cls, transcript_filename):
    for spec in yaml.load_all(open(transcript_filename)):
      cls._create_test(spec)

  def _run_log_test(self, spec):
    if 'error' in spec:
      self.assertRaisesRegex(
          Exception, spec['error'], self._run_log, spec)
    else:
      self._run_log(spec)

  def _run_log(self, spec):

    def parse_int_list(s):
      """Parses strings like '[1, 2, 3]'."""
      s = s.strip()
      assert s[0] == '[' and s[-1] == ']', s
      if not s[1:-1].strip():
        return []
      return [int(x) for x in s[1:-1].split(',')]

    def split_args(s):
      """Splits 'a, b, [c, d]' into ['a', 'b', '[c, d]']."""
      args = []
      start = 0
      depth = 0
      for ix in range(len(s)):
        c = s[ix]
        if c in '({[':
          depth += 1
        elif c in ')}]':
          depth -= 1
        elif c == ',' and depth == 0:
          args.append(s[start:ix].strip())
          start = ix + 1
      assert depth == 0, s
      args.append(s[start:].strip())
      return args

    def parse(s, names):
      """Parse (recursive) 'Foo(arg, kw=arg)' for Foo in the names dict."""
      s = s.strip()
      if s in names:
        return names[s]
      elif s[0] == '[':
        return parse_int_list(s)
      elif '(' in s:
        assert s[-1] == ')', s
        callee = parse(s[:s.index('(')], names)
        posargs = []
        kwargs = {}
        for arg in split_args(s[s.index('(') + 1:-1]):
          if '=' in arg:
            kw, value = arg.split('=', 1)
            kwargs[kw] = parse(value, names)
          else:
            posargs.append(parse(arg, names))
        return callee(*posargs, **kwargs)
      else:
        try:
          return int(s)
        except ValueError:
          raise ValueError('Unknown function: %s' % s)

    def parse_fn(s, names):
      """Like parse(), but implicitly calls no-arg constructors."""
      fn = parse(s, names)
      if isinstance(fn, type):
        return fn()
      return fn

    # pylint: disable=wrong-import-order, wrong-import-position
    from apache_beam.transforms import window as window_module
    # pylint: enable=wrong-import-order, wrong-import-position
    window_fn_names = dict(window_module.__dict__)
    window_fn_names.update({'CustomTimestampingFixedWindowsWindowFn':
                            CustomTimestampingFixedWindowsWindowFn})
    trigger_names = {'Default': DefaultTrigger}
    trigger_names.update(trigger.__dict__)

    window_fn = parse_fn(spec.get('window_fn', 'GlobalWindows'),
                         window_fn_names)
    trigger_fn = parse_fn(spec.get('trigger_fn', 'Default'), trigger_names)
    accumulation_mode = getattr(
        AccumulationMode, spec.get('accumulation_mode', 'ACCUMULATING').upper())
    timestamp_combiner = getattr(
        TimestampCombiner,
        spec.get('timestamp_combiner', 'OUTPUT_AT_EOW').upper())

    def only_element(xs):
      x, = list(xs)
      return x

    transcript = [only_element(line.items()) for line in spec['transcript']]

    self._execute(
        window_fn, trigger_fn, accumulation_mode, timestamp_combiner,
        transcript, spec)

  def _windowed_value_info(self, windowed_value):
    # Currently some runners operate at the millisecond level, and some at the
    # microsecond level.  Trigger transcript timestamps are expressed as
    # integral units of the finest granularity, whatever that may be.
    # In these tests we interpret them as integral seconds and then truncate
    # the results to integral seconds to allow for portability across
    # different sub-second resolutions.
    window, = windowed_value.windows
    return {
        'window': [int(window.start), int(window.max_timestamp())],
        'values': sorted(windowed_value.value),
        'timestamp': int(windowed_value.timestamp),
        'index': windowed_value.pane_info.index,
        'nonspeculative_index': windowed_value.pane_info.nonspeculative_index,
        'early': windowed_value.pane_info.timing == PaneInfoTiming.EARLY,
        'late': windowed_value.pane_info.timing == PaneInfoTiming.LATE,
        'final': windowed_value.pane_info.is_last,
    }


class TriggerDriverTranscriptTest(TranscriptTest):

  def _execute(
      self, window_fn, trigger_fn, accumulation_mode, timestamp_combiner,
      transcript, unused_spec):

    driver = GeneralTriggerDriver(
        Windowing(window_fn, trigger_fn, accumulation_mode, timestamp_combiner),
        TestClock())
    state = InMemoryUnmergedState()
    output = []
    watermark = MIN_TIMESTAMP

    def fire_timers():
      to_fire = state.get_and_clear_timers(watermark)
      while to_fire:
        for timer_window, (name, time_domain, t_timestamp) in to_fire:
          for wvalue in driver.process_timer(
              timer_window, name, time_domain, t_timestamp, state):
            output.append(self._windowed_value_info(wvalue))
        to_fire = state.get_and_clear_timers(watermark)

    for action, params in transcript:

      if action != 'expect':
        # Fail if we have output that was not expected in the transcript.
        self.assertEqual(
            [], output, msg='Unexpected output: %s before %s: %s' % (
                output, action, params))

      if action == 'input':
        bundle = [
            WindowedValue(t, t, window_fn.assign(WindowFn.AssignContext(t, t)))
            for t in params]
        output = [
            self._windowed_value_info(wv)
            for wv in driver.process_elements(state, bundle, watermark)]
        fire_timers()

      elif action == 'watermark':
        watermark = params
        fire_timers()

      elif action == 'expect':
        for expected_output in params:
          for candidate in output:
            if all(candidate[k] == expected_output[k]
                   for k in candidate if k in expected_output):
              output.remove(candidate)
              break
          else:
            self.fail('Unmatched output %s in %s' % (expected_output, output))

      elif action == 'state':
        # TODO(robertwb): Implement once we support allowed lateness.
        pass

      else:
        self.fail('Unknown action: ' + action)

    # Fail if we have output that was not expected in the transcript.
    self.assertEqual([], output, msg='Unexpected output: %s' % output)


class TestStreamTranscriptTest(TranscriptTest):
  """A suite of TestStream-based tests based on trigger transcript entries.
  """

  def _execute(
      self, window_fn, trigger_fn, accumulation_mode, timestamp_combiner,
      transcript, spec):

    runner_name = TestPipeline().runner.__class__.__name__
    if runner_name in spec.get('broken_on', ()):
      self.skipTest('Known to be broken on %s' % runner_name)

    test_stream = TestStream()
    for action, params in transcript:
      if action == 'expect':
        test_stream.add_elements([('expect', params)])
      else:
        test_stream.add_elements([('expect', [])])
        if action == 'input':
          test_stream.add_elements([('input', e) for e in params])
        elif action == 'watermark':
          test_stream.advance_watermark_to(params)
        elif action == 'clock':
          test_stream.advance_processing_time(params)
        elif action == 'state':
          pass  # Requires inspection of implementation details.
        else:
          raise ValueError('Unexpected action: %s' % action)
    test_stream.add_elements([('expect', [])])

    class Check(beam.DoFn):
      """A StatefulDoFn that verifies outputs are produced as expected.

      This DoFn takes in two kinds of inputs, actual outputs and
      expected outputs.  When an actual output is received, it is buffered
      into state, and when an expected output is received, this buffered
      state is retrieved and compared against the expected value(s) to ensure
      they match.

      The key is ignored, but all items must be on the same key to share state.
      """
      def process(
          self, element, seen=beam.DoFn.StateParam(
              beam.transforms.userstate.BagStateSpec(
                  'seen',
                  beam.coders.FastPrimitivesCoder()))):
        _, (action, data) = element
        if action == 'actual':
          seen.add(data)

        elif action == 'expect':
          actual = list(seen.read())
          seen.clear()

          if len(actual) > len(data):
            raise AssertionError(
                'Unexpected output: expected %s but got %s' % (data, actual))
          elif len(data) > len(actual):
            raise AssertionError(
                'Unmatched output: expected %s but got %s' % (data, actual))
          else:

            def diff(actual, expected):
              for key in sorted(expected.keys(), reverse=True):
                if key in actual:
                  if actual[key] != expected[key]:
                    return key

            for output in actual:
              diffs = [diff(output, expected) for expected in data]
              if all(diffs):
                raise AssertionError(
                    'Unmatched output: %s not found in %s (diffs in %s)' % (
                        output, data, diffs))

        else:
          raise ValueError('Unexpected action: %s' % action)

    with TestPipeline(options=PipelineOptions(streaming=True)) as p:
      # Split the test stream into a branch of to-be-processed elements, and
      # a branch of expected results.
      inputs, expected = (
          p
          | test_stream
          | beam.MapTuple(
              lambda tag, value: beam.pvalue.TaggedOutput(tag, ('key', value))
              ).with_outputs('input', 'expect'))
      # Process the inputs with the given windowing to produce actual outputs.
      outputs = (
          inputs
          | beam.MapTuple(
              lambda key, value: TimestampedValue((key, value), value))
          | beam.WindowInto(
              window_fn,
              trigger=trigger_fn,
              accumulation_mode=accumulation_mode,
              timestamp_combiner=timestamp_combiner)
          | beam.GroupByKey()
          | beam.MapTuple(
              lambda k, vs,
                     window=beam.DoFn.WindowParam,
                     t=beam.DoFn.TimestampParam,
                     p=beam.DoFn.PaneInfoParam: (
                         k,
                         self._windowed_value_info(WindowedValue(
                             vs, windows=[window], timestamp=t, pane_info=p))))
          # Place outputs back into the global window to allow flattening
          # and share a single state in Check.
          | 'Global' >> beam.WindowInto(beam.transforms.window.GlobalWindows()))
      # Feed both the expected and actual outputs to Check() for comparison.
      tagged_expected = (
          expected | beam.MapTuple(lambda key, value: (key, ('expect', value))))
      tagged_outputs = (
          outputs | beam.MapTuple(lambda key, value: (key, ('actual', value))))
      # pylint: disable=expression-not-assigned
      (tagged_expected, tagged_outputs) | beam.Flatten() | beam.ParDo(Check())


TRANSCRIPT_TEST_FILE = os.path.join(
    os.path.dirname(__file__), '..', 'testing', 'data',
    'trigger_transcripts.yaml')
if os.path.exists(TRANSCRIPT_TEST_FILE):
  TriggerDriverTranscriptTest._create_tests(TRANSCRIPT_TEST_FILE)
  TestStreamTranscriptTest._create_tests(TRANSCRIPT_TEST_FILE)


if __name__ == '__main__':
  unittest.main()
