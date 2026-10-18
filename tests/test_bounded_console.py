from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ConsoleBound(unittest.TestCase):
    def test_rotates_without_losing_child_failure_status(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'console.log'
            result=subprocess.run([sys.executable,str(root/'scripts/bounded_console.py'),str(path),
                sys.executable,'-c',"import sys; [print('x'*10000) for _ in range(1000)]; sys.exit(7)"],
                capture_output=True,timeout=10)
            self.assertEqual(result.returncode,7)
            logs=list(Path(directory).glob('console.log*'))
            self.assertEqual(len(logs),3)
            self.assertLessEqual(sum(log.stat().st_size for log in logs),6*1024*1024)
