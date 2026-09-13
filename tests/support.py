import tempfile
import unittest
from pathlib import Path


class FileTestCase(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".verification"
        base.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="test-", dir=base)
        self.root = Path(self.temporary.name).resolve()
        if self.root.parent != base.resolve():
            raise RuntimeError("Test cleanup must stay inside the workspace verification directory")
        self.addCleanup(self.temporary.cleanup)

    def file(self, name, content=b"identical content"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path
