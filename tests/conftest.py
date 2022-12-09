import io
import json
import pytest
import sys

from textwrap import dedent

@pytest.fixture
def tr():
    return TestResource()

class TestResource(object):

    WORK_AREA_ROOT = 'tests/work_area/'

    OUTS = dict(
        listing_a2aa = dedent('''
            Paths to be renamed (total 3, listed 3).

            a
            aa

            b
            bb

            c
            cc

        ''').lstrip(),
        confirm3 = dedent('''
            Rename paths (total 3, listed 3) [yes]?

        ''').lstrip(),
        paths_renamed = dedent('''
            Paths renamed.
        ''').lstrip(),
        no_action = dedent('''
            No action taken.
        ''').lstrip(),
    )

    def dump(self, val = None, label = 'dump()'):
        fmt = '\n--------\n{label} =>\n{val}'
        msg = fmt.format(label = label, val = val)
        print(msg)

    def dumpj(self, val = None, label = 'dump()', indent = 4):
        val = json.dumps(val, indent = indent)
        self.dump(val, label)

class StdStreams(object):

    def __init__(self):
        self.orig_stdout = sys.stdout
        self.orig_stderr = sys.stderr
        self.reset()

    def reset(self):
        self.close()
        io1 = io.StringIO()
        io2 = io.StringIO()
        self._stdout = io1
        self._stderr = io2
        sys.stdout = io1
        sys.stderr = io2

    def close(self):
        if hasattr(self, '_stdout'):
            self._stdout.close()
            self._stderr.close()

    def restore(self):
        sys.stdout = self.orig_stdout
        sys.stderr = self.orig_stderr

    @property
    def stdout(self):
        return self._stdout.getvalue()

    @property
    def stderr(self):
        return self._stderr.getvalue()

@pytest.fixture(scope = 'function')
def std_streams():
    ss = StdStreams()
    yield ss
    ss.close()
    ss.restore()

