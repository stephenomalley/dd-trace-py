import pytest

from ddtrace.appsec._constants import IAST
from ddtrace.appsec._iast import oce
from ddtrace.appsec._iast.constants import VULN_SSRF
from ddtrace.contrib.requests.patch import patch
from ddtrace.internal import core
from tests.appsec.iast.iast_utils import get_line_and_hash
from tests.utils import override_global_config


try:
    from ddtrace.appsec._iast._taint_tracking import OriginType  # noqa: F401
    from ddtrace.appsec._iast._taint_tracking import taint_pyobject
    from ddtrace.appsec._iast._taint_tracking.aspects import add_aspect
except (ImportError, AttributeError):
    pytest.skip("IAST not supported for this Python version", allow_module_level=True)

FIXTURES_PATH = "tests/appsec/iast/taint_sinks/test_ssrf.py"


def setup():
    oce._enabled = True


def test_ssrf(tracer, iast_span_defaults):
    with override_global_config(dict(_appsec_enabled=True, _iast_enabled=True)):
        patch()
        import requests
        from requests.exceptions import ConnectionError

        tainted_path = taint_pyobject(
            pyobject="forbidden_dir/",
            source_name="test_ssrf",
            source_value="forbidden_dir/",
            source_origin=OriginType.PARAMETER,
        )
        url = add_aspect("http://localhost/", tainted_path)
        try:
            # label test_ssrf
            requests.get(url)
        except ConnectionError:
            pass
        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
        assert span_report

        vulnerability = list(span_report.vulnerabilities)[0]
        source = span_report.sources[0]
        assert vulnerability.type == VULN_SSRF
        assert vulnerability.evidence.valueParts == [
            {"value": "http://localhost/"},
            {"source": 0, "value": tainted_path},
        ]
        assert vulnerability.evidence.value is None
        assert vulnerability.evidence.pattern is None
        assert vulnerability.evidence.redacted is None
        assert source.name == "test_ssrf"
        assert source.origin == OriginType.PARAMETER
        assert source.value == tainted_path

        line, hash_value = get_line_and_hash("test_ssrf", VULN_SSRF, filename=FIXTURES_PATH)
        assert vulnerability.location.path == FIXTURES_PATH
        assert vulnerability.location.line == line
        assert vulnerability.hash == hash_value
