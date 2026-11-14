"""Give a surface a required header to exercise automatic header propagation.

Load with `-p tests.surface_headers`. Registration is scoped to that pytest subprocess.
"""

from llmdv.surfaces import register
from llmdv.surfaces.completions import Completions


class CompletionsWithHeader(Completions):
    def extra_headers(self) -> dict[str, str]:
        return {"x-llmdv-surface": self.name}


def pytest_configure(config) -> None:
    register(CompletionsWithHeader())
