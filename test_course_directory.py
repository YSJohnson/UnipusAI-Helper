"""Run with python test_course_directory.py (requires Edge; no account needed)."""
import queue
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote

import UnipusAI_Helper as app
from fluent_ui import FluentModernGUI


def test_course_directory_layouts(driver):
    for layout in ('task', 'node'):
        # Different build suffixes must not break either supported layout.
        html = '<div class="unipus-tabs_unitTabScrollContainer__fXBxR"><button>Unit 1</button></div>'
        for index, title in enumerate(('Reading', 'Vocabulary', 'Reading')):
            required = f'<i class="courses-unit_{layout}RequireIcon__newBuild"></i>' if index == 0 else ''
            html += (f'<div class="courses-unit_{layout}ItemInnerLayout__newBuild">'
                     f'<span class="courses-unit_{layout}TypeName__newBuild" '
                     f'onclick="window.clicked={index}">{title}</span>{required}</div>')
        driver.get('data:text/html;charset=utf-8,' + quote(html))
        gui = SimpleNamespace(
            driver=driver, WebDriverWait=app.WebDriverWait, EC=app.EC,
            By=app.By, NoSuchElementException=app.NoSuchElementException,
            gui_log_queue=queue.Queue(), logger=None,
            _extract_course_task_section=lambda _: '',
        )
        with patch('fluent_ui.time.sleep'):
            tabs = FluentModernGUI._scan_course_directory(gui)
        assert [tab['l1_title'] for tab in tabs] == ['Reading', 'Vocabulary', 'Reading']
        assert [tab['is_compulsory'] for tab in tabs] == [True, False, False]
        assert tabs[2]['_name_occurrence'] == 1

        solver = app.AISolver.__new__(app.AISolver)
        solver.driver = driver
        solver.ai_client = Mock()
        solver.processed_hashes = set()
        solver._should_stop = lambda: False
        solver.stop_requested = Mock()
        solver.stop_requested.wait.return_value = False
        solver._process_tab_with_accumulation = Mock()
        solver.process_selected_tabs([tabs[2]])
        assert driver.execute_script('return window.clicked') == 2
        solver._process_tab_with_accumulation.assert_called_once()


if __name__ == '__main__':
    options = app.webdriver.EdgeOptions()
    options.add_argument('--headless=new')
    browser = app.webdriver.Edge(options=options)
    try:
        test_course_directory_layouts(browser)
        print('Legacy and node course directory layouts: PASS')
    finally:
        browser.quit()
