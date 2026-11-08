"""Issue #36: explicit learning-record flags, conservative matching and selection."""
import os
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from fluent_ui import FluentModernGUI as GUI
from task_grading import (
    READ_GRADING_JS, assign_task_scoring, build_grading_index, selectable_task, task_scoring,
)


def task(name='Discussion', flag=False, **extra):
    return dict(name=name, role='group', scoreTaskFlag=flag, **extra)


def records(unit='u1', children=None):
    return {'unit_id': unit, 'tree': [
        {'nodeId': unit, 'role': 'unit', 'children': [
            {'role': 'section', 'name': 'Section A', 'children':
             children if children is not None else [task()]}
        ]}
    ]}


class GradingIndexTests(unittest.TestCase):
    def test_same_names_in_other_units_and_sections_are_independent(self):
        first = records()
        first['tree'][0]['children'].append(
            {'role': 'section', 'name': 'Section B', 'children': [task(flag=True)]})
        index = build_grading_index([first, records('u2', [task(flag=True)])])
        self.assertIs(task_scoring(index, 'u1', 'Section A', 'Discussion'), False)
        self.assertIs(task_scoring(index, 'u1', 'Section B', 'Discussion'), True)
        self.assertIs(task_scoring(index, 'u2', 'Section A', 'Discussion'), True)

    def test_numeric_scores_and_question_types_do_not_replace_explicit_boolean(self):
        for flag in (None, 0, 1, 'false', 'true', ''):
            with self.subTest(flag=flag):
                node = task(flag=flag, score=0, tabType='discussion')
                index = build_grading_index([records(children=[node])])
                self.assertIsNone(task_scoring(index, 'u1', 'Section A', 'Discussion'))
        node.pop('scoreTaskFlag')
        index = build_grading_index([records(children=[node])])
        self.assertIsNone(task_scoring(index, 'u1', 'Section A', 'Discussion'))

    def test_parent_flag_is_not_inherited_by_unknown_child(self):
        node = {'role': 'node', 'name': 'Reading', 'scoreTaskFlag': False,
                'children': [{'role': 'group', 'name': 'Text'}]}
        index = build_grading_index([records(children=[node])])
        self.assertIsNone(task_scoring(index, 'u1', 'Section A / Reading', 'Text'))

    def test_ambiguous_record_paths_remain_unknown(self):
        index = build_grading_index([records(children=[task(), task(flag=True)])])
        self.assertIsNone(task_scoring(index, 'u1', 'Section A', 'Discussion'))

    def test_ambiguous_directory_paths_remain_unknown(self):
        index = build_grading_index([records()])
        tabs = [dict(_unit_id='u1', section='Section A', l1_title='Discussion') for _ in range(2)]
        assign_task_scoring(tabs, index)
        self.assertTrue(all(t['is_scored'] is None for t in tabs))

    def test_wrong_unit_root_and_missing_sections_never_match_by_name(self):
        item = records('u2')
        item['unit_id'] = 'u1'
        self.assertEqual(build_grading_index([item]), {})
        index = build_grading_index([records()])
        self.assertIsNone(task_scoring(index, 'u1', '', 'Discussion'))

    def test_whitespace_and_case_are_normalized_without_dropping_hierarchy(self):
        node = {'role': 'node', 'name': 'Preview', 'children': [task('Preview')]}
        index = build_grading_index([records(children=[node])])
        self.assertIs(task_scoring(index, 'u1', ' section  a / PREVIEW ', ' preview\n'), False)
        self.assertIsNone(task_scoring(index, 'u1', 'Section A', 'Preview'))

    def test_only_explicitly_ungraded_tasks_are_excluded(self):
        for flag in (True, False, None):
            self.assertTrue(selectable_task({'is_scored': flag}, False))
            self.assertEqual(selectable_task({'is_scored': flag}, True), flag is not False)
        self.assertTrue(selectable_task({}, True))


class ScanHarness:
    READ_TASKS_JS = GUI.READ_TASKS_JS
    UNIT_LABEL_JS = GUI.UNIT_LABEL_JS
    TASK_ITEM_SELECTOR = GUI.TASK_ITEM_SELECTOR
    _parse_unit_label = staticmethod(GUI._parse_unit_label)
    _status_key = staticmethod(GUI._status_key)
    _format_task_display = staticmethod(GUI._format_task_display)
    _is_active_unit = GUI._is_active_unit
    _enrich_task_grading = GUI._enrich_task_grading
    _scan_course_directory = GUI._scan_course_directory

    def __init__(self):
        self.gui_log_queue = queue.Queue()
        self.logger = None
        self.By, self.EC = By, EC
        self.solver = SimpleNamespace(_is_publish_page=lambda: False)
        units = [Mock(text=f'Unit {i}') for i in (1, 2)]
        for i, unit in enumerate(units, 1):
            unit.get_attribute.return_value = f'id-u{i}-tab'
        self.units = units
        self.active = units[1]
        self.clicks = []
        self.driver = Mock(current_url='https://ucloud.unipus.cn/app/cmgt/resource-detail/123')
        self.driver.execute_script.side_effect = self.execute
        self.driver.find_elements.return_value = []
        self.driver.execute_async_script.side_effect = lambda _, resource, ids: {
            'units': [records(u) for u in ids], 'failed': 0}
        container = Mock()
        container.find_elements.return_value = units
        self.WebDriverWait = Mock(return_value=Mock(until=Mock(return_value=container)))

    def execute(self, script, *args):
        if script == self.UNIT_LABEL_JS:
            return args[0].text
        if script == self.READ_TASKS_JS:
            return [{'name': 'Discussion', 'section': 'Section A', 'compulsory': True,
                     'index': 0, 'status': '未开始'}]
        if 'arguments[0].click()' in script:
            self.active = args[0]
            self.clicks.append(args[0])
        else:
            return self.active is args[0]


class ScanGradingTests(unittest.TestCase):
    def test_current_unit_reads_only_its_records_without_switching_units(self):
        scan = ScanHarness()
        tabs = scan._scan_course_directory(current_unit_only=True)
        self.assertEqual(scan.clicks, [])
        self.assertEqual(len(tabs), 1)
        self.assertIs(tabs[0]['is_scored'], False)
        self.assertEqual(scan.driver.execute_async_script.call_args.args[1:], ('123', ['u2']))

    def test_all_units_are_scoped_by_id_and_keep_execution_metadata(self):
        scan = ScanHarness()
        with patch('fluent_ui.time.sleep'):
            tabs = scan._scan_course_directory()
        self.assertEqual(scan.clicks, scan.units)
        self.assertEqual([t['_unit_id'] for t in tabs], ['u1', 'u2'])
        self.assertEqual([t['_unit_idx'] for t in tabs], [0, 1])
        self.assertEqual([t['_name_occurrence'] for t in tabs], [0, 0])
        self.assertTrue(all(t['_section_title'] == 'Section A' for t in tabs))
        self.assertEqual(scan.driver.execute_async_script.call_args.args[2], ['u1', 'u2'])

    def test_record_failure_keeps_the_original_task(self):
        scan = ScanHarness()
        scan.driver.execute_async_script.side_effect = RuntimeError('unavailable')
        tabs = scan._scan_course_directory(current_unit_only=True)
        self.assertEqual(len(tabs), 1)
        self.assertIsNone(tabs[0]['is_scored'])
        self.assertIn('计分未知', tabs[0]['display'])
        self.assertTrue(tabs[0]['is_compulsory'])


class GradingSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])
        cls.gui = GUI(Mock(), Mock(), SimpleNamespace(config=SimpleNamespace(username='test', model='test')), {
            'gui_log_queue': queue.Queue(), 'WebDriverWait': WebDriverWait, 'EC': EC, 'By': By,
            'NoSuchElementException': NoSuchElementException, 'WebDriverHelper': Mock(),
        })

    @classmethod
    def tearDownClass(cls):
        cls.gui._closing = True
        cls.gui.close()
        cls.gui.deleteLater()
        cls.qt.processEvents()

    def setUp(self):
        self.gui.exclude_ungraded.setChecked(False)
        self.gui._all_tabs = [dict(l1_idx=i, l2_idx=-1, l1_title=f'Task {i}', display=f'Task {i}',
                                  unit_label='Unit 1', section='Section A', is_compulsory=True,
                                  status='todo', is_scored=flag)
                              for i, flag in enumerate((True, False, None))]
        self.gui._build_tab_list_ui()

    def selected(self):
        return [item['index'] for item in self.gui._tab_widget_items if item['checkbox'].isChecked()]

    def test_default_preserves_required_progress_tasks_and_shows_grade_labels(self):
        self.assertEqual(self.selected(), [0, 1, 2])
        self.assertEqual([i['grading'].text() for i in self.gui._tab_widget_items],
                         ['计分', '不计分', '计分未知'])

    def test_enabling_filter_and_every_bulk_selection_keep_unknown_tasks(self):
        self.gui.exclude_ungraded.setChecked(True)
        self.assertEqual(self.selected(), [0, 2])
        for action in (self.gui._on_select_all, self.gui._on_select_compulsory,
                       self.gui._on_select_pending):
            self.gui._on_deselect_all()
            action()
            self.assertEqual(self.selected(), [0, 2])

    def test_filter_survives_rescan_and_can_be_disabled(self):
        self.gui.exclude_ungraded.setChecked(True)
        self.gui._build_tab_list_ui(current_unit_only=True)
        self.assertEqual(self.selected(), [0, 2])
        self.gui.exclude_ungraded.setChecked(False)
        self.gui._on_select_pending()
        self.assertEqual(self.selected(), [0, 1, 2])

    def test_manual_selection_remains_available_with_filter_enabled(self):
        self.gui.exclude_ungraded.setChecked(True)
        self.gui._tab_widget_items[1]['checkbox'].setChecked(True)
        self.assertEqual(self.selected(), [0, 1, 2])
        self.assertEqual(len(self.gui._selected_tab_keys), 3)

    def test_search_and_select_filtered_results_respect_grading_filter(self):
        self.gui.task_search.setText('不计分')
        self.assertTrue(self.gui._task_matches_query(self.gui._all_tabs[1], '不计分'))
        self.gui._on_deselect_all()
        self.gui.exclude_ungraded.setChecked(True)
        self.gui._on_select_visible()
        self.assertEqual(self.selected(), [])
        self.gui.exclude_ungraded.setChecked(False)
        self.gui._on_select_visible()
        self.assertEqual(self.selected(), [1])
        self.gui.task_search.clear()


class GradingBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='issue36_')
        options = webdriver.EdgeOptions()
        for argument in ('--headless=new', '--no-sandbox', '--disable-gpu',
                         '--disable-dev-shm-usage', '--disable-extensions'):
            options.add_argument(argument)
        options.add_argument('--user-data-dir=' + cls.work.name + '/profile')
        cls.driver = webdriver.Edge(options=options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.work.cleanup()

    def read(self, mode='ok', origin='https://ucloud.unipus.cn'):
        # Shadow browser services with offline fixtures; no network or real credentials.
        fixture = """
            const mode = arguments[2], location = {origin: arguments[3]};
            const localStorage = {getItem: key => key === '__token'
                ? JSON.stringify({jwt: 'fixture'}) : '{}'};
            const requests = [];
            const originalDone = arguments[arguments.length - 1];
            arguments[arguments.length - 1] = result => originalDone({result, requests});
            const fetch = async (url, options) => {
                requests.push({url, credentials: options.credentials, auth: options.headers.Authorization});
                if (options.signal.aborted) throw new Error('aborted');
                if (url === '/api/account/user/info') return {ok: true, json: async () =>
                    mode === 'auth' ? {code: 401} : {code: 1, value: {
                        userInfo: {appUserId: '1234567890123456789', ssoId: 'self'}
                    }}};
                const unit = new URL(url, location.origin).searchParams.get('nodeId');
                if (mode === 'partial' && unit === 'u2') throw new Error('unavailable');
                return {ok: mode !== 'http', json: async () => ({code: 1, value: {
                    list: [{role: 'unit', nodeId: unit, children: []}]
                }})};
            };
        """
        return self.driver.execute_async_script(fixture + READ_GRADING_JS, '123', ['u1', 'u2'], mode, origin)

    def test_authenticated_read_requests_only_scanned_units_and_keeps_ids_exact(self):
        output = self.read()
        self.assertEqual(output['result']['failed'], 0)
        self.assertEqual([u['unit_id'] for u in output['result']['units']], ['u1', 'u2'])
        self.assertEqual(len(output['requests']), 3)
        self.assertIn('appUserId=1234567890123456789', output['requests'][1]['url'])
        self.assertTrue(all(r['credentials'] == 'same-origin' for r in output['requests']))
        self.assertNotIn('jwt', str(output['result']))

    def test_one_failed_unit_does_not_discard_successful_units(self):
        result = self.read('partial')['result']
        self.assertEqual(result['failed'], 1)
        self.assertEqual([u['unit_id'] for u in result['units']], ['u1'])

    def test_auth_and_http_failures_do_not_return_false_scoring_flags(self):
        for mode in ('auth', 'http'):
            with self.subTest(mode=mode):
                self.assertEqual(self.read(mode)['result'], {'units': [], 'failed': 2})

    def test_other_origins_never_receive_authenticated_requests(self):
        output = self.read(origin='https://example.invalid')
        self.assertEqual(output['requests'], [])
        self.assertEqual(output['result'], {'units': [], 'failed': 2})


if __name__ == '__main__':
    unittest.main()
