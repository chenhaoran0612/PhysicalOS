"""Browser fixture for issue #42, third part: the robot a task is done with.

The same as tests/skill_run_browser_fixture.py (the console on a temporary state directory and one
device with the real management layer and a stand-in body). Nothing here reaches a real machine.
"""
import runpy
from pathlib import Path

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).with_name('skill_run_browser_fixture.py')), run_name='__main__')
